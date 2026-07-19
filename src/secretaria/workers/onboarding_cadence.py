"""Pure cadence/due-date math for the onboarding nudge cron (contract v1 §11/§12).

Every function here is a pure function of its arguments — no DB, no network,
no settings lookup, no logging. `workers/onboarding_cron.py` is the only
caller; it resolves `Settings`/tenant timezone and passes plain values in, so
the cadence math itself is exhaustively unit-testable without a DB or an
event loop concern.

Two independent cadences share the same shape ("a fixed list of day-offsets
from an anchor, then a recurring step, capped or uncapped"):

  - RETRY nudges: `RETRY_CADENCE_DAYS` (default [3, 7, 14, 21, 30]) then every
    `RETRY_BIWEEKLY_DAYS` (14), CAPPED at `RETRY_WINDOW_TOTAL_DAYS` (60) —
    beyond the cap there is no next retry (`next_retry_time` returns None).
  - CONFIG reminders: `CONFIG_REMINDER_CADENCE_DAYS` (default [1, 3, 7]) then
    every `CONFIG_REMINDER_WEEKLY_DAYS` (7), UNCAPPED — reminders continue
    indefinitely until `config_status` becomes 'completa' (a state check the
    caller makes, not this module).

`_next_checkpoint_day` is the shared engine behind both `next_retry_time` and
`config_reminder_due`: given how many days have elapsed (as of some
reference point) since the anchor, find the smallest checkpoint day strictly
AFTER that many days have elapsed — i.e. "the next one in the sequence, not
the one that just fired". Day-offset arithmetic (not calendar-day counting)
is deliberate: America/Sao_Paulo (the only shipped tenant timezone) has had
no DST since 2019, so plain `timedelta` maths over tz-aware datetimes is
exact here; a tz that DOES observe DST would need this reworked.
"""

from datetime import datetime, time, timedelta

_SECONDS_PER_DAY = 86400


def days_elapsed(anchor: datetime, now: datetime) -> float:
    """Fractional days between `anchor` and `now` (may be negative).

    Both must be timezone-aware (or both naive-but-consistent) — subtracting
    aware datetimes is instant-correct regardless of which tz label either
    one carries, so callers may pass UTC or local-tz datetimes interchangeably.
    """
    return (now - anchor).total_seconds() / _SECONDS_PER_DAY


def _next_checkpoint_day(
    elapsed_days: float,
    cadence_days: list[int],
    recurring_days: int,
    cap_days: float | None,
) -> int | None:
    """Smallest checkpoint (day-offset from anchor) STRICTLY greater than
    `elapsed_days`, walking the fixed `cadence_days` list first and then
    continuing in `recurring_days` steps from its last element (or from 0
    when `cadence_days` is empty). `cap_days=None` means uncapped; otherwise
    a checkpoint beyond `cap_days` yields None (nothing left to schedule).
    """
    for day in cadence_days:
        if day > elapsed_days:
            return None if (cap_days is not None and day > cap_days) else day

    day = (cadence_days[-1] if cadence_days else 0) + recurring_days
    while day <= elapsed_days:
        day += recurring_days
    if cap_days is not None and day > cap_days:
        return None
    return day


def next_retry_time(
    anchor: datetime,
    now: datetime,
    cadence_days: list[int],
    biweekly_days: int,
    window_days: int,
) -> datetime | None:
    """The NEXT retry-nudge checkpoint strictly after `now`, or None once the
    sequence has exhausted `window_days` (contract: "capped at
    RETRY_WINDOW_TOTAL_DAYS=60 (beyond -> next_retry_at=None)").

    Called right after a nudge fires (`now` is effectively "the checkpoint
    that just fired"), so the checkpoint AT `now` itself is deliberately
    excluded — this always advances to the FOLLOWING one.
    """
    elapsed = days_elapsed(anchor, now)
    next_day = _next_checkpoint_day(elapsed, cadence_days, biweekly_days, float(window_days))
    if next_day is None:
        return None
    return anchor + timedelta(days=next_day)


def config_reminder_due(
    anchor: datetime,
    last_sent: datetime | None,
    now: datetime,
    cadence_days: list[int],
    weekly_days: int,
) -> bool:
    """Whether a config reminder is due at `now`.

    Never sent yet (`last_sent is None`): due once the FIRST cadence point
    (`cadence_days[0]`, e.g. D+1) has been reached. Otherwise: due once the
    next cadence point AFTER `last_sent` (uncapped — config reminders keep
    recurring every `weekly_days` past the fixed cadence) has been reached.
    """
    if last_sent is None:
        first_day = cadence_days[0] if cadence_days else weekly_days
        return now >= anchor + timedelta(days=first_day)

    elapsed_at_last_sent = days_elapsed(anchor, last_sent)
    next_day = _next_checkpoint_day(elapsed_at_last_sent, cadence_days, weekly_days, None)
    if next_day is None:  # unreachable (cap_days=None never yields None); satisfies typing
        return False
    return now >= anchor + timedelta(days=next_day)


def within_send_window(now_local: datetime, window: str) -> bool:
    """Whether `now_local`'s wall-clock time falls inside `window`
    ("HH:MM-HH:MM", both bounds inclusive).

    Handles an overnight window (start > end, e.g. "22:00-06:00") by treating
    it as wrapping past midnight; the shipped default ("09:00-20:00") never
    wraps. Malformed `window` values fail CLOSED (returns False) — a bad
    config value must never turn into round-the-clock messaging.
    """
    try:
        start_str, end_str = window.split("-", 1)
        start_h, start_m = (int(p) for p in start_str.split(":"))
        end_h, end_m = (int(p) for p in end_str.split(":"))
        start, end = time(start_h, start_m), time(end_h, end_m)
    except (ValueError, AttributeError):
        return False

    current = now_local.time()
    if start <= end:
        return start <= current <= end
    return current >= start or current <= end
