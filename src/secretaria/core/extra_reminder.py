"""The clinic's extra reminder: "N dias antes, às HH:MM" in its time zone (TASK-048 R9).

Spec docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md §6.2.
The ONE place the rule is written: the planner (services/reminder_schedule.py), the
hub configuration (schemas/config.py, services/hub_configuration.py) and the R9
migration's SQL (same legacy conversion) all follow it. Pure: no DB, no I/O.

The due time is computed on the clinic's LOCAL calendar - the appointment's local
date minus N days, at HH:MM local - and only then converted to UTC, so a daylight
saving change between the two never shifts the hour the patient receives it.
"""

import re
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EXTRA_DAYS_MIN = 2
EXTRA_DAYS_MAX = 14
SEND_TIME_EARLIEST = time(6, 0)
SEND_TIME_LATEST = time(22, 0)
SEND_TIME_STEP_MINUTES = 15
DEFAULT_SEND_TIME = "09:00"
# Never closer to the appointment than one hour before the fixed 1-day reminder (the
# 1500-minute floor R7 had). With the ranges above this only bites a hand-edited row.
MIN_LEAD = timedelta(hours=25)
FALLBACK_ZONE = "UTC"
_MINUTES_PER_DAY = 1440
_CLOCK = re.compile(r"([01][0-9]|2[0-3]):([0-5][0-9])")


def parse_send_time(value: object) -> time | None:
    """'HH:MM' (24 h, two digits each, ASCII) -> time; anything else -> None."""
    if not isinstance(value, str):
        return None
    match = _CLOCK.fullmatch(value)
    if match is None:
        return None
    return time(int(match.group(1)), int(match.group(2)))


def is_valid_send_time(value: object) -> bool:
    """What a clinic may choose: 06:00..22:00 in 15-minute steps."""
    parsed = parse_send_time(value)
    return (
        parsed is not None
        and parsed.minute % SEND_TIME_STEP_MINUTES == 0
        and SEND_TIME_EARLIEST <= parsed <= SEND_TIME_LATEST
    )


def clinic_zone(name: str | None) -> ZoneInfo:
    """The clinic's IANA zone; an unknown or empty name falls back to UTC."""
    try:
        return ZoneInfo(name or FALLBACK_ZONE)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(FALLBACK_ZONE)


def custom_due_at(tenant, start: datetime) -> datetime | None:
    """When the extra reminder of an appointment starting at `start` leaves (UTC), or None.

    None = the clinic has no extra reminder (days or time missing/unusable), or the
    computed moment is less than MIN_LEAD before the start. Whether it is already in
    the past is the CALLER's check (it knows `now`).
    """
    days = tenant.reminder_extra_days_before
    send_time = parse_send_time(tenant.reminder_extra_send_time)
    if days is None or days <= 0 or send_time is None:
        return None
    start_utc = start.replace(tzinfo=UTC) if start.tzinfo is None else start.astimezone(UTC)
    zone = clinic_zone(tenant.timezone)
    local_day = start_utc.astimezone(zone).date() - timedelta(days=days)
    due = datetime.combine(local_day, send_time, tzinfo=zone).astimezone(UTC)
    if start_utc - due < MIN_LEAD:
        return None
    return due


def from_legacy_lead(
    lead: int | None, *, current_send_time: str | None
) -> tuple[int | None, str | None]:
    """R7's "minutes before" -> (days, HH:MM): whole days rounded up, within 2..14.

    Keeps the clinic's stored hour when it is a valid one, else 09:00. None/<=0 = off.
    The R9 migration applies the same rule in SQL to the stored values.
    """
    if lead is None or lead <= 0:
        return None, None
    days = min(EXTRA_DAYS_MAX, max(EXTRA_DAYS_MIN, -(-lead // _MINUTES_PER_DAY)))
    send_time = current_send_time if is_valid_send_time(current_send_time) else DEFAULT_SEND_TIME
    return days, send_time


def legacy_lead_minutes(days: int | None) -> int | None:
    """The legacy column's mirror (days x 1440) - kept until the column is dropped."""
    return days * _MINUTES_PER_DAY if days else None
