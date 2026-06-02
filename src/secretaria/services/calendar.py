"""Google Calendar integration for the clinic.

Fase A (single tenant): credentials come from the environment
(GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET / GOOGLE_REFRESH_TOKEN, scope
calendar.events). Multi-tenant (Phase 5): the refresh token moves to an
encrypted column on `tenants` and `CalendarService` takes a `Tenant` row.

The googleapiclient library is blocking, so every public method offloads to
a worker thread with asyncio.to_thread to keep the event loop responsive.
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from secretaria.config import Settings, get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


class CalendarService:
    """Async wrapper around the (sync) Google Calendar v3 API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._tz = ZoneInfo(self._settings.CLINIC_TIMEZONE)
        self._calendar_id = self._settings.GOOGLE_CALENDAR_ID
        self._service: Any | None = None

    def _build_service(self) -> Any:
        s = self._settings
        if not (s.GOOGLE_CLIENT_ID and s.GOOGLE_CLIENT_SECRET and s.GOOGLE_REFRESH_TOKEN):
            raise RuntimeError(
                "Google Calendar credentials missing. Set GOOGLE_CLIENT_ID, "
                "GOOGLE_CLIENT_SECRET and GOOGLE_REFRESH_TOKEN in .env first."
            )
        creds = Credentials(
            token=None,
            refresh_token=s.GOOGLE_REFRESH_TOKEN,
            token_uri=TOKEN_URI,
            client_id=s.GOOGLE_CLIENT_ID,
            client_secret=s.GOOGLE_CLIENT_SECRET,
            scopes=SCOPES,
        )
        # Force a refresh now so a bad/expired refresh_token fails here rather
        # than on the first real API call.
        creds.refresh(GoogleRequest())
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    def _client(self) -> Any:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _ensure_tz(self, dt: datetime) -> datetime:
        return dt if dt.tzinfo else dt.replace(tzinfo=self._tz)

    async def check_availability(self, start: datetime, end: datetime) -> list[dict]:
        """Return events overlapping [start, end) on the clinic calendar.

        Each item: {"id", "summary", "start", "end"} with RFC3339 strings.
        Empty list = window is free.

        Why events.list instead of freebusy.query: the calendar.events OAuth
        scope grants the events.* methods but NOT freebusy.query (which needs
        calendar.readonly / calendar / calendar.events.freebusy). Sticking to
        events.list keeps the grant narrow AND surfaces real summaries to the
        LLM, which it can quote back to the patient.
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
                        maxResults=50,
                    )
                    .execute()
                )
            except HttpError as exc:
                logger.error("calendar_events_list_http_error", error=str(exc))
                raise
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

    async def list_free_slots(
        self,
        day: datetime,
        slot_minutes: int = 30,
        open_hour: int = 8,
        close_hour: int = 18,
        max_slots: int = 6,
    ) -> list[dict]:
        """Return free [start, end) slots on `day` within business hours.

        Walks the day in `slot_minutes` increments and keeps slots that do
        not overlap with any busy event already on the calendar. Returns up
        to `max_slots` slots so the UI list message stays under WhatsApp's
        10-row cap and the patient is not buried in options.
        """
        day_local = self._ensure_tz(day)
        start_of_day = day_local.replace(
            hour=open_hour, minute=0, second=0, microsecond=0
        )
        end_of_day = day_local.replace(
            hour=close_hour, minute=0, second=0, microsecond=0
        )
        if start_of_day >= end_of_day:
            return []

        busy = await self.check_availability(start_of_day, end_of_day)
        busy_ranges: list[tuple[datetime, datetime]] = []
        for ev in busy:
            try:
                bs = datetime.fromisoformat(ev["start"])
                be = datetime.fromisoformat(ev["end"])
            except (KeyError, ValueError):
                continue
            busy_ranges.append((self._ensure_tz(bs), self._ensure_tz(be)))

        delta = timedelta(minutes=slot_minutes)
        slots: list[dict] = []
        cursor = start_of_day
        while cursor + delta <= end_of_day and len(slots) < max_slots:
            slot_end = cursor + delta
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
        return slots

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
                raise

        event = await asyncio.to_thread(_insert)
        logger.info("calendar_event_created", event_id=event.get("id"), summary=summary)
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
                raise

        await asyncio.to_thread(_delete)
        logger.info("calendar_event_cancelled", event_id=event_id)
