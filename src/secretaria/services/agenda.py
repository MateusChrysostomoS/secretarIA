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
  no-show do not block);
* local `calendar_events` that are confirmed, busy and not handed over to Google - the same
  predicate `local_calendar._counts_as_busy` applies (not imported: services/local_calendar.py
  will import THIS module, and the two must not form a cycle);
* `booking_holds` in the `payment` phase that have not expired (`BLOCKING_PHASE`; a code-phase hold
  reserves nothing, TASK-072).

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
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import and_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    BookingHold,
    CalendarEvent,
    Professional,
)
from secretaria.models.calendar_event import EVENT_STATUS_CONFIRMED
from secretaria.services.booking_hold import BLOCKING_PHASE


@dataclass(frozen=True)
class BusyWindow:
    """One busy interval of a sibling professional. Nothing else about it is ever exposed."""

    start: datetime
    end: datetime


def _aware(value: datetime) -> datetime:
    """UTC-attach a naive timestamp read back from SQLite (Postgres returns it aware)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


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


async def shared_busy(
    session: AsyncSession,
    professional_id: UUID | None,
    start: datetime,
    end: datetime,
) -> list[BusyWindow]:
    """Windows of the siblings' live appointments, local busy events and payment holds that
    overlap `[start, end)`; sorted, deduplicated, windows only. `[]` without siblings."""
    sibling_ids = await sibling_professional_ids(session, professional_id)
    if not sibling_ids:
        return []

    windows: set[tuple[datetime, datetime]] = set()

    appointments = await session.execute(
        select(Appointment.start_at, Appointment.end_at).where(
            Appointment.professional_id.in_(sibling_ids),
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            Appointment.start_at.is_not(None),
            Appointment.end_at.is_not(None),
            Appointment.start_at < end,
            Appointment.end_at > start,
        )
    )
    windows.update((_aware(s), _aware(e)) for s, e in appointments.all())

    events = await session.execute(
        select(CalendarEvent.start_at, CalendarEvent.end_at).where(
            CalendarEvent.professional_id.in_(sibling_ids),
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

    return [BusyWindow(s, e) for s, e in sorted(windows)]


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
