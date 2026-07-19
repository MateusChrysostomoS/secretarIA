"""Exhaustive tests for workers/onboarding_cadence.py — the pure cadence/
due-date math behind the onboarding nudge cron (contract v1 §11/§12).

Every function under test is pure (no DB, no network, no settings lookup), so
these run with plain datetimes — no fixtures, no monkeypatching.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import UTC, datetime, timedelta  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.workers.onboarding_cadence import (  # noqa: E402
    config_reminder_due,
    days_elapsed,
    next_retry_time,
    within_send_window,
)

ANCHOR = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RETRY_CADENCE = [3, 7, 14, 21, 30]
BIWEEKLY = 14
WINDOW_DAYS = 60
CONFIG_CADENCE = [1, 3, 7]
WEEKLY = 7


def _at(days: float) -> datetime:
    return ANCHOR + timedelta(days=days)


# --------------------------------------------------------------------------
# days_elapsed
# --------------------------------------------------------------------------


def test_days_elapsed_basic():
    assert days_elapsed(ANCHOR, _at(3)) == pytest.approx(3.0)
    assert days_elapsed(ANCHOR, ANCHOR) == 0.0
    assert days_elapsed(ANCHOR, _at(-1)) == pytest.approx(-1.0)


# --------------------------------------------------------------------------
# next_retry_time — fixed cadence checkpoints
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "elapsed_days,expected_next_day",
    [
        (0, 3),  # anchor itself -> first checkpoint
        (3, 7),  # exactly AT D+3 (the nudge just fired) -> advances to D+7
        (5, 7),  # mid-way between checkpoints -> next one still ahead
        (7, 14),
        (14, 21),
        (21, 30),
    ],
)
def test_next_retry_time_fixed_cadence(elapsed_days, expected_next_day):
    now = _at(elapsed_days)
    result = next_retry_time(ANCHOR, now, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)
    assert result == _at(expected_next_day)


def test_next_retry_time_exactly_at_checkpoint_excludes_itself():
    """The checkpoint that just fired must never be returned as 'next'."""
    now = ANCHOR + timedelta(days=7)
    result = next_retry_time(ANCHOR, now, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)
    assert result == _at(14)
    assert result != _at(7)


# --------------------------------------------------------------------------
# next_retry_time — biweekly continuation past the fixed cadence
# --------------------------------------------------------------------------


def test_next_retry_time_d30_continues_biweekly_to_d44():
    now = _at(30)
    result = next_retry_time(ANCHOR, now, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)
    assert result == _at(44)


def test_next_retry_time_d44_continues_to_d58():
    now = _at(44)
    result = next_retry_time(ANCHOR, now, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)
    assert result == _at(58)


# --------------------------------------------------------------------------
# next_retry_time — capped at RETRY_WINDOW_TOTAL_DAYS
# --------------------------------------------------------------------------


def test_next_retry_time_d58_is_none_beyond_60_day_cap():
    """D+58's next biweekly point would be D+72, which exceeds the 60-day cap."""
    now = _at(58)
    result = next_retry_time(ANCHOR, now, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)
    assert result is None


def test_next_retry_time_well_beyond_window_is_none():
    now = _at(90)
    result = next_retry_time(ANCHOR, now, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)
    assert result is None


def test_next_retry_time_cap_boundary_exact_60_excluded():
    """A cap that lands exactly on a checkpoint is inclusive (<=), but the
    default cadence never lands exactly on 60 - verify the generic boundary
    with a synthetic cadence that does."""
    result_included = next_retry_time(ANCHOR, ANCHOR, [], 30, window_days=30)
    assert result_included == _at(30)  # 30 <= 30 -> included
    result_excluded = next_retry_time(ANCHOR, ANCHOR, [], 31, window_days=30)
    assert result_excluded is None  # 31 > 30 -> excluded


# --------------------------------------------------------------------------
# next_retry_time — defensive/robustness (empty cadence, negative elapsed)
# --------------------------------------------------------------------------


def test_next_retry_time_empty_cadence_falls_back_to_recurring_step():
    result = next_retry_time(ANCHOR, ANCHOR, [], BIWEEKLY, WINDOW_DAYS)
    assert result == _at(BIWEEKLY)


def test_next_retry_time_now_before_anchor_returns_first_checkpoint():
    now = ANCHOR - timedelta(days=1)
    result = next_retry_time(ANCHOR, now, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)
    assert result == _at(3)


# --------------------------------------------------------------------------
# config_reminder_due — never-sent case
# --------------------------------------------------------------------------


def test_config_reminder_due_never_sent_before_first_point():
    now = _at(0.9)
    assert config_reminder_due(ANCHOR, None, now, CONFIG_CADENCE, WEEKLY) is False


def test_config_reminder_due_never_sent_at_first_point_d1():
    now = _at(1)
    assert config_reminder_due(ANCHOR, None, now, CONFIG_CADENCE, WEEKLY) is True


def test_config_reminder_due_never_sent_after_first_point():
    now = _at(1.5)
    assert config_reminder_due(ANCHOR, None, now, CONFIG_CADENCE, WEEKLY) is True


