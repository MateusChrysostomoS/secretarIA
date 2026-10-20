"""CalendarService.list_events pages the hub agenda; check_availability stays one page.

The hub's GET /tenants/me/calendar/events used `check_availability`, a single
events.list page of 50: a busy month silently lost every event after the 50th.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import datetime, timedelta  # noqa: E402

from secretaria.services import calendar as calendar_module  # noqa: E402
from secretaria.services.calendar import (  # noqa: E402
    DEFAULT_MAX_EVENTS,
    HUB_LIST_PAGE_SIZE,
    CalendarService,
)


def _event(n: int, **extra) -> dict:
    start = datetime(2026, 11, 2, 8, 0) + timedelta(minutes=10 * n)
    return {
        "id": f"evt-{n}",
        "summary": f"Consulta - Paciente {n}",
        "start": {"dateTime": start.isoformat() + "-03:00"},
        "end": {"dateTime": (start + timedelta(minutes=10)).isoformat() + "-03:00"},
        **extra,
    }


class _Request:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _PagedGoogle:
    """events.list over `events`, cut into pages of `maxResults`, chained by pageToken."""

    def __init__(self, events: list[dict], *, endless: bool = False) -> None:
        self.events = events
        self.endless = endless
        self.calls: list[dict] = []

    def events_api(self):
        google = self

        class _Events:
            def list(self, **kwargs):
                google.calls.append(kwargs)
                offset = int(kwargs.get("pageToken") or 0)
                size = kwargs["maxResults"]
                page = google.events[offset : offset + size]
                body: dict = {"items": page}
                if google.endless or offset + size < len(google.events):
                    body["nextPageToken"] = str(offset + size)
                return _Request(body)

        return _Events()


def _service(google: _PagedGoogle) -> CalendarService:
    service = CalendarService()
    service._calendar_id = "cal-1"
    client = type("Client", (), {"events": lambda _self: google.events_api()})()
    service._client = lambda: client  # type: ignore[method-assign]
    return service


WINDOW = (datetime(2026, 11, 1), datetime(2026, 12, 6))


async def test_list_events_reads_every_page_of_a_busy_month():
    google = _PagedGoogle([_event(n) for n in range(600)])
    events = await _service(google).list_events(*WINDOW)

    assert [e["id"] for e in events] == [f"evt-{n}" for n in range(600)]
    assert len(google.calls) == 3  # 250 + 250 + 100
    assert all(call["maxResults"] == HUB_LIST_PAGE_SIZE for call in google.calls)
    assert "pageToken" not in google.calls[0]
    assert [call.get("pageToken") for call in google.calls[1:]] == ["250", "500"]
    # Same query as availability: expanded recurrences, chronological.
    assert all(
        call["singleEvents"] is True and call["orderBy"] == "startTime" for call in google.calls
    )


async def test_list_events_keeps_the_availability_filtering():
    google = _PagedGoogle(
        [
            _event(0),
            _event(1, transparency="transparent"),
            {"id": "all-day", "start": {"date": "2026-11-02"}, "end": {"date": "2026-11-03"}},
            _event(3),
        ]
    )
    events = await _service(google).list_events(*WINDOW)

    assert [e["id"] for e in events] == ["evt-0", "evt-3"]
    assert set(events[0]) == {"id", "summary", "start", "end"}


async def test_list_events_stops_at_the_cap_and_says_so(monkeypatch):
    warnings: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        calendar_module.logger, "warning", lambda event, **kw: warnings.append((event, kw))
    )
    google = _PagedGoogle([_event(n) for n in range(700)])
    events = await _service(google).list_events(*WINDOW, max_events=300)

    assert len(events) == 300
    assert [call["maxResults"] for call in google.calls] == [250, 50]
    assert warnings == [("calendar_events_list_truncated", {"events": 300, "max_events": 300})]


async def test_list_events_never_follows_an_endless_token_chain():
    google = _PagedGoogle([], endless=True)
    events = await _service(google).list_events(*WINDOW)

    assert events == []
    assert len(google.calls) == calendar_module._HUB_LIST_MAX_PAGES


async def test_check_availability_is_still_one_page_of_its_own_size():
    google = _PagedGoogle([_event(n) for n in range(600)])
    busy = await _service(google).check_availability(*WINDOW)

    assert len(busy) == DEFAULT_MAX_EVENTS
    assert len(google.calls) == 1
    assert google.calls[0]["maxResults"] == DEFAULT_MAX_EVENTS
    assert "pageToken" not in google.calls[0]
