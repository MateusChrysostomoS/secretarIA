"""Clinic-local dates used by both the agent prompt and booking tools."""

from datetime import UTC, date, datetime

import pytest

from secretaria.services.booking_dates import resolve_booking_day

NOW = datetime(2026, 10, 7, 1, 30, tzinfo=UTC)  # still Tuesday 06/10 in São Paulo


@pytest.mark.parametrize(
    "text, expected",
    [
        ("07/10", date(2026, 10, 7)),
        ("7/10/2026", date(2026, 10, 7)),
        ("2026-10-07", date(2026, 10, 7)),
        ("hoje", date(2026, 10, 6)),
        ("amanhã", date(2026, 10, 7)),
        ("depois de amanhã", date(2026, 10, 8)),
        ("quinta", date(2026, 10, 8)),
        ("quinta-feira da semana que vem", date(2026, 10, 15)),
        ("quinta da próxima semana", date(2026, 10, 15)),
        ("segunda da semana que vem", date(2026, 10, 12)),
        ("daqui a 3 dias", date(2026, 10, 9)),
    ],
)
def test_booking_dates_use_the_clinic_local_calendar(text, expected):
    assert resolve_booking_day(text, timezone="America/Sao_Paulo", now=NOW) == expected


@pytest.mark.parametrize("text", ["31/02", "2026-02-30", "07/10/26", "em breve", "quinta que vem"])
def test_ambiguous_or_invalid_dates_need_clarification(text):
    with pytest.raises(ValueError):
        resolve_booking_day(text, timezone="America/Sao_Paulo", now=NOW)


def test_no_year_means_current_clinic_year_not_a_silent_next_year():
    now = datetime(2026, 12, 31, 12, tzinfo=UTC)
    assert resolve_booking_day("01/01", timezone="America/Sao_Paulo", now=now) == date(2026, 1, 1)
    assert resolve_booking_day("amanhã", timezone="America/Sao_Paulo", now=now) == date(2027, 1, 1)


def test_tomorrow_handles_leap_day_and_month_rollover():
    assert resolve_booking_day(
        "amanhã", timezone="UTC", now=datetime(2028, 2, 28, 12, tzinfo=UTC)
    ) == date(2028, 2, 29)
    assert resolve_booking_day(
        "amanhã", timezone="UTC", now=datetime(2026, 2, 28, 12, tzinfo=UTC)
    ) == date(2026, 3, 1)


def test_relative_dates_are_isolated_between_clinics():
    assert resolve_booking_day("hoje", timezone="Asia/Tokyo", now=NOW) == date(2026, 10, 7)
    assert resolve_booking_day("hoje", timezone="America/Sao_Paulo", now=NOW) == date(2026, 10, 6)


WED_11H13 = datetime(2026, 10, 7, 14, 13, tzinfo=UTC)  # Wednesday 07/10, 11:13 in São Paulo
WED_08H = datetime(2026, 10, 7, 11, 0, tzinfo=UTC)  # Wednesday 07/10, 08:00 in São Paulo


@pytest.mark.parametrize(
    "text, at, now, expected",
    [
        # Owner, 2026-10-07: "quarta às 10" said on a Wednesday at 11h is NEXT Wednesday.
        ("quarta", "10:00", WED_11H13, date(2026, 10, 14)),
        ("quarta", "15:00", WED_11H13, date(2026, 10, 7)),  # still ahead today
        ("quarta", None, WED_11H13, date(2026, 10, 7)),  # no time: nearest, as before
        ("quarta", "10:00", WED_08H, date(2026, 10, 7)),  # said before 10h: today
        ("hoje", "10:00", WED_11H13, date(2026, 10, 7)),  # "hoje" stays today
        ("quinta", "10:00", WED_11H13, date(2026, 10, 8)),
    ],
)
def test_a_weekday_equal_to_today_moves_a_week_when_its_time_has_passed(text, at, now, expected):
    assert resolve_booking_day(text, timezone="America/Sao_Paulo", now=now, at=at) == expected


def test_time_has_passed_today_only_speaks_about_today():
    from secretaria.services.booking_dates import time_has_passed_today

    tz = "America/Sao_Paulo"
    assert time_has_passed_today(date(2026, 10, 7), "10:00", timezone=tz, now=WED_11H13)
    assert not time_has_passed_today(date(2026, 10, 7), "15:00", timezone=tz, now=WED_11H13)
    assert not time_has_passed_today(date(2026, 10, 8), "10:00", timezone=tz, now=WED_11H13)
    assert not time_has_passed_today(date(2026, 10, 7), None, timezone=tz, now=WED_11H13)
