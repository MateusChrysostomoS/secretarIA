"""Free time on one agenda, minus the slots other conversations are holding.

The day picker and the slot picker (services/flow_router.py) used to be the only readers
of an agenda's free time; the AI draft resolver (services/booking_draft.py, TASK-030 P2)
and the AI availability tool (P4) need the same answer. This module IS that answer,
extracted, so there is one definition of "free":

  * the calendar's own walk (`CalendarService.list_available_days` / `list_free_slots`:
    business hours, busy events, slots already past);
  * minus the windows `BookingHold` rows are reserving (services/booking_hold.py), which
    Google has never heard of.

Nothing here returns an event - only instants. That is the contract the AI tool relies
on: no title, attendee or event id ever leaves the calendar layer through this module.

The day picker passes no holds, on purpose: it has always listed every day with free
Google time and let the slot step hide the held slots, and it must keep rendering byte
for byte (tests/test_availability.py). The AI readers pass the holds.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

from secretaria.services.booking_hold import overlaps

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService

# Compatibility floor for a full-day scan. Shorter services need more positions;
# `slot_scan_limit` expands this to the configured duration (which can be one minute).
FREE_SLOT_SCAN_MAX = 96

Window = tuple[datetime, datetime]


def slot_scan_limit(duration_minutes: int) -> int:
    """Enough positions for a full day at any accepted positive minute duration."""
    minutes = max(1, duration_minutes)
    return max(FREE_SLOT_SCAN_MAX, (24 * 60 + minutes - 1) // minutes)


def slot_start(raw: datetime | str, tz: tzinfo | None) -> datetime:
    """A slot's start as an AWARE datetime, whatever shape the calendar gave.

    `list_free_slots` hands back naive ISO strings; a naive value is read in the clinic's
    own timezone, the only reading that can be right - the slot was computed from that
    clinic's business hours.
    """
    value = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    if value.tzinfo is None and tz is not None:
        return value.replace(tzinfo=tz)
    return value


def without_holds(
    slots: Sequence[dict],
    holds: Sequence[Window],
    *,
    duration_minutes: int,
    tz: tzinfo | None,
) -> list[dict]:
    """`slots` minus every slot whose [start, start + duration) overlaps a held window."""
    if not holds:
        return list(slots)
    length = timedelta(minutes=duration_minutes or 0)
    kept: list[dict] = []
    for slot in slots:
        start = slot_start(slot["start"], tz)
        if not any(
            overlaps(start, start + length, held_start, held_end) for held_start, held_end in holds
        ):
            kept.append(slot)
    return kept


async def free_slots_for_day(
    calendar: CalendarService,
    *,
    day: date,
    duration_minutes: int,
    holds: Sequence[Window] = (),
    max_slots: int = FREE_SLOT_SCAN_MAX,
) -> list[datetime]:
    """Every free slot START on `day` (clinic-local, aware), held windows excluded.

    Raises `CalendarUnavailableError` exactly like the calendar does.
    """
    tz = calendar.tzinfo
    slots = await calendar.list_free_slots(
        day=datetime(day.year, day.month, day.day),
        slot_minutes=duration_minutes,
        max_slots=max(max_slots, slot_scan_limit(duration_minutes)),
    )
    kept = without_holds(slots, holds, duration_minutes=duration_minutes, tz=tz)
    return [slot_start(slot["start"], tz).astimezone(tz) for slot in kept]


def _touches_day(day_start: datetime, holds: Sequence[Window], tz: tzinfo | None) -> bool:
    start = day_start if day_start.tzinfo is not None else day_start.replace(tzinfo=tz)
    end = start + timedelta(days=1)
    return any(overlaps(start, end, held_start, held_end) for held_start, held_end in holds)


async def available_day_starts(
    calendar: CalendarService,
    *,
    start: datetime,
    window_days: int,
    duration_minutes: int,
    holds: Sequence[Window] = (),
) -> list[datetime]:
    """Clinic-local midnights of the days with at least one free slot, in order.

    ONE calendar read for the whole window (`list_available_days`). With holds, only the
    days a hold touches are re-read slot by slot and dropped when nothing is left.
    """
    days = await calendar.list_available_days(
        start_day=start, days=window_days, slot_minutes=duration_minutes
    )
    if not holds:
        return days
    kept: list[datetime] = []
    for day_start in days:
        if _touches_day(day_start, holds, calendar.tzinfo) and not await free_slots_for_day(
            calendar, day=day_start.date(), duration_minutes=duration_minutes, holds=holds
        ):
            continue
        kept.append(day_start)
    return kept


async def days_with_free_slots(
    calendar: CalendarService,
    *,
    start: datetime,
    window_days: int,
    duration_minutes: int,
    holds: Sequence[Window] = (),
) -> list[date]:
    """`available_day_starts`, as dates."""
    starts = await available_day_starts(
        calendar,
        start=start,
        window_days=window_days,
        duration_minutes=duration_minutes,
        holds=holds,
    )
    return [day_start.date() for day_start in starts]
