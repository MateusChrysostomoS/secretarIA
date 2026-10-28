"""The doctor's minimum notice for a NEW booking (Instituto Gicovate, 2026-10-27).

`Professional.min_booking_notice_hours` = N: a patient booking through the secretarIA is
never offered, nor allowed to confirm, a slot starting less than N hours from now.
Rescheduling and editing are not limited. 0 = no limit. NULL (not configured) = the
clinic-wide default, BOOKING_NOTICE_DEFAULT_HOURS (12h in production; the suite runs with
0, see tests/conftest.py, and the tests below that need it turn it on).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import (
    availability,  # noqa: E402
    flow_router as fr,  # noqa: E402
)
from tests.test_flow_router_insurance import _conversation, _tenant  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
# The day picker's 20-day window starts at the REAL today, so the agenda is anchored there:
# day 0 = today, 09:10 is the pinned "now" of the notice rule. Open days 0, 1 and 2,
# 08:00-12:00, 30-minute slots.
_TODAY = datetime.now(TZ).date()
NOW = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 9, 10, tzinfo=TZ)
OPEN_DAYS = [_TODAY + timedelta(days=n) for n in range(3)]
D0, D1, D2 = (f"day|{d.isoformat()}" for d in OPEN_DAYS)


class _Agenda:
    tzinfo = TZ

    def __init__(self):
        self.created: list = []

    async def list_available_days(self, start_day, days, slot_minutes=None):
        first = start_day.date()
        return [
            datetime(d.year, d.month, d.day, tzinfo=TZ)
            for d in OPEN_DAYS
            if first <= d < first + timedelta(days=days)
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        d = day.date()
        if d not in OPEN_DAYS:
            return []
        slots, t = [], datetime(d.year, d.month, d.day, 8, tzinfo=TZ)
        while t.hour < 12 and len(slots) < max_slots:
            slots.append({
                "start": t.isoformat(),
                "end": (t + timedelta(minutes=30)).isoformat(),
                "label": t.strftime("%H:%M"),
            })
            t += timedelta(minutes=30)
        return slots

    async def has_conflict(self, start, end, ignore_event_id=None):
        return False

    async def create_event(self, start, end, summary, description="", **_):
        self.created.append(start)
        return {"id": "evt", "htmlLink": "https://cal/evt",
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}


def _doctor(hours):
    return SimpleNamespace(
        id=uuid4(), name="Dr. Carlos", specialty=None, about=None,
        context_doctor_message=None, appointment_types=None, business_hours=None,
        min_booking_notice_hours=hours,
    )


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(fr, "_notice_now", lambda tz: NOW.astimezone(tz))


@pytest.fixture
def default_12h(monkeypatch):
    """The production default: 12h when the doctor never configured the notice."""
    monkeypatch.setattr(
        availability, "get_settings", lambda: SimpleNamespace(BOOKING_NOTICE_DEFAULT_HOURS=12)
    )


# --- the rule itself ----------------------------------------------------------------


def test_zero_is_an_explicit_no_limit_even_with_a_default(default_12h):
    assert availability.booking_notice_floor(_doctor(0), NOW) is None


@pytest.mark.parametrize("hours", [None, -5, True, "24"])
def test_not_configured_falls_back_to_the_12h_default(default_12h, hours):
    twelve = NOW + timedelta(hours=12)
    assert availability.booking_notice_floor(_doctor(hours), NOW) == twelve
    assert availability.booking_notice_floor(None, NOW) == twelve
    assert availability.booking_notice_floor(SimpleNamespace(), NOW) == twelve


def test_the_doctors_own_value_wins_over_the_default(default_12h):
    assert availability.booking_notice_floor(_doctor(24), NOW) == NOW + timedelta(hours=24)
    assert availability.booking_notice_floor(_doctor(2), NOW) == NOW + timedelta(hours=2)


def test_with_no_default_an_unconfigured_doctor_has_no_rule():
    assert availability.booking_notice_floor(_doctor(None), NOW) is None


def test_the_floor_is_now_plus_the_doctors_hours():
    assert availability.booking_notice_floor(_doctor(24), NOW) == NOW + timedelta(hours=24)


async def test_free_slots_before_the_floor_are_dropped():
    slots = await availability.free_slots_for_day(
        _Agenda(), day=OPEN_DAYS[1], duration_minutes=30,
        not_before=NOW + timedelta(hours=24),
    )
    assert [s.strftime("%H:%M") for s in slots] == ["09:30", "10:00", "10:30", "11:00", "11:30"]


# --- guided booking -------------------------------------------------------------------


async def _days(hours, branch=fr.BOOKING_DAY_BRANCH):
    result = await fr.enter_day_picker(
        _conversation(flow_step=fr.STEP_AWAITING_DAY), _tenant(), _Agenda(),
        duration_minutes=30, branch=branch, professionals=[_doctor(hours)],
    )
    # A day row's id is "day|<date>|<page>"; the page is not what is under test.
    return [
        row[0].rsplit("|", 1)[0] for row in result.bubbles[0].rows
        if row[0].startswith(fr.ROW_DAY_PREFIX)
    ]


async def test_without_a_rule_today_is_offered(default_12h):
    assert await _days(0) == [D0, D1, D2]


async def test_an_unconfigured_doctor_gets_the_12h_default(default_12h):
    # Now 09:10 -> floor 21:10 today: nothing left today (agenda closes at 12:00).
    assert await _days(None) == [D1, D2]


@pytest.mark.parametrize(
    ("hours", "expected"),
    [
        (24, [D1, D2]),  # floor day 1 09:10: day 1 still has 09:30+
        (26, [D1, D2]),  # floor day 1 11:10: 11:30 left
        (27, [D2]),      # floor day 1 12:10: nothing left on day 1
        (48, [D2]),
    ],
)
async def test_the_day_picker_starts_after_the_notice(hours, expected):
    assert await _days(hours) == expected


async def test_rescheduling_is_not_limited_by_the_notice():
    assert await _days(48, branch=fr.MANAGE_DAY_BRANCH) == [D0, D1, D2]


async def test_the_floor_days_slot_list_starts_after_the_notice():
    result = await fr._enter_slot_picker(
        _conversation(flow_step=fr.STEP_AWAITING_DAY), _tenant(), _Agenda(),
        datetime(OPEN_DAYS[1].year, OPEN_DAYS[1].month, OPEN_DAYS[1].day, tzinfo=TZ),
        duration_minutes=30,
        branch=fr.BOOKING_DAY_BRANCH, professionals=[_doctor(24)],
    )
    times = [row[1] for row in result.bubbles[-1].rows if row[0].startswith("slot|")]
    assert [t[-5:] for t in times] == ["09:30", "10:00", "10:30", "11:00", "11:30"]


async def _confirm(slot: str, hours):
    agenda = _Agenda()
    conversation = _conversation(
        flow_step=fr.STEP_AWAITING_CONFIRMATION, flow_selected_slot=slot,
        flow_selected_day=slot[:10],
    )
    result = await fr.route(conversation, _tenant(), agenda, fr.LABEL_CONFIRM,
                            professionals=[_doctor(hours)])
    return result, agenda


async def test_confirming_a_time_inside_the_notice_reopens_the_days_and_says_why():
    result, agenda = await _confirm(f"{OPEN_DAYS[1].isoformat()}T08:30:00", 24)
    assert agenda.created == []
    assert result.flow_step == fr.STEP_AWAITING_DAY
    text = " ".join(getattr(b, "body", "") or "" for b in result.bubbles)
    assert fr.booking_notice_message(24) in text


async def test_confirming_a_time_after_the_notice_books():
    result, agenda = await _confirm(f"{OPEN_DAYS[1].isoformat()}T10:00:00", 24)
    assert len(agenda.created) == 1
    text = " ".join(getattr(b, "body", "") or "" for b in result.bubbles)
    assert "antecedência" not in text


def test_the_message_names_the_hours():
    assert fr.booking_notice_message(24) == (
        "Os agendamentos precisam de pelo menos 24 horas de antecedência."
    )
    assert "1 hora de" in fr.booking_notice_message(1)
