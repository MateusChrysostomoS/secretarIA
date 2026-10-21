"""The secretarIA's own agenda: `CalendarService`'s interface over `calendar_events`.

Founder's decision 2026-10-20: the agenda works without Google. An agenda no Google
credential covers is this one - chosen in ONE place,
services/calendar.py::CalendarService.from_tenant_config, which every factory point
already goes through (the clinic's agenda, every professional's, the LLM tools', the
hub's, the workers'). Nothing downstream branches on the backend: the flow router, the
LLM tools, the hub and the reminder/deposit paths call the same methods and get the
same shapes back.

A SUBCLASS of `CalendarService`, like workers/shared/flow_simulation.py::SimCalendar:
the opening-hours arithmetic (`list_free_slots`, `list_available_days`,
`is_slot_free`, `has_conflict`, `_windows_for_day`, `_busy_ranges`) is inherited
untouched and runs on this class's `check_availability`, so "free" means exactly the
same thing on both backends. Only the I/O is replaced; `_build_service` refuses, so no
path can reach Google from here by accident.

Semantics mirrored from Google, one by one:

  * reads (`check_availability`, `list_events`) return busy, timed events overlapping
    [start, end) as `{"id", "summary", "start", "end"}` with clinic-local RFC 3339
    strings. A row marked free (`busy=False`, Google's "transparent") is never listed;
    there are no all-day rows to skip. Never truncated: a truncated busy list is how
    free time gets offered where there is none.
  * `create_event` returns a Google-shaped resource: `id` (`loc_<hex>`), `status`,
    `start`/`end` as `{"dateTime", "timeZone"}`, and `htmlLink` None - there is no
    Google page to open, so the doctor's e-mail simply leaves its "Ver no Google
    Agenda" line out.
  * `cancel_event` is idempotent and answers like Google's after the R7 change: True
    when this call cancelled a live event, False for an unknown, already-cancelled or
    already-pushed-to-Google one.
  * `update_event` / `update_event_details` raise `CalendarEventNotFoundError` (which
    `is_event_not_found` recognises) where Google raises a 404.
  * `add_attendee` sends nothing: an invite is a Google feature. The patient still gets
    the "Adicionar à sua agenda" link in the confirmation e-mail
    (plugins/booking_notifications.py), which never depended on the invite.

Events are addressed by (tenant, id) - never by the owner as well. A Google id can live
on several calendars, which is why Google's cancel had to be asked of "the right one";
a `loc_` id is unique, so an action reaches its event even when the owner a caller
resolved has drifted (a clinic going from one active professional to two re-keys the
clinic's agenda, `from_tenant_config`). The owner matters for writes that ADD busy
time (create) and for every read.

Concurrency. Every write takes a per-agenda lock - an `asyncio.Lock` per (event loop,
agenda) inside the process, plus `pg_advisory_xact_lock` on Postgres across the API and
the worker - for the whole transaction. An EXCLUSIVE write (`exclusive=True`, used by
the patient write sites through `create_event_exclusive` & co. below) re-checks for an
overlapping busy event under that lock and raises `CalendarSlotTakenError` instead of
writing, so two patients can never both get one slot. Plain writes stay permissive like
Google's: the clinic may overlap on purpose (an encaixe, a block over a booking), and
the hub's staff edit has its own `allow_overlap` switch for that.

Every call commits in its own short session (`async_session_factory`), exactly as a
Google call commits outside the caller's transaction - so every caller's existing
"the event exists, the row failed" compensation keeps meaning the same thing.
"""

import asyncio
import hashlib
import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING, ClassVar
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import Settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models.calendar_event import (
    EVENT_STATUS_CANCELLED,
    EVENT_STATUS_CONFIRMED,
    CalendarEvent,
)
from secretaria.services.calendar import (
    DEFAULT_MAX_EVENTS,
    HUB_LIST_MAX_EVENTS,
    LOCAL_EVENT_ID_PREFIX,
    CalendarEventNotFoundError,
    CalendarService,
    CalendarSlotTakenError,
)

if TYPE_CHECKING:
    from secretaria.services.tenant_config import TenantRuntimeConfig

logger = get_logger(__name__)


def new_local_event_id() -> str:
    """A fresh `loc_<32 hex>` id. Fits `appointments.google_event_id` (VARCHAR 255)."""
    return f"{LOCAL_EVENT_ID_PREFIX}{uuid4().hex}"


