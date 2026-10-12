"""Local calendar and time rule for the extra reminder (TASK-048 R9)."""

from datetime import UTC, datetime, time, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from secretaria.core.extra_reminder import (
    DEFAULT_SEND_TIME,
    EXTRA_DAYS_MAX,
    EXTRA_DAYS_MIN,
    MIN_LEAD,
    custom_due_at,
    from_legacy_lead,
    is_valid_send_time,
    legacy_lead_minutes,
    parse_send_time,
)


def clinic(days, send_time, timezone="America/Sao_Paulo"):
    return SimpleNamespace(
        timezone=timezone,
        reminder_extra_days_before=days,
        reminder_extra_send_time=send_time,
    )


def test_n_days_before_at_the_chosen_local_hour():
    start = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)  # 09:00 in São Paulo
    assert custom_due_at(clinic(5, "08:30"), start) == datetime(2026, 10, 6, 11, 30, tzinfo=UTC)


def test_days_count_on_the_clinic_calendar_not_utc():
    start = datetime(2026, 10, 11, 1, 30, tzinfo=UTC)  # 2026-10-10 22:30 in São Paulo
    assert custom_due_at(clinic(2, "09:00"), start) == datetime(2026, 10, 8, 12, 0, tzinfo=UTC)


def test_dst_change_between_reminder_and_appointment_keeps_the_local_hour():
    # New York leaves DST on 2026-11-01. Appointment 2026-11-03 10:00 EST (UTC-5);
    # the reminder on 2026-10-31 09:00 is still EDT (UTC-4) -> 13:00 UTC, not 14:00.
    start = datetime(2026, 11, 3, 15, 0, tzinfo=UTC)
    due = custom_due_at(clinic(3, "09:00", "America/New_York"), start)
    assert due == datetime(2026, 10, 31, 13, 0, tzinfo=UTC)
    assert due.astimezone(ZoneInfo("America/New_York")).time() == time(9, 0)


def test_a_naive_start_is_read_as_utc():
    start = datetime(2026, 10, 11, 12, 0)
    assert custom_due_at(clinic(5, "08:30"), start) == datetime(2026, 10, 6, 11, 30, tzinfo=UTC)


@pytest.mark.parametrize(
    ("days", "send_time"),
    [
        (None, "09:00"),
        (3, None),
        (None, None),
        (0, "09:00"),
        (-2, "09:00"),
        (3, "25:00"),
        (3, "9:00"),
    ],
)
def test_off_or_unusable_settings_mean_no_extra_reminder(days, send_time):
    assert custom_due_at(clinic(days, send_time), datetime(2026, 10, 20, 12, 0, tzinfo=UTC)) is None


def test_closer_than_25_hours_never_fires():
    # Only reachable by a hand-edited row (days below the allowed range).
    start = datetime(2026, 10, 11, 3, 0, tzinfo=UTC)  # 00:00 in São Paulo
    assert custom_due_at(clinic(1, "22:00"), start) is None


def test_an_unknown_time_zone_falls_back_to_utc():
    start = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    assert custom_due_at(clinic(2, "09:00", "Mars/Olympus"), start) == datetime(
        2026, 10, 9, 9, 0, tzinfo=UTC
    )


@pytest.mark.parametrize("zone", ["America/Sao_Paulo", "America/New_York"])
@pytest.mark.parametrize("base", ["2026-10-11", "2026-03-09", "2026-11-02"])
def test_every_allowed_setting_is_at_least_25_hours_before_any_start(zone, base):
    tz = ZoneInfo(zone)
    day = datetime.fromisoformat(base)
    times = [f"{h:02d}:{m:02d}" for h in range(6, 23) for m in (0, 15, 30, 45) if (h, m) <= (22, 0)]
    for minute_of_day in range(0, 24 * 60, 15):
        start = (day + timedelta(minutes=minute_of_day)).replace(tzinfo=tz).astimezone(UTC)
        for send_time in times:
            due = custom_due_at(clinic(EXTRA_DAYS_MIN, send_time, zone), start)
            assert due is not None
            assert start - due >= MIN_LEAD


@pytest.mark.parametrize("value", ["06:00", "09:15", "12:30", "21:45", "22:00"])
def test_send_times_on_the_grid_are_valid(value):
    assert is_valid_send_time(value) is True


@pytest.mark.parametrize(
    "value", ["05:45", "22:15", "10:10", "24:00", "9:00", "09:00:00", "", None, 900, "٠٩:٠٠"]
)
def test_send_times_off_the_grid_are_invalid(value):
    assert is_valid_send_time(value) is False


def test_parse_send_time_reads_any_clock_time():
    assert parse_send_time("07:10") == time(7, 10)
    assert parse_send_time("23:59") == time(23, 59)
    assert parse_send_time("24:00") is None


@pytest.mark.parametrize(
    ("lead", "expected_days"),
    [
        (None, None),
        (0, None),
        (-5, None),
        (720, 2),
        (1500, 2),
        (2880, 2),
        (2881, 3),
        (7200, 5),
        (20160, 14),
        (30000, 14),
    ],
)
def test_legacy_minutes_become_whole_days_rounded_up_within_range(lead, expected_days):
    days, send_time = from_legacy_lead(lead, current_send_time=None)
    assert days == expected_days
    assert send_time == (DEFAULT_SEND_TIME if expected_days else None)


def test_legacy_translation_keeps_a_valid_stored_hour():
    assert from_legacy_lead(4320, current_send_time="07:30") == (3, "07:30")
    assert from_legacy_lead(4320, current_send_time="03:00") == (3, DEFAULT_SEND_TIME)


def test_the_mirror_is_days_times_1440():
    assert legacy_lead_minutes(5) == 7200
    assert legacy_lead_minutes(None) is None
    assert (EXTRA_DAYS_MIN, EXTRA_DAYS_MAX) == (2, 14)
