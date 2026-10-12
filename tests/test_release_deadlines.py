"""services/release_deadlines.py - when a released PAID slot's refund falls due (TASK-052).

B = min(T + 6 h, 30 min before the clinic closes on T's day), in the clinic's zone; when the
clinic does not close AFTER T that day (closed, no hours, or T already past closing - 30 min),
B = T + 6 h. E = T + 24 h. Never before T.
"""

from datetime import UTC, date, datetime, time, timedelta

from secretaria.services import release_deadlines as rd

SP = "America/Sao_Paulo"  # UTC-3 all year (no DST since 2019)
# Tuesday 2030-03-05, 14:00 in Sao Paulo.
T = datetime(2030, 3, 5, 17, 0, tzinfo=UTC)


def _hours(end: str, *, day: str = "tuesday") -> dict:
    return {day: [{"start": "08:00", "end": "12:00"}, {"start": "13:00", "end": end}]}


def test_the_owners_example_late_closing_gives_t_plus_six_hours():
    # Closes 22:00 -> 21:30 is later than 20:00 (T + 6 h): B = 20:00 local.
    b = rd.decide_by(released_at=T, business_hours=_hours("22:00"), timezone=SP)
    assert b == T + timedelta(hours=6)


def test_an_early_closing_comes_first():
    # Closes 18:00 -> 17:30 local = 20:30 UTC, before T + 6 h.
    assert rd.decide_by(released_at=T, business_hours=_hours("18:00"), timezone=SP) == datetime(
        2030, 3, 5, 20, 30, tzinfo=UTC
    )


def test_released_after_the_closing_margin_gets_six_hours_never_less_than_t():
    late = datetime(2030, 3, 5, 20, 45, tzinfo=UTC)  # 17:45 local, closes 18:00
    assert rd.decide_by(released_at=late, business_hours=_hours("18:00"), timezone=SP) == late + (
        timedelta(hours=6)
    )


def test_a_day_without_hours_or_a_clinic_without_hours_gets_six_hours():
    assert rd.decide_by(
        released_at=T, business_hours=_hours("18:00", day="monday"), timezone=SP
    ) == T + timedelta(hours=6)
    assert rd.decide_by(released_at=T, business_hours={}, timezone=SP) == T + timedelta(hours=6)
    assert rd.decide_by(released_at=T, business_hours=None, timezone=SP) == T + timedelta(hours=6)


def test_the_clinic_zone_decides_the_day_and_the_closing():
    # 14:00 in Manaus (UTC-4) is 18:00 UTC; closes 15:00 local -> 14:30 local = 18:30 UTC.
    manaus_t = datetime(2030, 3, 5, 18, 0, tzinfo=UTC)
    assert rd.decide_by(
        released_at=manaus_t, business_hours=_hours("15:00"), timezone="America/Manaus"
    ) == datetime(2030, 3, 5, 18, 30, tzinfo=UTC)


def test_bad_entries_and_a_bad_zone_are_ignored_safely():
    messy = {"tuesday": [{"start": "08:00", "end": "25:99"}, {"start": "x"}, "nope"]}
    assert rd.closing_time(messy, date(2030, 3, 5)) is None
    b = rd.decide_by(released_at=T, business_hours=messy, timezone="Not/AZone")
    assert b == T + timedelta(hours=6)


def test_the_closing_is_the_latest_end_of_the_day():
    assert rd.closing_time(_hours("19:30"), date(2030, 3, 5)) == time(19, 30)


def test_a_naive_instant_is_read_as_utc_and_e_is_one_day_later():
    naive = T.replace(tzinfo=None)
    b = rd.decide_by(released_at=naive, business_hours=None, timezone=SP)
    assert b == T + timedelta(hours=6)
    assert rd.expires_at(naive) == T + timedelta(hours=24)


def test_b_is_never_before_t_nor_after_e():
    for end in ("09:00", "14:20", "14:31", "18:00", "23:59"):
        b = rd.decide_by(released_at=T, business_hours=_hours(end), timezone=SP)
        assert T <= b < rd.expires_at(T)
