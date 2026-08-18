"""Google Calendar integration for the clinic.

Fase A (single tenant): credentials come from the environment
(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN, scope
calendar.events). Multi-tenant (Phase 5): the refresh token moves to an
encrypted column on `tenants` and `CalendarService` takes a `Tenant` row.

The googleapiclient library is blocking, so every public method offloads to
a worker thread with asyncio.to_thread to keep the event loop responsive.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from secretaria.services.tenant_config import TenantRuntimeConfig

from google.auth.exceptions import RefreshError, TransportError
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from httplib2 import ServerNotFoundError

from secretaria.config import Settings, get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar.events",
    # Covers calendars.insert AND full CRUD on events in calendars the app
    # itself created (shared_account mode — create_secondary_calendar below).
    # calendar.events (above) remains required for calendars the app did NOT
    # create: the clinic's own primary, and professionals' own accounts.
    "https://www.googleapis.com/auth/calendar.app.created",
]
TOKEN_URI = "https://oauth2.googleapis.com/token"

# Weekday index (date.weekday(): Mon=0) -> business_hours JSON key.
_WEEKDAY_KEYS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

# Network / transport failures that mean Google Calendar is unreachable right
# now (not a logic error). OSError covers socket.timeout, ConnectionError and
# TimeoutError; ServerNotFoundError (httplib2) and TransportError (google-auth)
# are separate hierarchies. All are surfaced as CalendarUnavailableError.
_NETWORK_ERRORS: tuple[type[BaseException], ...] = (
    OSError,
    TransportError,
    ServerNotFoundError,
)

# HTTP statuses that mean the calendar is unavailable rather than the request
# being wrong: auth expired/revoked (401/403) and Google-side outages (5xx).
_UNAVAILABLE_HTTP_STATUSES = frozenset({401, 403, 500, 502, 503, 504})

# events.list page size for a single-day question (the unchanged default) vs
# the multi-week scan `list_available_days` runs. 50 is plenty for one day and
# keeps every existing caller's payload small; a 20-day window at a busy clinic
# easily exceeds it, and a truncated busy list would advertise days that are
# actually full. 2500 is the Calendar API's own per-page maximum.
DEFAULT_MAX_EVENTS = 50
DAY_SCAN_MAX_EVENTS = 2500


class CalendarUnavailableError(Exception):
    """Raised when Google Calendar cannot be reached or refused our credentials.

    Distinct from a programming/logic error so the agent layer can degrade
    gracefully (hand the conversation to a human) instead of returning the
    generic retry fallback. Triggered by an expired/revoked refresh token
    (401/403 or RefreshError), a Google-side 5xx, or a network failure.
    """


def _raise_if_unavailable(exc: HttpError) -> None:
    """Translate an outage/auth HttpError into CalendarUnavailableError.

    Returns normally for client errors (404, 409, 400, ...) so the caller can
    handle or re-raise them as before.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status in _UNAVAILABLE_HTTP_STATUSES:
        raise CalendarUnavailableError(f"Google Calendar returned HTTP {status}") from exc


class GoogleScopeInsufficientError(Exception):
    """A 403 caused by a token that predates a scope this call needs.

    Raised by `create_secondary_calendar` when a stored refresh token was
    granted before the `calendar.app.created` scope existed for this tenant
    (any clinic that connected before this feature shipped). Deliberately
    DISTINCT from `CalendarUnavailableError`: the fix here is not "wait and
    retry" or "hand off to a human" — it is "the doctor must reconnect the
    clinic's Google account" (the OAuth consent screen re-issues a token with
    the new scope). See api/hub/professionals.py's mapping to the hub's
    `google_reconnect_required` error code.
    """