def _aware(value: datetime) -> datetime:
    """UTC-attach a naive timestamp read back from SQLite (Postgres returns it aware)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------
# The per-agenda lock
# --------------------------------------------------------------------------

# One asyncio.Lock per agenda, per event loop: a lock that once waited on one loop
# refuses to be used from another (tests run one loop each; a process runs one).
_PROCESS_LOCKS: "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]" = (
    weakref.WeakKeyDictionary()
)


def _agenda_key(tenant_id: UUID, professional_id: UUID | None) -> str:
    return f"{tenant_id}:{professional_id or 'clinic'}"


def _advisory_key(agenda_key: str) -> int:
    """A stable signed 64-bit key for `pg_advisory_xact_lock` (same in every process)."""
    digest = hashlib.blake2b(f"calendar_events:{agenda_key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _process_lock(agenda_key: str) -> asyncio.Lock:
    locks = _PROCESS_LOCKS.setdefault(asyncio.get_running_loop(), {})
    lock = locks.get(agenda_key)
    if lock is None:
        lock = locks[agenda_key] = asyncio.Lock()
    return lock


async def _advisory_lock(session: AsyncSession, agenda_key: str) -> None:
    """Serialize writers of one agenda across processes until this transaction ends.

    Postgres only: SQLite (the test engine) has one writer at a time anyway, and the
    in-process lock already covers the single process it runs in.
    """
    if session.get_bind().dialect.name != "postgresql":
        return
    await session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": _advisory_key(agenda_key)}
    )


@asynccontextmanager
async def agenda_write_lock(
    tenant_id: UUID, professional_id: UUID | None
) -> AsyncIterator[AsyncSession]:
    """A session whose one transaction holds the agenda's write lock; commits on exit."""
    key = _agenda_key(tenant_id, professional_id)
    async with async_session_factory() as session, _process_lock(key), session.begin():
        await _advisory_lock(session, key)
        yield session


# --------------------------------------------------------------------------
# Queries
# --------------------------------------------------------------------------


def _owner_is(professional_id: UUID | None) -> ColumnElement[bool]:
    if professional_id is None:
        return CalendarEvent.professional_id.is_(None)
    return CalendarEvent.professional_id == professional_id


def _counts_as_busy() -> ColumnElement[bool]:
    """Live, busy, and not handed over to Google (whose copy is the one that counts)."""
    return and_(
        CalendarEvent.status == EVENT_STATUS_CONFIRMED,
        CalendarEvent.busy.is_(True),
        CalendarEvent.google_event_id.is_(None),
    )


def _overlapping(start_utc: datetime, end_utc: datetime) -> ColumnElement[bool]:
    """Google's events.list window: the event ends after `start` and starts before `end`."""
    return and_(CalendarEvent.start_at < end_utc, CalendarEvent.end_at > start_utc)


async def _overlap_exists(
    session: AsyncSession,
    tenant_id: UUID,
    professional_id: UUID | None,
    start_utc: datetime,
    end_utc: datetime,
    *,
    except_id: str | None = None,
) -> bool:
    stmt = select(CalendarEvent.id).where(
        CalendarEvent.tenant_id == tenant_id,
        _owner_is(professional_id),
        _counts_as_busy(),
        _overlapping(start_utc, end_utc),
    )
    if except_id is not None:
        stmt = stmt.where(CalendarEvent.id != except_id)
    return (await session.scalar(stmt.limit(1))) is not None


async def google_copy_id(tenant_id: UUID, event_id: str) -> str | None:
    """The Google id a local event was pushed to (services/calendar_sync.py), or None."""
    async with async_session_factory() as session:
        return await session.scalar(
            select(CalendarEvent.google_event_id).where(
                CalendarEvent.tenant_id == tenant_id, CalendarEvent.id == event_id
            )
        )


def _read_item(row: CalendarEvent, tz: ZoneInfo) -> dict:
    """The `check_availability` / `list_events` item shape (Google's `_events_page`)."""
    return {
        "id": row.id,
        "summary": row.summary,
        "start": _aware(row.start_at).astimezone(tz).isoformat(),
        "end": _aware(row.end_at).astimezone(tz).isoformat(),
    }


