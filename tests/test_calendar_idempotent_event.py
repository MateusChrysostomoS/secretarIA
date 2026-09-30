"""CalendarService additions of TASK-017: an idempotent `create_event(event_id=...)` and the
`extra_busy` windows of the free-slot walks (spec sections 4.2 item 4 and 9).

No Google network: `CalendarService._client` is replaced by a scripted fake that records every
call, so what is under test is OUR handling of a 409, of a deleted event and of held windows.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from googleapiclient.errors import HttpError

from secretaria.services.calendar import CalendarService, CalendarUnavailableError

TZ = ZoneInfo("America/Sao_Paulo")


def _http_error(status: int) -> HttpError:
    return HttpError(resp=SimpleNamespace(status=status, reason="test"), content=b"{}")


class _Request:
    def __init__(self, outcome):
        self._outcome = outcome

    def execute(self):
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


class FakeGoogle:
    """`events().insert/get/list(...).execute()` with scripted outcomes."""

    def __init__(self, *, insert=None, get=None, listed=None):
        self.insert_outcome = insert
        self.get_outcome = get
        self.listed = listed or []
        self.calls: list[tuple[str, dict]] = []

    def events(self):
        return self

    def insert(self, **kwargs):
        self.calls.append(("insert", kwargs))
        return _Request(self.insert_outcome)

    def get(self, **kwargs):
        self.calls.append(("get", kwargs))
        return _Request(self.get_outcome)

    def list(self, **kwargs):
        self.calls.append(("list", kwargs))
        return _Request({"items": self.listed})


def _service(monkeypatch, google: FakeGoogle) -> CalendarService:
    settings = SimpleNamespace(
        CLINIC_TIMEZONE="America/Sao_Paulo",
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="client-id",
        GOOGLE_CLIENT_SECRET="client-secret",
        GOOGLE_REFRESH_TOKEN="refresh-token",
    )
    service = CalendarService(settings=settings)
    service._business_hours = {
        day: [{"start": "09:00", "end": "11:00"}]
        for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    }
    monkeypatch.setattr(service, "_client", lambda: google)
    return service


START = datetime(2030, 3, 4, 9, 0, tzinfo=TZ)  # a Monday far in the future
END = START + timedelta(minutes=30)


async def test_an_event_id_is_sent_to_google(monkeypatch):
    google = FakeGoogle(insert={"id": "abc12345", "status": "confirmed"})
    service = _service(monkeypatch, google)
    event = await service.create_event(START, END, "Consulta", event_id="abc12345")
    assert event["id"] == "abc12345"
    (_, sent), = google.calls
    assert sent["body"]["id"] == "abc12345"


async def test_without_an_event_id_the_body_has_none_and_a_409_still_raises(monkeypatch):
    google = FakeGoogle(insert=_http_error(409))
    service = _service(monkeypatch, google)
    with pytest.raises(HttpError):
        await service.create_event(START, END, "Consulta")
    assert "id" not in google.calls[0][1]["body"]
    assert [name for name, _ in google.calls] == ["insert"]  # never falls back to a get


async def test_a_409_on_our_own_id_is_success_and_returns_the_existing_event(monkeypatch):
    existing = {"id": "abc12345", "status": "confirmed", "htmlLink": "https://x"}
    google = FakeGoogle(insert=_http_error(409), get=existing)
    service = _service(monkeypatch, google)
    event = await service.create_event(START, END, "Consulta", event_id="abc12345")
    assert event == existing
    assert [name for name, _ in google.calls] == ["insert", "get"]
    assert google.calls[1][1]["eventId"] == "abc12345"


async def test_a_409_on_an_id_of_a_deleted_event_is_not_success(monkeypatch):
    google = FakeGoogle(insert=_http_error(409), get={"id": "abc12345", "status": "cancelled"})
    service = _service(monkeypatch, google)
    with pytest.raises(CalendarUnavailableError):
        await service.create_event(START, END, "Consulta", event_id="abc12345")


async def test_an_outage_with_an_event_id_is_still_an_outage(monkeypatch):
    google = FakeGoogle(insert=_http_error(503))
    service = _service(monkeypatch, google)
    with pytest.raises(CalendarUnavailableError):
        await service.create_event(START, END, "Consulta", event_id="abc12345")


async def test_held_windows_are_skipped_before_max_slots_is_applied(monkeypatch):
    google = FakeGoogle()
    service = _service(monkeypatch, google)
    # 09:00-10:00 is held; 30-minute slots: 09:00 and 09:30 are gone, 10:00 and 10:30 remain.
    held = [(START, START + timedelta(hours=1))]
    slots = await service.list_free_slots(START, slot_minutes=30, max_slots=2, extra_busy=held)
    assert [slot["label"] for slot in slots] == ["10:00", "10:30"]


async def test_without_extra_busy_the_walk_is_unchanged(monkeypatch):
    service = _service(monkeypatch, FakeGoogle())
    slots = await service.list_free_slots(START, slot_minutes=30, max_slots=4)
    assert [slot["label"] for slot in slots] == ["09:00", "09:30", "10:00", "10:30"]


async def test_a_day_fully_covered_by_holds_is_not_offered(monkeypatch):
    service = _service(monkeypatch, FakeGoogle())
    monday, tuesday = START, START + timedelta(days=1)
    whole_monday = [(monday, monday.replace(hour=11))]
    days = await service.list_available_days(
        monday.replace(hour=0), 2, slot_minutes=30, extra_busy=whole_monday
    )
    assert [d.date() for d in days] == [tuesday.date()]
