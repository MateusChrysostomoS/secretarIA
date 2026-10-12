"""The deadlines of a released PAID slot (TASK-052, owner decisions 2026-10-12). Pure.

T = the release instant. B (`decide_by`) = min(T + 6 h, F), F = 30 minutes before closing on T's
local day: the patient who has not answered by B makes the clinic return the deposit. E
(`expires_at`) = T + 24 h: an offer still undecided then is "cancelled for lack of answer".

`business_hours` is chosen by the CALLER (services/release_offer.py): the hours of the
consultation's doctor (the clinic's when there is no doctor; "clinic with a secretary" = Task 16).
Shape `{"monday": [{"start": "08:00", "end": "12:00"}, ...]}` (schemas/config.py); closing = the
latest `end` of T's weekday, in `Tenant.timezone`. There is no holiday calendar in the product.

When the clinic does not close AFTER T that day (a day without windows, a clinic without hours,
or T already past closing - 30 min), B = T + 6 h: the patient keeps the time to decide and the
clinic reads the notice when it opens. Owner rule "never before T, B at least T" holds: T <= B
<= T + 6 h < E.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

DECIDE_HOURS = 6
CHECK_HOURS = 24
CLOSING_MARGIN = timedelta(minutes=30)
DEFAULT_TIMEZONE = "America/Sao_Paulo"
_WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or DEFAULT_TIMEZONE)
    except Exception:  # noqa: BLE001 - a bad zone never breaks the deadline
        return ZoneInfo(DEFAULT_TIMEZONE)


def _hhmm(value) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        hours, minutes = value.split(":")
        return time(int(hours), int(minutes))
    except (ValueError, TypeError):
        return None


def closing_time(business_hours: dict | None, day: date) -> time | None:
    """The latest valid `end` of `day`'s weekday windows, or None when the clinic is closed."""
    if not isinstance(business_hours, dict):
        return None
    windows = business_hours.get(_WEEKDAYS[day.weekday()])
    if not isinstance(windows, list):
        return None
    ends = [_hhmm(window.get("end")) for window in windows if isinstance(window, dict)]
    valid = [end for end in ends if end is not None]
    return max(valid) if valid else None


def decide_by(
    *, released_at: datetime, business_hours: dict | None, timezone: str | None
) -> datetime:
    released = _utc(released_at)
    six_hours = released + timedelta(hours=DECIDE_HOURS)
    zone = _zone(timezone)
    local_day = released.astimezone(zone).date()
    closing = closing_time(business_hours, local_day)
    if closing is None:
        return six_hours
    cut = (datetime.combine(local_day, closing, tzinfo=zone) - CLOSING_MARGIN).astimezone(UTC)
    if cut <= released:
        return six_hours
    return min(six_hours, cut)


def expires_at(released_at: datetime) -> datetime:
    return _utc(released_at) + timedelta(hours=CHECK_HOURS)
