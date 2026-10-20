"""Connect-time sync: push the secretarIA agenda's FUTURE events to Google, once.

Founder's decision 2026-10-20: "Caso o Google esteja conectado, ele marca todos os
compromissos e sincroniza a agenda". A clinic that started on the secretarIA's own
agenda (services/local_calendar.py) and connects Google later must not end up with two
agendas: the moment a credential covers an agenda, `CalendarService.from_tenant_config`
answers Google for it, so its upcoming bookings have to be on Google too or they stop
blocking time there.

`push_future_local_events` does that for one tenant, for every agenda a credential now
covers - the clinic's, a shared_account doctor's (the clinic token), a per_professional
doctor's own - and leaves alone any agenda still without one. For each FUTURE live
event never pushed (`end_at > now`, confirmed, `google_event_id` NULL):

  1. under the agenda's write lock (so a cancel or move cannot interleave), re-read the
     row - skipped if it was cancelled, moved to Google, or ended meanwhile;
  2. create it on the owner's Google calendar with a DETERMINISTIC id
     (`google_id_for`: "sec" + the local hex), so a retry after a half-finished push
     gets Google's 409 and recovers the existing copy instead of making a second one;
  3. in the SAME transaction, record the copy on the row (`google_event_id`,
     `synced_at`) and repoint every `appointments` row of the tenant from the `loc_` id
     to the Google id (and its `htmlLink`), so every later action reaches Google.

Past events stay on the secretarIA agenda (the hub still lists them and can act on them:
api/hub/calendar.py::_with_unpushed_local_events, services/calendar.py::
_local_event_target). Idempotent and safe to retry: a pushed row is never read again,
and the deterministic id makes a lost commit harmless. Logs one summary line,
`calendar_local_sync_done`, ids and counts only.

Run by the arq job `sync_local_calendar_to_google` (workers/calendar_sync.py), enqueued
by the OAuth callback (api/hub/oauth.py) on every successful connection, tenant or
professional; and on demand by POST /admin/tenants/{id}/calendar/sync-local.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Professional, Tenant
from secretaria.models.calendar_event import EVENT_STATUS_CONFIRMED, CalendarEvent
from secretaria.services import local_calendar
from secretaria.services.calendar import (
    LOCAL_EVENT_ID_PREFIX,
    CalendarService,
    CalendarUnavailableError,
    GoogleTokenRevokedError,
)
from secretaria.services.local_calendar import LocalCalendarService
from secretaria.services.patient_context import as_utc
from secretaria.services.tenant_config import load_tenant_config, resolve_professional_calendar

logger = get_logger(__name__)

# Google event ids are base32hex (0-9, a-v), 5 to 1024 characters. "sec" and the
# uuid hex of the local id are both inside that alphabet; "loc_" is not.
_GOOGLE_ID_PREFIX = "sec"


def google_id_for(local_event_id: str) -> str:
    """The Google id a local event is pushed under - the same on every retry. Pure."""
    return _GOOGLE_ID_PREFIX + local_event_id.removeprefix(LOCAL_EVENT_ID_PREFIX)


@dataclass
class SyncSummary:
    """What one run did. Counts only: safe to log and to return from an admin route."""

    tenant_id: str
    pushed: int = 0
    # Already on Google from an earlier run whose commit was lost (Google answered 409).
    recovered: int = 0
    # Future events of agendas no credential covers yet: they stay where they are.
    left_local: int = 0
    # Cancelled, moved or ended between the scan and the push: nothing to do.
    skipped: int = 0
    failed: int = 0
    # Google could not be reached / refused the credential: worth a retry (or not).
    unavailable: bool = False
    reconnect_required: bool = False

    def as_dict(self) -> dict:
        return asdict(self)


def _is_conflict(exc: BaseException) -> bool:
    return getattr(getattr(exc, "resp", None), "status", None) == 409


async def _owner_calendars(
    session, tenant: Tenant, owners: set[UUID | None]
) -> dict[UUID | None, CalendarService]:
    """Each owner's agenda as the factories resolve it NOW (Google or still local)."""
    config = await load_tenant_config(session, tenant)
    calendars: dict[UUID | None, CalendarService] = {}
    for owner in owners:
        if owner is None:
            calendars[owner] = CalendarService.from_tenant_config(config)
            continue
        professional = await session.scalar(
            select(Professional).where(
                Professional.id == owner, Professional.tenant_id == tenant.id
            )
        )
        if professional is None:
            continue
        calendars[owner] = await resolve_professional_calendar(
            session, tenant, professional, tenant_config=config
        )
    return calendars


