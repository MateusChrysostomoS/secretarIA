"""CalendarService.update_event_details / is_slot_free (TASK-032 R6, spec §5.6)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import datetime, timedelta  # noqa: E402

from secretaria.services.calendar import CalendarService  # noqa: E402


class _Executable:
    def __init__(self, value):
        self._value = value

    def execute(self):
        return self._value


class _Events:
    def __init__(self, calls):
        self._calls = calls

    def patch(self, calendarId, eventId, body):  # noqa: N803 - Google's keyword names
        self._calls.append((calendarId, eventId, body))
        return _Executable({"id": eventId})


class _Client:
    def __init__(self):
        self.calls: list[tuple] = []

    def events(self):
        return _Events(self.calls)


def _service(client=None) -> CalendarService:
    service = CalendarService()
    service._calendar_id = "cal-1"
    service._business_hours = {}  # fallback window: 08:00-18:00 every day
    if client is not None:
        service._client = lambda: client  # type: ignore[method-assign]
    return service


def _tomorrow_at(service, hour: int, minute: int = 0) -> datetime:
    day = datetime.now(service.tzinfo) + timedelta(days=2)
    return day.replace(hour=hour, minute=minute, second=0, microsecond=0)


async def test_update_event_details_patches_title_description_and_window():
    client = _Client()
    service = _service(client)
    start = _tomorrow_at(service, 10)
    end = start + timedelta(minutes=40)

    event = await service.update_event_details(
        "evt-1", start, end, "Retorno - Maria", "Serviço: Retorno\nConvênio: Unimed"
    )

    assert event == {"id": "evt-1"}
    [(calendar_id, event_id, body)] = client.calls
    assert (calendar_id, event_id) == ("cal-1", "evt-1")
    assert body["summary"] == "Retorno - Maria"
    assert body["description"] == "Serviço: Retorno\nConvênio: Unimed"
    assert body["start"]["dateTime"] == start.isoformat()
    assert body["end"]["dateTime"] == end.isoformat()


async def test_update_event_keeps_patching_only_the_window():
    client = _Client()
    service = _service(client)
    start = _tomorrow_at(service, 9)

    await service.update_event("evt-1", start, start + timedelta(minutes=30))

    [(_c, _e, body)] = client.calls
    assert set(body) == {"start", "end"}


async def _free(service, events, start, minutes=40, ignore=None) -> bool:
    async def _events(window_start, window_end, max_results=0):
        return events

    service.check_availability = _events  # type: ignore[method-assign]
    return await service.is_slot_free(
        start, start + timedelta(minutes=minutes), ignore_event_id=ignore
    )


async def test_a_free_future_slot_inside_the_hours_is_free():
    service = _service()
    assert await _free(service, [], _tomorrow_at(service, 10)) is True


async def test_outside_the_business_hours_or_in_the_past_is_not_free():
    service = _service()
    assert await _free(service, [], _tomorrow_at(service, 7)) is False
    assert await _free(service, [], _tomorrow_at(service, 17, 30)) is False  # ends after 18:00
    assert await _free(service, [], datetime.now(service.tzinfo) - timedelta(hours=1)) is False


async def test_a_busy_event_blocks_unless_it_is_the_one_being_edited():
    service = _service()
    start = _tomorrow_at(service, 10)
    busy = [
        {
            "id": "evt-mine",
            "start": (start - timedelta(minutes=10)).isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
        }
    ]
    assert await _free(service, busy, start) is False
    assert await _free(service, busy, start, ignore="evt-mine") is True
    other = [{**busy[0], "id": "evt-other"}]
    assert await _free(service, other, start, ignore="evt-mine") is False