# Marker substrings Google has been observed to use for a scope-insufficient
# 403, checked case-insensitively against the HttpError's reason/details/raw
# content. Several are matched (rather than one exact shape) because the
# Calendar API's classic `errors[].reason` error body and the newer
# `status`/`message`-only body both occur in practice, and we cannot probe
# the real API from this dev/test environment to pin down which one a given
# Google backend revision returns — see docs/CHECKPOINT_google_calendar_modes.md.
_SCOPE_INSUFFICIENT_MARKERS = (
    "insufficientpermissions",
    "access_token_scope_insufficient",
    "insufficient authentication scopes",
)


def _raise_if_scope_insufficient(exc: HttpError) -> None:
    """Translate a scope-insufficient 403 into GoogleScopeInsufficientError.

    MUST be checked before `_raise_if_unavailable`: a plain 403 is normally
    treated as a generic outage (token revoked, access denied), but a 403
    caused specifically by a missing OAuth scope needs the more specific
    "reconnect" signal instead — see `create_secondary_calendar`. Returns
    normally (no raise) for anything that isn't a scope-insufficient 403, so
    the caller falls through to its existing `_raise_if_unavailable` handling.
    """
    status = getattr(getattr(exc, "resp", None), "status", None)
    if status != 403:
        return
    content = exc.content
    content_text = (
        content.decode("utf-8", errors="ignore") if isinstance(content, bytes) else str(content)
    )
    haystack = f"{exc.reason} {exc.error_details} {content_text}".lower()
    if any(marker in haystack for marker in _SCOPE_INSUFFICIENT_MARKERS):
        raise GoogleScopeInsufficientError(
            "Google rejected the request: insufficient OAuth scope"
        ) from exc