async def _push_one(
    tenant_id: UUID, owner: UUID | None, event_id: str, calendar: CalendarService, now: datetime
) -> str:
    """Push ONE event; "pushed" | "recovered" | "skipped". Raises what Google raises."""
    async with local_calendar.agenda_write_lock(tenant_id, owner) as session:
        row = await session.scalar(
            select(CalendarEvent)
            .where(CalendarEvent.tenant_id == tenant_id, CalendarEvent.id == event_id)
            .with_for_update()
        )
        if (
            row is None
            or row.status != EVENT_STATUS_CONFIRMED
            or row.google_event_id is not None
            or as_utc(row.end_at) <= now
        ):
            return "skipped"
        google_id = google_id_for(row.id)
        outcome = "pushed"
        try:
            created = await calendar.create_event(
                as_utc(row.start_at),
                as_utc(row.end_at),
                row.summary or "",
                row.description or "",
                event_id=google_id,
            )
        except Exception as exc:
            if not _is_conflict(exc):
                raise
            # An earlier run created it and lost its commit: adopt that copy.
            created = await calendar.get_event(google_id) or {"id": google_id}
            outcome = "recovered"
        link = created.get("htmlLink")
        row.google_event_id = created.get("id") or google_id
        row.synced_at = now
        await session.execute(
            update(Appointment)
            .where(Appointment.tenant_id == tenant_id, Appointment.google_event_id == row.id)
            .values(google_event_id=row.google_event_id, google_event_link=link)
            .execution_options(synchronize_session=False)
        )
    return outcome


async def push_future_local_events(
    tenant_id: UUID, *, now: datetime | None = None
) -> SyncSummary:
    """Push every future, never-pushed local event of `tenant_id` a credential now covers."""
    now = now or datetime.now(UTC)
    summary = SyncSummary(tenant_id=str(tenant_id))
    async with async_session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            logger.warning("calendar_local_sync_unknown_tenant", tenant_id=str(tenant_id))
            return summary
        rows = (
            await session.execute(
                select(CalendarEvent.id, CalendarEvent.professional_id)
                .where(
                    CalendarEvent.tenant_id == tenant_id,
                    CalendarEvent.status == EVENT_STATUS_CONFIRMED,
                    CalendarEvent.google_event_id.is_(None),
                    CalendarEvent.end_at > now,
                )
                .order_by(CalendarEvent.start_at)
            )
        ).all()
        by_owner: dict[UUID | None, list[str]] = {}
        for event_id, owner in rows:
            by_owner.setdefault(owner, []).append(event_id)
        calendars = await _owner_calendars(session, tenant, set(by_owner)) if by_owner else {}

    for owner, event_ids in by_owner.items():
        calendar = calendars.get(owner)
        if calendar is None or isinstance(calendar, LocalCalendarService):
            summary.left_local += len(event_ids)
            continue
        for index, event_id in enumerate(event_ids):
            try:
                outcome = await _push_one(tenant_id, owner, event_id, calendar, now)
            except CalendarUnavailableError as exc:
                # Nothing more will get through on this agenda right now.
                summary.unavailable = True
                summary.reconnect_required |= isinstance(exc, GoogleTokenRevokedError)
                summary.failed += len(event_ids) - index
                logger.warning(
                    "calendar_local_sync_unavailable",
                    tenant_id=str(tenant_id),
                    professional_id=str(owner) if owner else None,
                    error_type=type(exc).__name__,
                )
                break
            except Exception as exc:
                summary.failed += 1
                logger.error(
                    "calendar_local_sync_event_failed",
                    tenant_id=str(tenant_id),
                    event_id=event_id,
                    error_type=type(exc).__name__,
                )
                continue
            if outcome == "pushed":
                summary.pushed += 1
            elif outcome == "recovered":
                summary.recovered += 1
            else:
                summary.skipped += 1

    logger.info("calendar_local_sync_done", **summary.as_dict())
    return summary
