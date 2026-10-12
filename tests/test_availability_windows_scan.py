"""The scan behind get_availability: windows from the real free-time code (TASK-030 P4).

`scan_free_windows` is built only from services/availability.py; here it runs over a fake
calendar (limits, holds, read counts) and over the REAL CalendarService behind a fake Google
client (the pinned clock, the DST days, and the canary that proves no event field survives).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import json  # noqa: E402
from datetime import date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import (  # noqa: E402
    availability_windows as aw,
    calendar as calendar_module,
)
from secretaria.services.calendar import CalendarService, CalendarUnavailableError  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 10, 5)  # a fixed anchor: no test here reads the wall clock

# --------------------------------------------------------------------------
# scan_free_windows: built from services/availability.py, holds subtracted
# --------------------------------------------------------------------------


class _Calendar:
    """`free` maps a day to its free slot STARTS ("HH:MM"). Counts every read."""

    default_slot_minutes = 30

    def __init__(self, free: dict[date, list[str]], *, down: bool = False):
        self.tzinfo = TZ
        self._free = free
        self._down = down
        self.day_scans = 0
        self.slot_reads: list[date] = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        if self._down:
            raise CalendarUnavailableError("down")
        self.day_scans += 1
        first = start_day.date()
        wanted = {first + timedelta(days=i) for i in range(days)}
        return [
            datetime(d.year, d.month, d.day, tzinfo=TZ)
            for d in sorted(self._free)
            if d in wanted and self._free[d]
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        if self._down:
            raise CalendarUnavailableError("down")
        self.slot_reads.append(day.date())
        return [
            {"start": f"{day.date().isoformat()}T{hhmm}", "end": "", "label": hhmm}
            for hhmm in self._free.get(day.date(), [])[:max_slots]
        ]


def _span(first="2026-10-05", last="2026-10-18"):
    return aw.DayRange(date.fromisoformat(first), date.fromisoformat(last))


async def test_the_scan_returns_windows_earliest_first():
    cal = _Calendar(
        {
            date(2026, 10, 7): ["14:00", "14:30"],
            date(2026, 10, 6): ["08:00", "08:30", "09:00", "15:00"],
        }
    )
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert [
        (w.payload()["day"], w.payload()["start"], w.payload()["end"]) for w in scan.windows
    ] == [
        ("2026-10-06", "08:00", "09:30"),
        ("2026-10-06", "15:00", "15:30"),
        ("2026-10-07", "14:00", "15:00"),
    ]
    assert scan.truncated is False


async def test_a_held_slot_is_not_part_of_any_window():
    cal = _Calendar({date(2026, 10, 6): ["08:00", "08:30", "09:00"]})
    held = (datetime(2026, 10, 6, 8, 30, tzinfo=TZ), datetime(2026, 10, 6, 9, 0, tzinfo=TZ))
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30, holds=[held])
    assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in scan.windows] == [
        ("08:00", "08:30"),
        ("09:00", "09:30"),
    ]


async def test_an_agenda_with_nothing_free_costs_one_calendar_read():
    cal = _Calendar({})
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert (scan.windows, scan.truncated) == ([], False)
    assert (cal.day_scans, cal.slot_reads) == (1, [])


async def test_days_outside_the_span_are_ignored():
    cal = _Calendar({date(2026, 10, 4): ["08:00"], date(2026, 10, 20): ["08:00"]})
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert scan.windows == []


def _many_windows(days: int, per_day: int) -> dict[date, list[str]]:
    """`per_day` separate windows a day: one free 30-minute slot every two hours from 08:00."""
    one_day = [f"{8 + 2 * i:02d}:00" for i in range(per_day)]
    return {TODAY + timedelta(days=i): list(one_day) for i in range(days)}


async def test_more_than_30_windows_are_cut_at_30_and_reported():
    cal = _Calendar(_many_windows(days=14, per_day=3))  # 42 windows exist
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert len(scan.windows) == aw.MAX_WINDOWS == 30
    assert scan.truncated is True
    # Earliest first; ten days fill the cap, an eleventh read proves there is more, and the
    # scan stops there instead of reading the other three days.
    assert scan.windows[-1].day == TODAY + timedelta(days=9)
    assert len(cal.slot_reads) == 11


async def test_exactly_30_windows_is_not_a_truncation():
    cal = _Calendar(_many_windows(days=10, per_day=3))  # 30 windows exist
    scan = await aw.scan_free_windows(cal, span=_span(), duration_minutes=30)
    assert (len(scan.windows), scan.truncated) == (30, False)


async def test_a_calendar_outage_propagates_untouched():
    with pytest.raises(CalendarUnavailableError):
        await aw.scan_free_windows(_Calendar({}, down=True), span=_span(), duration_minutes=30)


# --------------------------------------------------------------------------
# The REAL CalendarService behind a fake Google client (no network, a pinned clock)
# --------------------------------------------------------------------------


class _GoogleEvents:
    """events.list replays `items`, shaped like Google's own payload (attendees and all)."""

    def __init__(self, items):
        self._items = items

    def list(self, **_kwargs):
        items = self._items

        class _Request:
            def execute(self):
                return {"items": items}

        return _Request()