async def list_unpushed_events(
    session: AsyncSession,
    tenant_id: UUID,
    owners: set[UUID | None],
    start: datetime,
    end: datetime,
    tz: ZoneInfo,
) -> list[dict]:
    """Busy local events of `owners` never pushed to Google, as `list_events` items.

    For the hub agenda of a clinic whose agendas are Google now: the appointments it
    booked on the secretarIA agenda before connecting stay here (only FUTURE ones are
    pushed, services/calendar_sync.py), and so does any future one a push could not
    reach yet. Without this read they would vanish from the console. Runs on the
    caller's session (a read, no lock).
    """
    if not owners:
        return []
    start_utc = (start if start.tzinfo else start.replace(tzinfo=tz)).astimezone(UTC)
    end_utc = (end if end.tzinfo else end.replace(tzinfo=tz)).astimezone(UTC)
    clauses = []
    ids = [owner for owner in owners if owner is not None]
    if ids:
        clauses.append(CalendarEvent.professional_id.in_(ids))
    if None in owners:
        clauses.append(CalendarEvent.professional_id.is_(None))
    rows = await session.scalars(
        select(CalendarEvent)
        .where(
            CalendarEvent.tenant_id == tenant_id,
            or_(*clauses),
            _counts_as_busy(),
            _overlapping(start_utc, end_utc),
        )
        .order_by(CalendarEvent.start_at)
    )
    return [_read_item(row, tz) for row in rows]


# --------------------------------------------------------------------------
# The service
# --------------------------------------------------------------------------