def test_config_reminder_due_never_sent_empty_cadence_falls_back_to_weekly():
    now = _at(7)
    assert config_reminder_due(ANCHOR, None, now, [], WEEKLY) is True
    assert config_reminder_due(ANCHOR, None, _at(6.9), [], WEEKLY) is False


# --------------------------------------------------------------------------
# config_reminder_due — fixed cadence (D+1 -> D+3 -> D+7)
# --------------------------------------------------------------------------


def test_config_reminder_due_d1_to_d3():
    last_sent = _at(1)
    assert config_reminder_due(ANCHOR, last_sent, _at(2.9), CONFIG_CADENCE, WEEKLY) is False
    assert config_reminder_due(ANCHOR, last_sent, _at(3), CONFIG_CADENCE, WEEKLY) is True


def test_config_reminder_due_d3_to_d7():
    last_sent = _at(3)
    assert config_reminder_due(ANCHOR, last_sent, _at(6.9), CONFIG_CADENCE, WEEKLY) is False
    assert config_reminder_due(ANCHOR, last_sent, _at(7), CONFIG_CADENCE, WEEKLY) is True


# --------------------------------------------------------------------------
# config_reminder_due — weekly continuation past the fixed cadence (uncapped)
# --------------------------------------------------------------------------


def test_config_reminder_due_d7_continues_weekly_to_d14():
    last_sent = _at(7)
    assert config_reminder_due(ANCHOR, last_sent, _at(13.9), CONFIG_CADENCE, WEEKLY) is False
    assert config_reminder_due(ANCHOR, last_sent, _at(14), CONFIG_CADENCE, WEEKLY) is True


def test_config_reminder_due_d14_continues_weekly_to_d21():
    last_sent = _at(14)
    assert config_reminder_due(ANCHOR, last_sent, _at(20.9), CONFIG_CADENCE, WEEKLY) is False
    assert config_reminder_due(ANCHOR, last_sent, _at(21), CONFIG_CADENCE, WEEKLY) is True


def test_config_reminder_due_never_caps_unlike_retry():
    """Config reminders keep recurring indefinitely - no D+60-style cutoff."""
    last_sent = _at(365)
    assert config_reminder_due(ANCHOR, last_sent, _at(372), CONFIG_CADENCE, WEEKLY) is True


# --------------------------------------------------------------------------
# within_send_window
# --------------------------------------------------------------------------


def _local(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 7, 18, hour, minute, tzinfo=UTC)


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (0, 0, False),
        (8, 59, False),
        (9, 0, True),  # start boundary, inclusive
        (12, 0, True),
        (19, 59, True),
        (20, 0, True),  # end boundary, inclusive
        (20, 1, False),
        (23, 59, False),
    ],
)
def test_within_send_window_default_09_20(hour, minute, expected):
    assert within_send_window(_local(hour, minute), "09:00-20:00") is expected


@pytest.mark.parametrize(
    "hour,minute,expected",
    [
        (22, 0, True),  # start boundary
        (23, 30, True),
        (0, 0, True),  # past midnight, still inside
        (5, 59, True),
        (6, 0, True),  # end boundary
        (6, 1, False),
        (12, 0, False),
        (21, 59, False),
    ],
)
def test_within_send_window_overnight_wraparound(hour, minute, expected):
    assert within_send_window(_local(hour, minute), "22:00-06:00") is expected


@pytest.mark.parametrize("bad_window", ["", "not-a-window", "09:00", "25:00-30:00", "abc-def"])
def test_within_send_window_malformed_fails_closed(bad_window):
    assert within_send_window(_local(12, 0), bad_window) is False


# --------------------------------------------------------------------------
# DST-free sanity for America/Sao_Paulo (contract v1 §11 assumption)
# --------------------------------------------------------------------------


def test_america_sao_paulo_has_no_dst_offset_shift():
    """Brazil abolished DST in 2019: the UTC offset must be identical across
    the year, which is what makes plain timedelta day-offset arithmetic (as
    used throughout this module) exact for this tz."""
    tz = ZoneInfo("America/Sao_Paulo")
    jan = datetime(2026, 1, 15, 12, 0, tzinfo=tz)
    jul = datetime(2026, 7, 15, 12, 0, tzinfo=tz)
    assert jan.utcoffset() == jul.utcoffset() == timedelta(hours=-3)


def test_next_retry_time_identical_across_utc_and_local_tz_labels():
    """Passing tz-aware datetimes in America/Sao_Paulo vs UTC must yield the
    exact same checkpoint - tz LABEL never affects the elapsed-days math."""
    tz = ZoneInfo("America/Sao_Paulo")
    anchor_utc = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
    now_utc = anchor_utc + timedelta(days=3)
    anchor_local = anchor_utc.astimezone(tz)
    now_local = now_utc.astimezone(tz)

    result_utc = next_retry_time(anchor_utc, now_utc, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)
    result_local = next_retry_time(anchor_local, now_local, RETRY_CADENCE, BIWEEKLY, WINDOW_DAYS)

    assert result_utc == result_local
