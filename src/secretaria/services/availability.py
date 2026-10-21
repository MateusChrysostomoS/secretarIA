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

The day picker passes no holds, on purpose: it lists every day with free Google time and
lets the slot step hide the held slots AND say which times are held (`held_slots`), so a
patient can see that a day may free up when an unpaid Pix expires (TASK-052, owner decision
2026-10-11, reverting the TASK-050 port that hid fully held days). The AI readers pass the
holds.
`slot_taken_at_confirm` is the same definition asked one last time, at the moment a
patient-confirmed booking or move is about to be WRITTEN: a list is a snapshot, and the
clinic (or another channel, or a person in Google Calendar) can fill the slot between the
list and the tap.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

from secretaria.core.logging import get_logger
from secretaria.services.booking_hold import overlaps

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService

logger = get_logger(__name__)

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


def held_slots(
    slots: Sequence[dict],
    holds: Sequence[Window],
    *,
    duration_minutes: int,
    tz: tzinfo | None,
) -> list[dict]:
    """The complement of `without_holds`: the slots another conversation is holding."""
    if not holds:
        return []
    free = without_holds(slots, holds, duration_minutes=duration_minutes, tz=tz)
    kept = {id(slot) for slot in free}
    return [slot for slot in slots if id(slot) not in kept]


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


async def slot_taken_at_confirm(
    calendar: CalendarService,
    start: datetime,
    end: datetime,
    *,
    source: str,
    ignore_event_id: str | None = None,
    conversation_id: object = None,
    tenant_id: object = None,
) -> bool:
    """True only when the agenda about to be WRITTEN proves [start, end) is already taken.

    Asked right before every patient-confirmed `create_event` / `update_event` (the
    guided "Confirmar", the Portal code-gate promotion, the reschedule "Confirmar" and the
    v1 AI booking tools). `calendar` must be the very object the write goes to - the
    owning professional's agenda, resolved exactly as the write resolves it - or the
    answer is about some other doctor's day. `ignore_event_id` is the patient's own event
    when that event is the one being moved: it never "takes" its own new time.

    FAIL-OPEN: a read that fails (Google outage, a revoked token, anything raised) answers
    False, so the booking goes ahead exactly as it did before this check existed, and the
    warning is what says the check did not run. Failing closed would turn a flaky
    `events.list` into a clinic that cannot take appointments; and the write right after
    it hits the same Google, so a real outage still ends in the caller's own
    `calendar_unavailable` path (a person and an alert), as it always did.

    `source` names the confirm path in both log lines; ids only, never the patient.
    """
    try:
        taken = bool(await calendar.has_conflict(start, end, ignore_event_id=ignore_event_id))
    except Exception as exc:
        logger.warning(
            "booking_slot_recheck_failed",
            source=source,
            error_type=type(exc).__name__,
            conversation_id=str(conversation_id) if conversation_id else None,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        return False
    if taken:
        logger.info(
            "booking_slot_taken_at_confirm",
            source=source,
            conversation_id=str(conversation_id) if conversation_id else None,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
    return taken
