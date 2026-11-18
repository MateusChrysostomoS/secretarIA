"""Shared per-person agenda: busy time of a person's OTHER clinics + its write lock (TASK-083).

A doctor who works at two clinics has two `professionals` rows (one per tenant). When both rows
point at the same `agendas` row (`professionals.agenda_id`, see services/agenda_linking.py) they are
SIBLINGS: the person is in one place at a time, so a window busy at one clinic is busy at the other.

## What crosses the tenant boundary

Only bare `(start, end)` windows (`BusyWindow`). Never a summary, patient, service, unit or clinic
name, and never a tenant id - not in the return value, not in a log line. `BusyWindow` has exactly
two fields on purpose: there is nothing else to leak.

## What counts as busy (the sibling's side)

* appointments in a LIVE status (`SCHEDULED`/`CONFIRMED`/`RESCHEDULED`; cancelled, attended and
  no-show do not block) that occupy their slot - TASK-085 R (D-R10): a consultation SUSPENDED by a
  change waiting for the Pix of its difference has its event off the agenda, so its row does not
  count either (`adjustment_suspension.occupies_slot_clause`);
* local `calendar_events` that are confirmed, busy and not handed over to Google - the same
  predicate `local_calendar._counts_as_busy` applies (not imported: services/local_calendar.py
  will import THIS module, and the two must not form a cycle);
* `booking_holds` in the `payment` phase that have not expired (`BLOCKING_PHASE`; a code-phase hold
  reserves nothing, TASK-072);
* fresh Google-write LEASES (`calendar_events` rows in status `google_lease`, younger than
  `GOOGLE_WRITE_LEASE`): a Google agenda's booking is invisible to the other clinic's Google, so
  the exclusive Google write leaves its bare window here until the appointment row exists.

A local booking writes an appointment AND a calendar event on the same window; identical windows are
returned once.

## Without an agenda

A professional with no `agenda_id` (or an unknown id) has no siblings: every function here returns
empty / does nothing, so every caller keeps today's behaviour (spec §3).

## Errors

A database error propagates (same database as everything else). Only the brain-api call that LINKS
agendas is fail-soft, and that lives in services/agenda_linking.py.
"""

import hashlib
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import async_session_factory
from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    BookingHold,
    CalendarEvent,
    Professional,
)
from secretaria.models.calendar_event import EVENT_STATUS_CONFIRMED, EVENT_STATUS_GOOGLE_LEASE
from secretaria.services.booking_hold import BLOCKING_PHASE
from secretaria.services.payments.adjustment_suspension import occupies_slot_clause

# How long a Google exclusive write's lease (models/calendar_event.py::EVENT_STATUS_GOOGLE_LEASE)
# counts as the sibling's busy time. It only has to bridge the gap between the Google write and
# the caller committing its appointment row (the same turn: seconds); past it the appointment
# row is what counts. Generous on purpose: a lease that outlives a cancel only keeps one window
# busy a little longer, never double-books.
GOOGLE_WRITE_LEASE = timedelta(minutes=2)


@dataclass(frozen=True)
class BusyWindow:
    """One busy interval of a sibling professional. Nothing else about it is ever exposed."""

    start: datetime
    end: datetime


