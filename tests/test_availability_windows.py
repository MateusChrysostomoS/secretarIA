"""Free time as windows: the range rules and the grouping (TASK-030 P4, spec §4.6)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import date, datetime, time  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.services import availability_windows as aw  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
TODAY = date(2026, 10, 5)  # a fixed anchor: no test here reads the wall clock
WINDOW_DAYS = 20  # flow_router.DAY_PICKER_WINDOW_DAYS


def _range(day_from="", day_to="", *, today=TODAY):
    return aw.resolve_range(day_from, day_to, today=today, booking_window_days=WINDOW_DAYS)


# --------------------------------------------------------------------------
# resolve_range: refused, clamped, defaulted
# --------------------------------------------------------------------------


def test_an_empty_range_is_today_through_the_largest_allowed_span():
    span = _range()
    assert (span.first, span.last, span.clamped) == (TODAY, date(2026, 10, 18), ())
    assert span.days == aw.MAX_DAYS == 14


def test_an_explicit_range_inside_the_limits_is_kept_untouched():
    span = _range("2026-10-08", "2026-10-09")
    assert (span.first, span.last, span.clamped, span.days) == (
        date(2026, 10, 8),
        date(2026, 10, 9),
        (),
        2,
    )


def test_a_365_day_range_is_clamped_to_14_days_and_says_so():
    span = _range("2026-10-05", "2027-10-05")
    assert (span.first, span.last) == (TODAY, date(2026, 10, 18))
    assert span.clamped == (aw.CLAMP_MAX_DAYS,)


def test_the_range_never_passes_the_booking_window():
    # Day 10 + 14 days would end on day 23, past the 20-day window the resolver accepts.
    span = _range("2026-10-15", "2026-11-30")
    assert (span.first, span.last) == (date(2026, 10, 15), date(2026, 10, 24))
    assert span.clamped == (aw.CLAMP_BOOKING_WINDOW,)
    # An omitted last day is not a clamp: it simply ends where booking ends.
    assert _range("2026-10-15").clamped == ()
    assert _range("2026-10-15").last == date(2026, 10, 24)


def test_a_range_across_a_month_and_year_boundary_counts_real_days():
    span = _range("2026-12-28", "2027-01-05", today=date(2026, 12, 28))
    assert (span.first, span.last, span.days) == (date(2026, 12, 28), date(2027, 1, 5), 9)
    assert _range(today=date(2026, 12, 28)).last == date(2027, 1, 10)


@pytest.mark.parametrize(
    "day_from, day_to, code",
    [
        ("2026-10-04", "", aw.RANGE_PAST_DAY),  # yesterday
        ("1900-01-01", "", aw.RANGE_PAST_DAY),
        ("", "2026-10-04", aw.RANGE_REVERSED),  # an omitted day_from is today
        ("2026-10-09", "2026-10-08", aw.RANGE_REVERSED),  # negative range
        ("2026-10-25", "", aw.RANGE_BEYOND_WINDOW),  # day 20 is one past the window
        ("9999-12-31", "", aw.RANGE_BEYOND_WINDOW),  # and must not overflow date arithmetic
        ("08/10/2026", "", aw.RANGE_BAD_FORMAT),
        ("2026-13-01", "", aw.RANGE_BAD_FORMAT),
        ("2026-02-30", "", aw.RANGE_BAD_FORMAT),
        ("20261008", "", aw.RANGE_BAD_FORMAT),  # date.fromisoformat accepts these two
        ("2026-W41-4", "", aw.RANGE_BAD_FORMAT),
        ("٢٠٢٦-١٠-٠٨", "", aw.RANGE_BAD_FORMAT),
        ("amanhã", "", aw.RANGE_BAD_FORMAT),
        ("", "next week", aw.RANGE_BAD_FORMAT),
    ],
)
def test_an_unusable_range_is_refused_with_a_stable_code(day_from, day_to, code):
    with pytest.raises(aw.RangeError) as error:
        _range(day_from, day_to)
    assert error.value.code == code
    # The sentence is the model's to read; it never repeats what the model sent.
    for sent in (day_from, day_to):
        assert not sent or sent not in str(error.value)


def test_the_last_bookable_day_is_accepted_and_the_next_is_not():
    assert _range("2026-10-24").first == date(2026, 10, 24)  # today + 19
    with pytest.raises(aw.RangeError):
        _range("2026-10-25")


# --------------------------------------------------------------------------
# slots_to_windows: runs of back-to-back slots, in the clinic's wall clock
# --------------------------------------------------------------------------


def _slot(day: int, hhmm: str, tz=TZ) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 10, day, hour, minute, tzinfo=tz)


def test_back_to_back_slots_become_one_window_ending_when_the_last_slot_ends():
    starts = [_slot(8, "08:00"), _slot(8, "08:40"), _slot(8, "09:20")]
    (window,) = aw.slots_to_windows(starts, duration_minutes=40)
    assert window.payload() == {"day": "2026-10-08", "start": "08:00", "end": "10:00"}


def test_a_gap_starts_a_new_window():
    starts = [_slot(8, "08:00"), _slot(8, "08:30"), _slot(8, "14:00"), _slot(8, "14:30")]
    windows = aw.slots_to_windows(starts, duration_minutes=30)
    assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in windows] == [
        ("08:00", "09:00"),
        ("14:00", "15:00"),
    ]


def test_slots_are_sorted_deduplicated_and_never_merged_across_days():
    starts = [_slot(9, "00:00"), _slot(8, "23:30"), _slot(8, "23:30"), _slot(8, "23:00")]
    windows = aw.slots_to_windows(starts, duration_minutes=30)
    assert [w.payload()["day"] for w in windows] == ["2026-10-08", "2026-10-09"]
    assert windows[0].start.strftime("%H:%M") == "23:00"
    assert aw.slots_to_windows([], duration_minutes=30) == []


def test_the_wall_clock_is_read_as_is_whatever_the_utc_offset():
    new_york = ZoneInfo("America/New_York")
    # 2037-03-08 is the US spring-forward Sunday: 08:00 local is UTC-4, the day before UTC-5.
    for day in (7, 8):
        starts = [
            datetime(2037, 3, day, 8, 0, tzinfo=new_york),
            datetime(2037, 3, day, 9, 0, tzinfo=new_york),
        ]
        (window,) = aw.slots_to_windows(starts, duration_minutes=60)
        assert (window.start.strftime("%H:%M"), window.end.strftime("%H:%M")) == ("08:00", "10:00")


def test_a_window_exposes_exactly_day_start_end():
    window = aw.FreeWindow(day=TODAY, start=time(8, 5), end=time(9, 5))
    assert window.payload() == {"day": "2026-10-05", "start": "08:05", "end": "09:05"}