class LocalCalendarService(CalendarService):
    """One secretarIA agenda: the clinic's (`professional_id=None`) or one doctor's."""

    backend: ClassVar[str] = "local"

    def __init__(
        self,
        tenant_id: UUID,
        professional_id: UUID | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        super().__init__(settings)
        self._tenant_id = tenant_id
        self._owner_professional_id = professional_id
        # Informational only (nothing here sends it anywhere): names the agenda in logs.
        self._calendar_id = f"secretaria:{professional_id or 'clinic'}"
        self._refresh_token_override = None

    @classmethod
    def for_config(
        cls, config: "TenantRuntimeConfig", *, professional_id: UUID | None
    ) -> "LocalCalendarService":
        """The agenda of `professional_id` (None = the clinic's) with `config`'s hours."""
        instance = cls(config.tenant_id, professional_id)
        instance._tz = ZoneInfo(config.timezone)
        instance._business_hours = config.business_hours or {}
        instance._default_slot_minutes = config.appointment_duration_min or 30
        return instance

    @classmethod
    def from_tenant_config(
        cls, config: "TenantRuntimeConfig", *, professional_id: UUID | None = None
    ) -> "LocalCalendarService":
        """Always local: asked of this class, the credential no longer chooses."""
        owner = professional_id if professional_id is not None else config.professional_id
        return cls.for_config(config, professional_id=owner)

    @classmethod
    def mirroring(cls, calendar: CalendarService) -> "LocalCalendarService":
        """The secretarIA agenda of the same tenant/owner, hours and zone as `calendar`."""
        local = cls(calendar._tenant_id, calendar._owner_professional_id)
        local._tz = calendar._tz
        local._business_hours = calendar._business_hours
        local._default_slot_minutes = calendar._default_slot_minutes
        return local

    @property
    def tenant_id(self) -> UUID:
        return self._tenant_id

    @property
    def professional_id(self) -> UUID | None:
        """Whose agenda: a professional's id, or None for the clinic's own."""
        return self._owner_professional_id

    def _build_service(self):
        raise RuntimeError("The secretarIA agenda has no Google client.")

    def references_same_calendar(self, other: object) -> bool:
        """Same tenant and same owner. Never the same as any Google agenda."""
        return (
            isinstance(other, LocalCalendarService)
            and other._tenant_id == self._tenant_id
            and other._owner_professional_id == self._owner_professional_id
        )

    def _utc(self, value: datetime) -> datetime:
        return self._ensure_tz(value).astimezone(UTC)

    def _window(self, start: datetime, end: datetime) -> tuple[datetime, datetime]:
        start_utc, end_utc = self._utc(start), self._utc(end)
        if end_utc < start_utc:
            # Google answers 400 timeRangeEmpty; the hub turned that into an error too.
            raise ValueError("The event ends before it starts.")
        return start_utc, end_utc

    def _resource(self, row: CalendarEvent) -> dict:
        """The Google-shaped event resource every write returns."""
        tz_name = str(self._tz)
        return {
            "id": row.id,
            "status": row.status,
            "htmlLink": None,
            "summary": row.summary,
            "description": row.description,
            "transparency": "opaque" if row.busy else "transparent",
            "start": {
                "dateTime": _aware(row.start_at).astimezone(self._tz).isoformat(),
                "timeZone": tz_name,
            },
            "end": {
                "dateTime": _aware(row.end_at).astimezone(self._tz).isoformat(),
                "timeZone": tz_name,
            },
        }

    async def _busy_rows(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        start_utc, end_utc = self._utc(start), self._utc(end)
        async with async_session_factory() as session:
            rows = await session.scalars(
                select(CalendarEvent)
                .where(
                    CalendarEvent.tenant_id == self._tenant_id,
                    _owner_is(self._owner_professional_id),
                    _counts_as_busy(),
                    _overlapping(start_utc, end_utc),
                )
                .order_by(CalendarEvent.start_at)
            )
            return list(rows)

    # ---- reads -------------------------------------------------------------

    async def check_availability(
        self, start: datetime, end: datetime, max_results: int = DEFAULT_MAX_EVENTS
    ) -> list[dict]:
        """Busy events overlapping [start, end). `max_results` is accepted, not applied."""
        return [_read_item(row, self._tz) for row in await self._busy_rows(start, end)]

    async def list_events(
        self, start: datetime, end: datetime, max_events: int = HUB_LIST_MAX_EVENTS
    ) -> list[dict]:
        """The hub agenda's read: same items, capped at `max_events` like Google's paging."""
        items = [_read_item(row, self._tz) for row in await self._busy_rows(start, end)]
        if len(items) > max_events:
            logger.warning(
                "local_calendar_events_list_truncated", events=len(items), max_events=max_events
            )
        return items[:max_events]

    async def get_event(self, event_id: str) -> dict | None:
        """The event (cancelled ones too, like Google), or None for an unknown id."""
        async with async_session_factory() as session:
            row = await session.scalar(
                select(CalendarEvent).where(
                    CalendarEvent.tenant_id == self._tenant_id, CalendarEvent.id == event_id
                )
            )
            return self._resource(row) if row is not None else None

    # ---- writes ------------------------------------------------------------

    async def create_event(
        self,
        start: datetime,
        end: datetime,
        summary: str,
        description: str = "",
        reminders: list[dict] | None = None,
        *,
        exclusive: bool = False,
        event_id: str | None = None,
    ) -> dict:
        """Add a busy event to THIS agenda. `reminders` is Google's popups: ignored here.

        `exclusive=True` refuses with `CalendarSlotTakenError` when a busy event of this
        agenda already overlaps - checked and written under the agenda's lock, so the
        answer cannot go stale between the check and the write.
        """
        start_utc, end_utc = self._window(start, end)
        owner = self._owner_professional_id
        async with agenda_write_lock(self._tenant_id, owner) as session:
            if event_id is not None:
                if not event_id.startswith(LOCAL_EVENT_ID_PREFIX):
                    raise ValueError("Native agenda event ids must have the local prefix.")
                existing = await session.get(CalendarEvent, event_id)
                if existing is not None:
                    if existing.tenant_id != self._tenant_id or existing.professional_id != owner:
                        raise CalendarEventNotFoundError("Event not found on this agenda.")
                    # A payment retry must recover its event before the exclusive overlap check.
                    return self._resource(existing)
            if exclusive and await _overlap_exists(
                session, self._tenant_id, owner, start_utc, end_utc
            ):
                logger.info("local_calendar_slot_taken", action="create", **self._log_ids())
                raise CalendarSlotTakenError("The slot is already taken on this agenda.")
            now = _now()
            row = CalendarEvent(
                id=event_id if event_id is not None else new_local_event_id(),
                tenant_id=self._tenant_id,
                professional_id=owner,
                summary=summary,
                description=description,
                start_at=start_utc,
                end_at=end_utc,
                busy=True,
                status=EVENT_STATUS_CONFIRMED,
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            # Built before the commit: every field was set here, nothing to reload.
            event = self._resource(row)
        logger.info("local_calendar_event_created", event_id=event["id"], **self._log_ids())
        return event

    async def update_event(
        self, event_id: str, start: datetime, end: datetime, *, exclusive: bool = False
    ) -> dict:
        """Move a live event to [start, end). Raises `CalendarEventNotFoundError` like a 404."""
        return await self._patch(event_id, start, end, exclusive=exclusive, details=None)

    async def update_event_details(
        self,
        event_id: str,
        start: datetime,
        end: datetime,
        summary: str,
        description: str = "",
        *,
        exclusive: bool = False,
    ) -> dict:
        """Move an event AND rewrite its title and description (the R6 "Alterar Dados")."""
        return await self._patch(
            event_id, start, end, exclusive=exclusive, details=(summary, description)
        )

    async def _patch(
        self,
        event_id: str,
        start: datetime,
        end: datetime,
        *,
        exclusive: bool,
        details: tuple[str, str] | None,
    ) -> dict:
        start_utc, end_utc = self._window(start, end)
        # The lock is the EVENT's agenda, whichever agenda object was asked (see the
        # module docstring); an event never changes owner, so reading it first is safe.
        async with async_session_factory() as session:
            found = (
                await session.execute(
                    select(CalendarEvent.professional_id).where(
                        CalendarEvent.tenant_id == self._tenant_id, CalendarEvent.id == event_id
                    )
                )
            ).first()
        if found is None:
            raise CalendarEventNotFoundError(event_id)
        owner = found[0]
        async with agenda_write_lock(self._tenant_id, owner) as session:
            row = await session.scalar(
                select(CalendarEvent)
                .where(CalendarEvent.tenant_id == self._tenant_id, CalendarEvent.id == event_id)
                .with_for_update()
            )
            if (
                row is None
                or row.status != EVENT_STATUS_CONFIRMED
                or row.google_event_id is not None
            ):
                raise CalendarEventNotFoundError(event_id)
            if exclusive and await _overlap_exists(
                session, self._tenant_id, owner, start_utc, end_utc, except_id=event_id
            ):
                logger.info("local_calendar_slot_taken", action="move", **self._log_ids())
                raise CalendarSlotTakenError("The slot is already taken on this agenda.")
            row.start_at, row.end_at = start_utc, end_utc
            if details is not None:
                row.summary, row.description = details
            row.updated_at = _now()
            event = self._resource(row)
        logger.info("local_calendar_event_updated", event_id=event_id, **self._log_ids())
        return event

    async def cancel_event(self, event_id: str) -> bool:
        """Cancel a live event. Idempotent: False when there was nothing live to cancel.

        A row already pushed to Google answers False as well: its Google copy is the
        event, and a caller acting on the old id must not "succeed" on a dead row
        while the copy keeps the slot (a Google agenda routes such an id to the copy -
        services/calendar.py::CalendarService._local_event_target).
        """
        async with async_session_factory() as session, session.begin():
            result = await session.execute(
                update(CalendarEvent)
                .where(
                    CalendarEvent.tenant_id == self._tenant_id,
                    CalendarEvent.id == event_id,
                    CalendarEvent.status == EVENT_STATUS_CONFIRMED,
                    CalendarEvent.google_event_id.is_(None),
                )
                .values(status=EVENT_STATUS_CANCELLED, updated_at=_now())
                .execution_options(synchronize_session=False)
            )
        cancelled = result.rowcount == 1
        logger.info(
            "local_calendar_event_cancelled" if cancelled else "local_calendar_event_already_gone",
            event_id=event_id,
            **self._log_ids(),
        )
        return cancelled

    async def add_attendee(self, event_id: str, email: str) -> dict:
        """No invite here: an invite is a Google feature. Never logs `email`."""
        logger.info("local_calendar_attendee_skipped", event_id=event_id, **self._log_ids())
        return {"id": event_id}

    async def create_secondary_calendar(self, summary: str) -> dict:
        raise RuntimeError("Secondary calendars exist only on a connected Google account.")

    def _log_ids(self) -> dict:
        return {
            "tenant_id": str(self._tenant_id),
            "professional_id": str(self._owner_professional_id)
            if self._owner_professional_id
            else None,
        }


# --------------------------------------------------------------------------
# The patient write sites' exclusive writes
# --------------------------------------------------------------------------
#
# A patient's own booking or move must never land on a time somebody else holds. Each
# patient write site already re-reads the agenda right before writing
# (services/availability.py::slot_taken_at_confirm); these make the write itself refuse
# on the secretarIA agenda, where that is atomic, and raise `CalendarSlotTakenError`,
# which each site answers exactly like a taken re-check. On any other calendar (Google,
# a test double) they are the plain call, byte for byte as before.


async def create_event_exclusive(
    calendar, *, start: datetime, end: datetime, summary: str, description: str = "",
    event_id: str | None = None,
) -> dict:
    # Only payment/release retries choose an id; preserve other callers' exact arguments.
    identity = {"event_id": event_id} if event_id is not None else {}
    if isinstance(calendar, LocalCalendarService):
        return await calendar.create_event(
            start=start, end=end, summary=summary, description=description, exclusive=True,
            **identity,
        )
    return await calendar.create_event(
        start=start, end=end, summary=summary, description=description, **identity,
    )


async def update_event_exclusive(
    calendar, event_id: str, *, start: datetime, end: datetime
) -> dict:
    if isinstance(calendar, LocalCalendarService):
        return await calendar.update_event(event_id, start=start, end=end, exclusive=True)
    return await calendar.update_event(event_id, start=start, end=end)


async def update_event_details_exclusive(
    calendar, event_id: str, start: datetime, end: datetime, summary: str, description: str = ""
) -> dict:
    if isinstance(calendar, LocalCalendarService):
        return await calendar.update_event_details(
            event_id, start, end, summary, description, exclusive=True
        )
    return await calendar.update_event_details(event_id, start, end, summary, description)