class _GoogleService:
    def __init__(self, items):
        self._events = _GoogleEvents(items)

    def events(self):
        return self._events


def _real_calendar(monkeypatch, *, tz_name, hours, minutes=60, items=(), now_utc=None):
    """A real CalendarService whose clock is `now_utc` and whose Google client is a stub."""
    if now_utc is not None:

        class _Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return now_utc.astimezone(tz) if tz else now_utc.replace(tzinfo=None)

        monkeypatch.setattr(calendar_module, "datetime", _Clock)
    settings = SimpleNamespace(
        CLINIC_TIMEZONE=tz_name,
        GOOGLE_CALENDAR_ID="primary",
        GOOGLE_CLIENT_ID="id",
        GOOGLE_CLIENT_SECRET="secret",
        GOOGLE_REFRESH_TOKEN="token",
    )
    service = CalendarService(settings=settings)
    service._business_hours = hours
    service._default_slot_minutes = minutes
    monkeypatch.setattr(service, "_service", _GoogleService(list(items)))
    return service


_ALL_WEEK = {
    day: [{"start": "08:00", "end": "12:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


async def _hhmm(calendar, span, minutes):
    scan = await aw.scan_free_windows(calendar, span=span, duration_minutes=minutes)
    return [
        (w.day.isoformat(), w.start.strftime("%H:%M"), w.end.strftime("%H:%M"))
        for w in scan.windows
    ]


async def test_a_range_that_starts_today_drops_the_hours_that_already_passed(monkeypatch):
    # 17:10 UTC is 14:10 in São Paulo: the 14:00 slot has started, the 15:00 one has not.
    calendar = _real_calendar(
        monkeypatch,
        tz_name="America/Sao_Paulo",
        hours={
            "monday": [{"start": "08:00", "end": "18:00"}],
            "tuesday": [{"start": "08:00", "end": "18:00"}],
        },
        now_utc=datetime(2026, 10, 5, 17, 10, tzinfo=ZoneInfo("UTC")),
    )
    span = aw.DayRange(date(2026, 10, 5), date(2026, 10, 6))
    assert await _hhmm(calendar, span, 60) == [
        ("2026-10-05", "15:00", "18:00"),
        ("2026-10-06", "08:00", "18:00"),
    ]


@pytest.mark.parametrize(
    "now_utc, first, last",
    [
        # US spring forward (2037-03-08) and fall back (2037-11-01), New York.
        (datetime(2037, 3, 5, 15, 0, tzinfo=ZoneInfo("UTC")), date(2037, 3, 7), date(2037, 3, 9)),
        (
            datetime(2037, 10, 29, 15, 0, tzinfo=ZoneInfo("UTC")),
            date(2037, 10, 31),
            date(2037, 11, 2),
        ),
    ],
)
async def test_a_dst_transition_day_reads_like_its_neighbours(monkeypatch, now_utc, first, last):
    """08:00-12:00 is 08:00-12:00 on all three days, though the UTC offset changes on one."""
    calendar = _real_calendar(
        monkeypatch, tz_name="America/New_York", hours=_ALL_WEEK, now_utc=now_utc
    )
    windows = await _hhmm(calendar, aw.DayRange(first, last), 60)
    assert [(start, end) for _day, start, end in windows] == [("08:00", "12:00")] * 3
    assert [day for day, _s, _e in windows] == [
        (first + timedelta(days=i)).isoformat() for i in range(3)
    ]


CANARY = "CANARY-7f3a91"


async def test_a_busy_event_removes_its_time_and_none_of_its_fields_survive(monkeypatch):
    event = {
        "id": f"evt-{CANARY}",
        "summary": f"Consulta - {CANARY} Silva",
        "description": f"Notas privadas {CANARY}",
        "htmlLink": f"https://calendar.example/{CANARY}",
        "attendees": [{"email": f"{CANARY}@example.com", "displayName": CANARY}],
        "start": {"dateTime": "2026-10-06T09:00:00-03:00"},
        "end": {"dateTime": "2026-10-06T10:00:00-03:00"},
    }
    calendar = _real_calendar(
        monkeypatch,
        tz_name="America/Sao_Paulo",
        hours={"tuesday": [{"start": "08:00", "end": "12:00"}]},
        items=[event],
        now_utc=datetime(2026, 10, 5, 12, 0, tzinfo=ZoneInfo("UTC")),
    )
    scan = await aw.scan_free_windows(
        calendar, span=aw.DayRange(date(2026, 10, 6), date(2026, 10, 6)), duration_minutes=60
    )
    assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in scan.windows] == [
        ("08:00", "09:00"),
        ("10:00", "12:00"),
    ]
    payload = json.dumps([w.payload() for w in scan.windows])
    assert CANARY not in payload
    assert set(scan.windows[0].payload()) == {"day", "start", "end"}


def test_the_calendar_exposes_the_slot_length_it_walks_by_default():
    config = TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="Clinica",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=40,
        appointment_types=[],
        business_hours={},
        google_calendar_id="cal",
        google_refresh_token=None,
    )
    assert CalendarService.from_tenant_config(config).default_slot_minutes == 40
