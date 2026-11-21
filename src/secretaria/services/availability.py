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

The doctor's BUFFER between consultations (TASK-088 §B, `Professional.buffer_minutes`) is the
same kind of rule as the notice floor: every function here takes `buffer_minutes` (default 0 =
exactly the behaviour above, no extra read). With N > 0 a time is only offered/confirmed when
it leaves N free minutes before and after each CONSULTATION of the agenda (a live appointment
with a patient, siblings' included - `CalendarService.consultation_windows`) and each held
window; blocks and all-day events are not padded. docs/CHECKPOINT_intervalo_profissional.md.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING

from secretaria.config import get_settings
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

def booking_notice_hours(professional: object | None) -> int:
    """The minimum notice, in hours, a NEW booking with `professional` needs (0 = none).

    The doctor's own `Professional.min_booking_notice_hours` wins, 0 included (an
    explicit "no limit"). Not configured (NULL, a snapshot without the field, no
    professional at all, or a value that is not a whole number >= 0) falls back to the
    clinic-wide default `BOOKING_NOTICE_DEFAULT_HOURS` (owner, 2026-10-27: 12h).
    """
    own = getattr(professional, "min_booking_notice_hours", None)
    if isinstance(own, int) and not isinstance(own, bool) and own >= 0:
        return own
    return max(get_settings().BOOKING_NOTICE_DEFAULT_HOURS, 0)


def booking_notice_floor(professional: object | None, now: datetime) -> datetime | None:
    """The earliest start a patient may BOOK with `professional`, or None (no rule).

    A new booking made through the secretarIA never starts less than
    `booking_notice_hours` after `now`. Only new bookings ask this; cancelling and
    rescheduling do not.
    """
    hours = booking_notice_hours(professional)
    return now + timedelta(hours=hours) if hours > 0 else None


# The most a doctor may ask for between two consultations (spec TASK-088 §B: 0 to 120).
BUFFER_MAX_MINUTES = 120


def booking_buffer_minutes(professional: object | None) -> int:
    """The free minutes a patient's booking with `professional` must leave around each
    consultation (0 = no buffer).

    `Professional.buffer_minutes`: a whole number is clamped to 0..`BUFFER_MAX_MINUTES`;
    NULL, a snapshot without the field, no professional, a bool or anything that is not a
    whole number counts as 0 (unlike the notice there is no clinic-wide default).
    """
    own = getattr(professional, "buffer_minutes", None)
    if isinstance(own, int) and not isinstance(own, bool) and own > 0:
        return min(own, BUFFER_MAX_MINUTES)
    return 0


def padded(windows: Sequence[Window], buffer_minutes: int) -> list[Window]:
    """`windows`, each grown by `buffer_minutes` on both sides (unchanged when 0)."""
    if buffer_minutes <= 0:
        return list(windows)
    pad = timedelta(minutes=buffer_minutes)
    return [(start - pad, end + pad) for start, end in windows]


async def read_consultation_windows(
    calendar: CalendarService,
    start: datetime,
    end: datetime,
    *,
    buffer_minutes: int,
    ignore_event_id: str | None = None,
) -> list[Window]:
    """The consultation windows that can pad anything in `[start, end)`, UNPADDED.

    ONE read of the calendar's `consultation_windows` seam, over the range widened by the
    buffer on both sides. No read at all without a buffer, and a calendar without the seam
    (the bare fakes of the old tests, an agenda that is not a clinic's) answers "no
    consultation windows known": nothing is padded. `ignore_event_id` is the booking being
    moved: it never pads itself.
    """
    if buffer_minutes <= 0:
        return []
    seam = getattr(calendar, "consultation_windows", None)
    if seam is None:
        return []
    pad = timedelta(minutes=buffer_minutes)
    return list(await seam(start - pad, end + pad, ignore_event_id=ignore_event_id))


async def offer_consultation_windows(
    calendar: CalendarService,
    start: datetime,
    end: datetime,
    *,
    buffer_minutes: int,
    ignore_event_id: str | None = None,
) -> list[Window]:
    """`read_consultation_windows` for a path that OFFERS times (day list, slot list, the AI's
    availability): FAIL-OPEN, like the confirm recheck (TASK-088 Task 5, R34).

    A failing read (the database, anything raised) offers the times WITHOUT the padding and
    logs a warning with the error type only: a flaky read must not stop a patient from booking,
    the calendar's own busy check still prevents any overlap, and the confirm recheck asks again.
    """
    try:
        return await read_consultation_windows(
            calendar,
            start,
            end,
            buffer_minutes=buffer_minutes,
            ignore_event_id=ignore_event_id,
        )
    except Exception as exc:
        logger.warning(
            "booking_buffer_offer_read_failed",
            error_type=type(exc).__name__,
            buffer_minutes=buffer_minutes,
        )
        return []


def _aware(value: datetime, tz: tzinfo | None) -> datetime:
    return value.replace(tzinfo=tz) if value.tzinfo is None else value


def at_or_after(
    slots: Sequence[dict], not_before: datetime | None, tz: tzinfo | None
) -> list[dict]:
    """The calendar slot dicts that start at or after `not_before` (all of them when None)."""
    if not_before is None:
        return list(slots)
    return [slot for slot in slots if slot_start(slot["start"], tz) >= not_before]


async def free_slots_for_day(
    calendar: CalendarService,
    *,
    day: date,
    duration_minutes: int,
    holds: Sequence[Window] = (),
    max_slots: int = FREE_SLOT_SCAN_MAX,
    not_before: datetime | None = None,
    buffer_minutes: int = 0,
    consultations: Sequence[Window] | None = None,
) -> list[datetime]:
    """Every free slot START on `day` (clinic-local, aware), held windows excluded.

    `not_before` drops the slots that start earlier (`booking_notice_floor`).
    `buffer_minutes` (`booking_buffer_minutes`) also drops every slot that starts less than
    N minutes after, or ends less than N minutes before, a consultation or a held window; a
    gap of exactly N is kept. `consultations` are the already-read consultation windows
    (`read_consultation_windows`) when the caller scans many days; None reads them here.
    With buffer 0 nothing changes and nothing extra is read.
    Raises `CalendarUnavailableError` exactly like the calendar does.
    """
    tz = calendar.tzinfo
    slots = await calendar.list_free_slots(
        day=datetime(day.year, day.month, day.day),
        slot_minutes=duration_minutes,
        max_slots=max(max_slots, slot_scan_limit(duration_minutes)),
    )
    blockers: Sequence[Window] = holds
    if buffer_minutes > 0:
        if consultations is None:
            first = datetime(day.year, day.month, day.day, tzinfo=tz)
            consultations = await offer_consultation_windows(
                calendar, first, first + timedelta(days=1), buffer_minutes=buffer_minutes
            )
        blockers = padded([*holds, *consultations], buffer_minutes)
    kept = at_or_after(
        without_holds(slots, blockers, duration_minutes=duration_minutes, tz=tz), not_before, tz
    )
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
    not_before: datetime | None = None,
    buffer_minutes: int = 0,
    consultations: Sequence[Window] | None = None,
) -> list[datetime]:
    """Clinic-local midnights of the days with at least one free slot, in order.

    ONE calendar read for the whole window (`list_available_days`). With holds, only the
    days a hold touches are re-read slot by slot and dropped when nothing is left.

    `not_before` (`booking_notice_floor`): the scan still covers the same days, but days
    before the floor's date are dropped and the floor's own day is re-read slot by slot,
    kept only when a slot at or after the floor is free. The window never grows, so the
    picker's "next 20 days" stays the same 20 days.

    `buffer_minutes` (`booking_buffer_minutes`): the consultation windows are read ONCE for
    the whole window (or passed in as `consultations`), and - like a hold - only the days a
    padded consultation or hold touches are re-read slot by slot and dropped when the buffer
    leaves no slot. With buffer 0 nothing is read beyond what is above.
    """
    tz = calendar.tzinfo
    days = await calendar.list_available_days(
        start_day=start, days=window_days, slot_minutes=duration_minutes
    )
    floor_day = None
    if not_before is not None:
        floor_day = not_before.astimezone(tz).date()
        days = [day_start for day_start in days if day_start.date() >= floor_day]
    blockers: Sequence[Window] = holds
    if buffer_minutes > 0:
        if consultations is None:
            first = _aware(start, tz)
            consultations = await offer_consultation_windows(
                calendar, first, first + timedelta(days=window_days), buffer_minutes=buffer_minutes
            )
        blockers = padded([*holds, *consultations], buffer_minutes)
    if not blockers and floor_day is None:
        return days
    kept: list[datetime] = []
    for day_start in days:
        on_floor_day = day_start.date() == floor_day
        if (on_floor_day or _touches_day(day_start, blockers, tz)) and not (
            await free_slots_for_day(
                calendar,
                day=day_start.date(),
                duration_minutes=duration_minutes,
                holds=holds,
                not_before=not_before if on_floor_day else None,
                buffer_minutes=buffer_minutes,
                consultations=consultations,
            )
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
    not_before: datetime | None = None,
    buffer_minutes: int = 0,
    consultations: Sequence[Window] | None = None,
) -> list[date]:
    """`available_day_starts`, as dates."""
    starts = await available_day_starts(
        calendar,
        start=start,
        window_days=window_days,
        duration_minutes=duration_minutes,
        holds=holds,
        not_before=not_before,
        buffer_minutes=buffer_minutes,
        consultations=consultations,
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


async def buffer_taken_at_confirm(
    calendar: CalendarService,
    start: datetime,
    end: datetime,
    *,
    buffer_minutes: int,
    source: str,
    ignore_event_id: str | None = None,
    holds: Sequence[Window] = (),
    conversation_id: object = None,
    tenant_id: object = None,
) -> bool:
    """True when [start, end) sits inside the doctor's buffer of a consultation (TASK-088 §B).

    The confirm / reschedule recheck that goes with `slot_taken_at_confirm`: a list is a
    snapshot, and a consultation (or a Pix hold of another patient) can land next to the
    chosen time between the list and the tap. True when [start, end) is closer than
    `buffer_minutes` to a consultation window - or overlaps it, which `has_conflict` also
    says - or to one of `holds`; a gap of exactly N is allowed. `holds` are OTHER
    conversations' held windows: the confirming conversation's own hold never pads itself.
    `ignore_event_id` is the patient's own event when it is the one being moved.

    No buffer (<= 0) answers False without any read. FAIL-OPEN like `slot_taken_at_confirm`:
    a failing consultation read counts as "no consultation known" (the warning says the
    check did not run) - a flaky database must not stop the clinic from taking appointments.
    `source` names the confirm path in the log lines; ids only, never the patient.
    """
    if buffer_minutes <= 0:
        return False
    tz = calendar.tzinfo
    start, end = _aware(start, tz), _aware(end, tz)
    try:
        consultations = await read_consultation_windows(
            calendar, start, end, buffer_minutes=buffer_minutes, ignore_event_id=ignore_event_id
        )
    except Exception as exc:
        logger.warning(
            "booking_buffer_recheck_failed",
            source=source,
            error_type=type(exc).__name__,
            conversation_id=str(conversation_id) if conversation_id else None,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
        consultations = []
    taken = any(
        overlaps(start, end, held_start, held_end)
        for held_start, held_end in padded([*holds, *consultations], buffer_minutes)
    )
    if taken:
        logger.info(
            "booking_buffer_taken_at_confirm",
            source=source,
            buffer_minutes=buffer_minutes,
            conversation_id=str(conversation_id) if conversation_id else None,
            tenant_id=str(tenant_id) if tenant_id else None,
        )
    return taken