def _aware(value: datetime) -> datetime:
    """UTC-attach a naive timestamp read back from SQLite (Postgres returns it aware)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _utc(value: datetime) -> datetime:
    """A query bound in UTC. SQLite drops the offset and compares wall clocks, so a bound in
    the clinic's zone would miss rows (local_calendar.py converts every bound the same way).
    A naive value is read as UTC, like the timestamps this module reads back."""
    return _aware(value).astimezone(UTC)


async def agenda_id_of(session: AsyncSession, professional_id: UUID | None) -> UUID | None:
    """The agenda a professional belongs to, or None (no agenda / unknown professional)."""
    if professional_id is None:
        return None
    return await session.scalar(
        select(Professional.agenda_id).where(Professional.id == professional_id)
    )


async def sibling_professional_ids(
    session: AsyncSession, professional_id: UUID | None
) -> list[UUID]:
    """The OTHER professionals (any tenant) on the same agenda; `[]` without an agenda."""
    agenda_id = await agenda_id_of(session, professional_id)
    if agenda_id is None:
        return []
    rows = await session.scalars(
        select(Professional.id)
        .where(Professional.agenda_id == agenda_id, Professional.id != professional_id)
        .order_by(Professional.id)
    )
    return list(rows)


async def agenda_members(session: AsyncSession, agenda_id: UUID) -> list[tuple[UUID, UUID]]:
    """`(tenant_id, professional_id)` of every professional on one agenda (any tenant)."""
    rows = await session.execute(
        select(Professional.tenant_id, Professional.id)
        .where(Professional.agenda_id == agenda_id)
        .order_by(Professional.id)
    )
    return [(tenant_id, pid) for tenant_id, pid in rows.all()]


def owned_events(members: Iterable[tuple[UUID, UUID]]) -> ColumnElement[bool]:
    """`calendar_events` rows owned by these `(tenant_id, professional_id)` pairs.

    Final wave M2: the table's only index is `(tenant_id, professional_id, start_at)`
    (models/calendar_event.py), so a predicate on `professional_id` alone cannot use it and
    scans the table on Postgres - under the person's advisory lock. Grouped per tenant
    (`tenant_id = t AND professional_id IN (...)`, OR-ed) it can. Same rows as before: a
    professional belongs to exactly one tenant. Never call with an empty `members`.
    """
    by_tenant: dict[UUID, list[UUID]] = {}
    for tenant_id, pid in members:
        by_tenant.setdefault(tenant_id, []).append(pid)
    return or_(
        *(
            and_(CalendarEvent.tenant_id == tenant_id, CalendarEvent.professional_id.in_(ids))
            for tenant_id, ids in sorted(by_tenant.items())
        )
    )


async def shared_busy(
    session: AsyncSession,
    professional_id: UUID | None,
    start: datetime,
    end: datetime,
) -> list[BusyWindow]:
    """Windows of the siblings' live appointments, local busy events and payment holds that
    overlap `[start, end)`; sorted, deduplicated, windows only. `[]` without siblings.

    `start`/`end` may be in any zone: they are converted to UTC before querying."""
    start, end = _utc(start), _utc(end)
    agenda_id = await agenda_id_of(session, professional_id)
    if agenda_id is None:
        return []
    siblings = [
        (tenant_id, pid)
        for tenant_id, pid in await agenda_members(session, agenda_id)
        if pid != professional_id
    ]
    if not siblings:
        return []
    sibling_ids = [pid for _tenant_id, pid in siblings]

    windows: set[tuple[datetime, datetime]] = set()

    appointments = await session.execute(
        select(Appointment.start_at, Appointment.end_at).where(
            Appointment.professional_id.in_(sibling_ids),
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            Appointment.start_at.is_not(None),
            Appointment.end_at.is_not(None),
            Appointment.start_at < end,
            Appointment.end_at > start,
            occupies_slot_clause(),
        )
    )
    windows.update((_aware(s), _aware(e)) for s, e in appointments.all())

    events = await session.execute(
        select(CalendarEvent.start_at, CalendarEvent.end_at).where(
            owned_events(siblings),
            and_(
                CalendarEvent.status == EVENT_STATUS_CONFIRMED,
                CalendarEvent.busy.is_(True),
                CalendarEvent.google_event_id.is_(None),
            ),
            CalendarEvent.start_at < end,
            CalendarEvent.end_at > start,
        )
    )
    windows.update((_aware(s), _aware(e)) for s, e in events.all())

    holds = await session.execute(
        select(BookingHold.start_at, BookingHold.end_at).where(
            BookingHold.professional_id.in_(sibling_ids),
            BookingHold.phase == BLOCKING_PHASE,
            BookingHold.expires_at > datetime.now(UTC),
            BookingHold.start_at < end,
            BookingHold.end_at > start,
        )
    )
    windows.update((_aware(s), _aware(e)) for s, e in holds.all())

    # A sibling's fresh Google exclusive write whose appointment row may not exist yet.
    leases = await session.execute(
        select(CalendarEvent.start_at, CalendarEvent.end_at).where(
            owned_events(siblings),
            CalendarEvent.status == EVENT_STATUS_GOOGLE_LEASE,
            CalendarEvent.created_at > datetime.now(UTC) - GOOGLE_WRITE_LEASE,
            CalendarEvent.start_at < end,
            CalendarEvent.end_at > start,
        )
    )
    windows.update((_aware(s), _aware(e)) for s, e in leases.all())

    return [BusyWindow(s, e) for s, e in sorted(windows)]


# --------------------------------------------------------------------------
# The read seam CalendarService consumes (spec §7 item 1)
# --------------------------------------------------------------------------

SharedBusyProvider = Callable[[datetime, datetime], Awaitable[list[BusyWindow]]]


async def professionals_with_siblings(
    session: AsyncSession, professionals: Iterable[Professional]
) -> frozenset[UUID]:
    """The ids, among `professionals`, whose agenda has at least one OTHER professional.

    No query at all when none of them has an agenda (spec §3: nothing changes, nothing extra
    is read); otherwise one grouped count. Siblings are any row on the agenda, active or not,
    exactly as `sibling_professional_ids` counts them.
    """
    by_id = {
        p.id: p.agenda_id
        for p in professionals
        # isinstance, not `is not None`: a stand-in row without the column reads as no agenda.
        if isinstance(getattr(p, "agenda_id", None), UUID)
    }
    if not by_id:
        return frozenset()
    shared = set(
        await session.scalars(
            select(Professional.agenda_id)
            .where(Professional.agenda_id.in_(set(by_id.values())))
            .group_by(Professional.agenda_id)
            .having(func.count(Professional.id) > 1)
        )
    )
    return frozenset(pid for pid, agenda_id in by_id.items() if agenda_id in shared)


def shared_busy_provider(professional_id: UUID) -> SharedBusyProvider:
    """`shared_busy` for one professional, as the CalendarService's optional read seam.

    Each call reads in its OWN short session (like every local-agenda read, and like a Google
    read happens outside the caller's transaction): it takes no lock, holds no connection past
    the call and never joins - or waits on - the caller's transaction. Built only for a
    professional that has siblings (services/calendar.py::CalendarService.from_tenant_config).
    """

    async def _provider(start: datetime, end: datetime) -> list[BusyWindow]:
        async with async_session_factory() as session:
            return await shared_busy(session, professional_id, _utc(start), _utc(end))

    return _provider


# --------------------------------------------------------------------------
# The cross-tenant write lock
# --------------------------------------------------------------------------


def agenda_lock_key(agenda_id: UUID) -> str:
    """The lock name every writer of one agenda must use, whatever its calendar backend."""
    return f"agenda:{agenda_id}"


def _advisory_key(agenda_id: UUID) -> int:
    """A stable signed 64-bit key for `pg_advisory_xact_lock` (same in every process)."""
    digest = hashlib.blake2b(agenda_lock_key(agenda_id).encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


async def agenda_write_lock(session: AsyncSession, agenda_id: UUID | None) -> None:
    """Serialize writers of one person's agenda, across clinics, until the transaction ends.

    Same pattern as `local_calendar._advisory_lock`: Postgres only (SQLite has one writer at a
    time anyway), a no-op without an agenda. The key is `hash("agenda:" + agenda_id)` and is
    deliberately NOT prefixed with a table name: a Google-backed clinic and a local-agenda clinic
    of the same person must contend on the SAME lock.
    """
    if agenda_id is None or session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _advisory_key(agenda_id)}
    )