class CalendarService:
    """Async wrapper around the (sync) Google Calendar v3 API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._tz = ZoneInfo(self._settings.CLINIC_TIMEZONE)
        self._calendar_id = self._settings.GOOGLE_CALENDAR_ID
        self._refresh_token_override: str | None = None
        self._service: Any | None = None
        # Per-tenant availability config. Empty business_hours => the legacy
        # fixed 08-18 window is used by list_free_slots (dev/Fase A fallback).
        self._business_hours: dict = {}
        self._default_slot_minutes: int = 30

    @classmethod
    def from_tenant_config(cls, config: "TenantRuntimeConfig") -> "CalendarService":
        """Build a CalendarService using per-tenant credentials.

        The OAuth app (client_id / client_secret) belongs to the platform and
        is read from settings. Only the refresh_token and calendar_id come from
        the tenant row.
        """
        instance = cls()
        instance._tz = ZoneInfo(config.timezone)
        instance._calendar_id = config.google_calendar_id
        instance._refresh_token_override = config.google_refresh_token
        instance._business_hours = config.business_hours or {}
        instance._default_slot_minutes = config.appointment_duration_min or 30
        return instance

    @classmethod
    def for_professional(
        cls,
        tenant_config: "TenantRuntimeConfig",
        *,
        google_calendar_id: str | None = None,
        google_refresh_token: str | None = None,
        business_hours: dict | None = None,
        appointment_duration_min: int | None = None,
    ) -> "CalendarService":
        """CalendarService for professional X under tenant config Y (contract v1 §10).

        Every keyword arg is an ALREADY-RESOLVED override (professional's own
        value, or None to keep the tenant's) — this performs pure
        substitution onto `tenant_config`, no fallback/decision logic of its
        own. The professional -> tenant -> env resolution chain lives in the
        caller: `services/tenant_config.py`'s per-professional helpers
        resolve the JSON config (business_hours/appointment_types), and its
        `get_professional_google_refresh_token` resolves the credential; the
        env fallback (`GOOGLE_REFRESH_TOKEN`) is still handled by
        `_build_service` itself when the resolved token is falsy, exactly
        like `from_tenant_config`.

        `google_calendar_id`/`google_refresh_token` falsy -> keep the
        tenant's own value.

        `business_hours` is the one argument where `None` and `{}` mean
        DIFFERENT things, and conflating them was a real bug:
          - `None` -> "no opinion", keep the tenant's own hours. That is what
            the caller passes when there is no tenant row to resolve against.
          - `{}`   -> the professional resolved to genuinely NO hours (their own
            override is empty, or closes every weekday). Honoured verbatim:
            substituting the clinic's hours here would resurrect exactly the
            legacy config the clinic deliberately emptied, on the
            patient-facing slot path. See the NULL-versus-EMPTY note in
            services/tenant_config.py.
        """
        resolved = replace(
            tenant_config,
            google_calendar_id=google_calendar_id or tenant_config.google_calendar_id,
            google_refresh_token=google_refresh_token or tenant_config.google_refresh_token,
            business_hours=(
                tenant_config.business_hours if business_hours is None else business_hours
            ),
            appointment_duration_min=(
                appointment_duration_min or tenant_config.appointment_duration_min
            ),
        )
        return cls.from_tenant_config(resolved)

    def _build_service(self) -> Any:
        s = self._settings
        refresh_token = self._refresh_token_override or s.GOOGLE_REFRESH_TOKEN
        if not (s.GOOGLE_CLIENT_ID and s.GOOGLE_CLIENT_SECRET and refresh_token):
            raise RuntimeError(
                "Google Calendar credentials missing. Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN in .env first."
            )
        creds = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri=TOKEN_URI,
            client_id=s.GOOGLE_CLIENT_ID,
            client_secret=s.GOOGLE_CLIENT_SECRET,
            scopes=SCOPES,
        )
        # Force a refresh now so a bad/expired refresh_token fails here rather
        # than on the first real API call. A revoked/expired token raises
        # RefreshError; a network blip raises a transport error. Both mean the
        # calendar is unavailable, not that the caller did something wrong.
        try:
            creds.refresh(GoogleRequest())
        except RefreshError as exc:
            logger.error("calendar_token_refresh_failed", error=str(exc))
            raise CalendarUnavailableError("Google Calendar refresh token rejected") from exc
        except _NETWORK_ERRORS as exc:
            logger.error("calendar_token_refresh_network_error", error=str(exc))
            raise CalendarUnavailableError("Google Calendar unreachable during auth") from exc
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def _client(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    @property
    def tzinfo(self) -> ZoneInfo:
        """The clinic-local timezone this service interprets naive times in."""
        return self._tz

    def _ensure_tz(self, dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=self._tz)

    async def check_availability(
        self, start: datetime, end: datetime, max_results: int = DEFAULT_MAX_EVENTS
    ) -> list[dict]:
        """Return events overlapping [start, end) on the clinic calendar.

        Each item: {"id", "summary", "start", "end"} with RFC3339 strings.
        Empty list = window is free.

        Why events.list instead of freebusy.query: the calendar.events OAuth
        scope grants the events.* methods but NOT freebusy.query (which needs
        calendar.readonly / calendar / calendar.events.freebusy). Sticking to
        events.list keeps the grant narrow AND surfaces real summaries to the
        LLM, which it can quote back to the patient.

        `max_results` sizes the single events.list page. The default suits a
        one-day question; a multi-day window must raise it (see
        `list_available_days`) or the busy list silently truncates and free
        time gets reported where there is none.
        """
        start_iso = self._ensure_tz(start).isoformat()
        end_iso = self._ensure_tz(end).isoformat()
        calendar_id = self._calendar_id

        def _list() -> list[dict]:
            try:
                resp = (
                    self._client()
                    .events()
                    .list(
                        calendarId=calendar_id,
                        timeMin=start_iso,
                        timeMax=end_iso,
                        singleEvents=True,
                        orderBy="startTime",
                        maxResults=max_results,
                    )
                    .execute()
                )
            except HttpError as exc:
                logger.error("calendar_events_list_http_error", error=str(exc))
                _raise_if_unavailable(exc)
                raise
            except _NETWORK_ERRORS as exc:
                logger.error("calendar_events_list_network_error", error=str(exc))
                raise CalendarUnavailableError("Google Calendar unreachable") from exc
            out: list[dict] = []
            for ev in resp.get("items", []):
                # transparency=="transparent" means the owner marked the event
                # as "free" - does not block time, do not report as conflict.
                if ev.get("transparency") == "transparent":
                    continue
                # All-day events expose `date` only; skip them so personal
                # all-day reminders (birthdays etc.) on a primary calendar
                # don't block clinic-hour scheduling.
                ev_start = ev.get("start", {}).get("dateTime")
                ev_end = ev.get("end", {}).get("dateTime")
                if not (ev_start and ev_end):
                    continue
                out.append(
                    {
                        "id": ev.get("id"),
                        "summary": ev.get("summary"),
                        "start": ev_start,
                        "end": ev_end,
                    }
                )
            return out

        return await asyncio.to_thread(_list)

    def _windows_for_day(
        self, day_local: datetime, open_hour: int, close_hour: int
    ) -> list[tuple[datetime, datetime]]:
        """Resolve the availability windows for `day_local` (clinic-local).

        Uses the tenant's per-weekday business_hours when configured; otherwise
        falls back to a single [open_hour, close_hour) window (Fase A / dev).
        Returns an empty list for a closed day.
        """
        weekday_key = _WEEKDAY_KEYS[day_local.weekday()]
        configured = self._business_hours.get(weekday_key) if self._business_hours else None
        windows: list[tuple[datetime, datetime]] = []
        if configured:
            for w in configured:
                try:
                    sh, sm = (int(x) for x in str(w["start"]).split(":"))
                    eh, em = (int(x) for x in str(w["end"]).split(":"))
                except (KeyError, ValueError, TypeError):
                    continue
                start = day_local.replace(hour=sh, minute=sm, second=0, microsecond=0)
                end = day_local.replace(hour=eh, minute=em, second=0, microsecond=0)
                if start < end:
                    windows.append((start, end))
        elif self._business_hours:
            # business_hours is configured but this weekday has no windows ->
            # the clinic is closed that day. Do not fall back to 08-18.
            return []
        else:
            start = day_local.replace(hour=open_hour, minute=0, second=0, microsecond=0)
            end = day_local.replace(hour=close_hour, minute=0, second=0, microsecond=0)
            if start < end:
                windows.append((start, end))
        return sorted(windows)

    def _busy_ranges(self, events: list[dict]) -> list[tuple[datetime, datetime]]:
        """Turn `check_availability` items into clinic-local [start, end) pairs."""
        ranges: list[tuple[datetime, datetime]] = []
        for ev in events:
            try:
                bs = datetime.fromisoformat(ev["start"])
                be = datetime.fromisoformat(ev["end"])
            except (KeyError, ValueError):
                continue
            ranges.append((self._ensure_tz(bs), self._ensure_tz(be)))
        return ranges

    def _walk_free_slots(
        self,
        windows: list[tuple[datetime, datetime]],
        busy_ranges: list[tuple[datetime, datetime]],
        delta: timedelta,
        now: datetime,
        max_slots: int,
    ) -> list[dict]:
        """Free slots inside `windows`, given an already-fetched busy list.

        Pure (no I/O) so the same walk answers both "which times are free on
        this day?" (`list_free_slots`) and "does this day have ANY free time?"
        (`list_available_days`, with max_slots=1) off ONE calendar read.
        """
        slots: list[dict] = []
        for window_start, window_end in windows:
            cursor = window_start
            while cursor + delta <= window_end and len(slots) < max_slots:
                slot_end = cursor + delta
                if cursor < now:
                    cursor = slot_end
                    continue
                overlaps = any(bs < slot_end and be > cursor for bs, be in busy_ranges)
                if not overlaps:
                    slots.append(
                        {
                            # Naive ISO (no tz) so it round-trips through the LLM
                            # exactly like check_availability / create_event inputs.
                            "start": cursor.replace(tzinfo=None).isoformat(timespec="minutes"),
                            "end": slot_end.replace(tzinfo=None).isoformat(timespec="minutes"),
                            "label": cursor.strftime("%H:%M"),
                        }
                    )
                cursor = slot_end
            if len(slots) >= max_slots:
                break
        return slots

    async def list_free_slots(
        self,
        day: datetime,
        slot_minutes: int | None = None,
        open_hour: int = 8,
        close_hour: int = 18,
        max_slots: int = 6,
    ) -> list[dict]:
        """Return free [start, end) slots on `day` within business hours.

        Walks each availability window for the weekday (per-tenant
        business_hours when set, else a fixed 08-18 fallback) in `slot_minutes`
        increments and keeps slots that do not overlap with any busy event on
        the calendar. Returns up to `max_slots` slots so the UI list message
        stays under WhatsApp's 10-row cap. `slot_minutes` defaults to the
        tenant's configured appointment duration.
        """
        day_local = self._ensure_tz(day)
        windows = self._windows_for_day(day_local, open_hour, close_hour)
        if not windows:
            return []

        busy = await self.check_availability(windows[0][0], windows[-1][1])
        return self._walk_free_slots(
            windows,
            self._busy_ranges(busy),
            timedelta(minutes=slot_minutes or self._default_slot_minutes),
            # Never offer a slot that has already started (matters for "hoje").
            datetime.now(self._tz),
            max_slots,
        )

    async def list_available_days(
        self,
        start_day: datetime,
        days: int,
        slot_minutes: int | None = None,
        open_hour: int = 8,
        close_hour: int = 18,
    ) -> list[datetime]:
        """Days in [start_day, start_day + `days`) with at least one free slot.

        Returns clinic-local midnight datetimes, in chronological order, ready
        to hand straight back to `list_free_slots(day=...)`. Days the clinic is
        closed on, and days whose every slot is taken or already past, are
        absent — which is exactly what makes a tappable day list honest.

        Costs ONE events.list call for the WHOLE window (`DAY_SCAN_MAX_EVENTS`
        page size), never one per day: the per-day walk afterwards is pure
        arithmetic on that single busy list. A day-at-a-time implementation
        would turn one patient turn into ~20 Google round-trips.
        """
        delta = timedelta(minutes=slot_minutes or self._default_slot_minutes)
        first = self._ensure_tz(start_day).replace(hour=0, minute=0, second=0, microsecond=0)
        day_windows: list[tuple[datetime, list[tuple[datetime, datetime]]]] = []
        for offset in range(max(days, 0)):
            day_local = first + timedelta(days=offset)
            windows = self._windows_for_day(day_local, open_hour, close_hour)
            if windows:
                day_windows.append((day_local, windows))
        if not day_windows:
            return []

        busy = await self.check_availability(
            day_windows[0][1][0][0],
            day_windows[-1][1][-1][1],
            max_results=DAY_SCAN_MAX_EVENTS,
        )
        busy_ranges = self._busy_ranges(busy)
        now = datetime.now(self._tz)
        return [
            day_local
            for day_local, windows in day_windows
            if self._walk_free_slots(windows, busy_ranges, delta, now, max_slots=1)
        ]

    async def create_event(
        self,
        start: datetime,
        end: datetime,
        summary: str,
        description: str = "",
    ) -> dict:
        """Create an event on the clinic's calendar. Returns the inserted event."""
        start_dt = self._ensure_tz(start)
        end_dt = self._ensure_tz(end)
        tz_name = str(self._tz)
        body = {
            "summary": summary,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
        }
        calendar_id = self._calendar_id

        def _insert() -> dict:
            try:
                return (
                    self._client()
                    .events()
                    .insert(calendarId=calendar_id, body=body)
                    .execute()
                )
            except HttpError as exc:
                logger.error("calendar_insert_http_error", error=str(exc))
                _raise_if_unavailable(exc)
                raise
            except _NETWORK_ERRORS as exc:
                logger.error("calendar_insert_network_error", error=str(exc))
                raise CalendarUnavailableError("Google Calendar unreachable") from exc

        event = await asyncio.to_thread(_insert)
        logger.info("calendar_event_created", event_id=event.get("id"), summary=summary)
        return event

    async def update_event(
        self,
        event_id: str,
        start: datetime,
        end: datetime,
    ) -> dict:
        """Move an existing event to a new [start, end) window. Returns the updated event."""
        start_dt = self._ensure_tz(start)
        end_dt = self._ensure_tz(end)
        tz_name = str(self._tz)
        body = {
            "start": {"dateTime": start_dt.isoformat(), "timeZone": tz_name},
            "end": {"dateTime": end_dt.isoformat(), "timeZone": tz_name},
        }
        calendar_id = self._calendar_id

        def _patch() -> dict:
            try:
                return (
                    self._client()
                    .events()
                    .patch(calendarId=calendar_id, eventId=event_id, body=body)
                    .execute()
                )
            except HttpError as exc:
                logger.error("calendar_patch_http_error", error=str(exc))
                _raise_if_unavailable(exc)
                raise
            except _NETWORK_ERRORS as exc:
                logger.error("calendar_patch_network_error", error=str(exc))
                raise CalendarUnavailableError("Google Calendar unreachable") from exc

        event = await asyncio.to_thread(_patch)
        logger.info("calendar_event_updated", event_id=event_id)
        return event

    async def cancel_event(self, event_id: str) -> None:
        """Delete an event by id. 404/410 are treated as success (idempotent)."""
        calendar_id = self._calendar_id

        def _delete() -> None:
            try:
                self._client().events().delete(
                    calendarId=calendar_id, eventId=event_id
                ).execute()
            except HttpError as exc:
                if exc.resp.status in (404, 410):
                    logger.info("calendar_event_already_gone", event_id=event_id)
                    return
                logger.error("calendar_delete_http_error", error=str(exc))
                _raise_if_unavailable(exc)
                raise
            except _NETWORK_ERRORS as exc:
                logger.error("calendar_delete_network_error", error=str(exc))
                raise CalendarUnavailableError("Google Calendar unreachable") from exc

        await asyncio.to_thread(_delete)
        logger.info("calendar_event_cancelled", event_id=event_id)

    async def create_secondary_calendar(self, summary: str) -> dict:
        """Create a NEW secondary Google Calendar via `calendars.insert`.

        shared_account mode (docs/CHECKPOINT_google_calendar_modes.md): the
        caller builds this CalendarService with the CLINIC's own credentials
        (`services/tenant_config.py::ensure_professional_secondary_calendar`)
        so the new calendar is created inside the clinic's connected Google
        account, where it shows up in that account's calendar sidebar. This
        method itself does no tenant/professional resolution or persistence —
        it is a pure Google API call, same layering as every other method
        here; the orchestration (idempotency check, persisting the returned
        id onto `Professional.google_calendar_id`) lives in
        `services/tenant_config.py`.

        Returns the inserted calendar resource (its `"id"` is the calendarId
        to persist). Requires the `calendar.app.created` OAuth scope — a
        token that predates it raises `GoogleScopeInsufficientError` (checked
        BEFORE the generic outage mapping), never `CalendarUnavailableError`
        nor an unhandled 500.
        """
        body = {"summary": summary, "timeZone": str(self._tz)}

        def _insert() -> dict:
            try:
                return self._client().calendars().insert(body=body).execute()
            except HttpError as exc:
                logger.error("calendar_secondary_insert_http_error", error=str(exc))
                _raise_if_scope_insufficient(exc)
                _raise_if_unavailable(exc)
                raise
            except _NETWORK_ERRORS as exc:
                logger.error("calendar_secondary_insert_network_error", error=str(exc))
                raise CalendarUnavailableError("Google Calendar unreachable") from exc

        calendar = await asyncio.to_thread(_insert)
        logger.info("calendar_secondary_created", calendar_id=calendar.get("id"), summary=summary)
        return calendar
