"""Free time as WINDOWS: the only shape of an agenda the AI may be told (TASK-030 P4).

`services/availability.py` answers "which instants are free on this agenda, holds
subtracted?". The AI's `get_availability` tool (ai/availability_tool.py) must not hand
those instants to a model one by one - thirty slots of forty minutes would exhaust the
answer in two days - so this module turns them into WINDOWS: maximal runs of consecutive
free slots, `{day, start, end}`, in the clinic's own wall clock.

Nothing here knows what a calendar EVENT is. The inputs are instants and a day range; the
outputs are dates and clock times. That is the contract the tool relies on: no title,
attendee, id, description, link or busy block can leave the calendar layer through this
module, because none of them ever enters it.

Three limits protect the model and the Google quota; each one is REPORTED when it bites
(the tool says so in its answer) instead of silently cutting the list:

  * `MAX_DAYS`          - at most 14 days per call;
  * the booking window  - never past what the patient can actually book
                          (`flow_router.DAY_PICKER_WINDOW_DAYS`, passed in by the caller),
                          so a window the AI offers is one the resolver will accept;
  * `MAX_WINDOWS`       - at most 30 windows per call, earliest first.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING

from secretaria.services.availability import Window, days_with_free_slots, free_slots_for_day

if TYPE_CHECKING:
    from secretaria.services.calendar import CalendarService


MAX_DAYS = 14
MAX_WINDOWS = 30

# Which limit shortened the answer (the `clamped` list the tool returns).
CLAMP_MAX_DAYS = "max_days"
CLAMP_BOOKING_WINDOW = "booking_window"
CLAMP_MAX_WINDOWS = "max_windows"

# Why a range was refused (stable enums for the log; never the value the AI sent).
RANGE_BAD_FORMAT = "bad_day_format"
RANGE_PAST_DAY = "past_day"
RANGE_REVERSED = "reversed_range"
RANGE_BEYOND_WINDOW = "beyond_window"

# ASCII digits only: `date.fromisoformat` also accepts "20261008" and "2026-W41-4", and a
# model that sends those has not read the tool description.
_ISO_DAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class RangeError(ValueError):
    """The AI's day range cannot be used.

    `code` is one of the RANGE_* enums; `str(error)` is the Portuguese sentence the model
    reads and can act on. It never contains the value the model sent - a model that was
    handed a patient's words in a date field must not get them echoed back.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DayRange:
    """The days a call will scan, both ends included, after every clamp."""

    first: date
    last: date
    clamped: tuple[str, ...] = ()

    @property
    def days(self) -> int:
        return (self.last - self.first).days + 1


def _parse_day(text: str) -> date | None:
    value = (text or "").strip()
    if not value:
        return None
    try:
        if not _ISO_DAY.fullmatch(value):
            raise ValueError("not AAAA-MM-DD")
        return date.fromisoformat(value)
    except ValueError:
        raise RangeError(
            RANGE_BAD_FORMAT,
            "Os dias precisam estar no formato AAAA-MM-DD (ex.: 2026-10-08), no fuso da clínica.",
        ) from None


def resolve_range(day_from: str, day_to: str, *, today: date, booking_window_days: int) -> DayRange:
    """Validate the AI's range against the clinic's own today, then clamp it.

    `today` is the CLINIC's date, never the server's: between 21:00 and 24:00 in
    America/Sao_Paulo the UTC date is already tomorrow.

    Refused (RangeError): a bad format, a first day before today, a last day before the
    first, a first day beyond the booking window. Clamped, and reported in `clamped`: an
    explicit last day further than `MAX_DAYS` from the first, or beyond the booking window.
    An omitted last day is not a clamp - it defaults to the largest range allowed.
    """
    parsed_from = _parse_day(day_from)
    parsed_to = _parse_day(day_to)
    horizon = today + timedelta(days=booking_window_days - 1)

    first = parsed_from if parsed_from is not None else today
    if first < today:
        raise RangeError(
            RANGE_PAST_DAY,
            f"day_from já passou. Hoje é {today.isoformat()}: use hoje ou uma data futura.",
        )
    if first > horizon:
        raise RangeError(
            RANGE_BEYOND_WINDOW,
            f"A agenda só abre até {horizon.isoformat()} ({booking_window_days} dias à frente). "
            "Peça ao paciente um dia dentro desse período.",
        )

    by_days = first + timedelta(days=MAX_DAYS - 1)
    limit, code = (
        (by_days, CLAMP_MAX_DAYS) if by_days <= horizon else (horizon, CLAMP_BOOKING_WINDOW)
    )
    if parsed_to is None:
        return DayRange(first, limit)
    if parsed_to < first:
        raise RangeError(RANGE_REVERSED, "day_to não pode ser anterior a day_from.")
    if parsed_to > limit:
        return DayRange(first, limit, (code,))
    return DayRange(first, parsed_to)


@dataclass(frozen=True)
class FreeWindow:
    """One continuous stretch of free time on one day, in the clinic's wall clock."""

    day: date
    start: time
    end: time

    def payload(self) -> dict[str, str]:
        """The three keys, and only the three, the AI is shown."""
        return {
            "day": self.day.isoformat(),
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M"),
        }


def slots_to_windows(starts: Sequence[datetime], *, duration_minutes: int) -> list[FreeWindow]:
    """Group free slot STARTS into maximal runs of back-to-back slots.

    A run `[08:00, 08:40, 09:20]` of 40-minute slots is the window 08:00-10:00: the end is
    when the LAST slot finishes. Grouping reads the clinic's wall clock (the tzinfo is
    dropped before any arithmetic) on purpose: a clinic's day is a row of wall-clock hours,
    and a transition day (DST) must read like its neighbours - converting through UTC is
    what would make it not. Duplicates collapse; slots of different days never merge.
    """
    delta = timedelta(minutes=max(int(duration_minutes), 1))
    runs: list[list[datetime]] = []
    for wall in sorted({start.replace(tzinfo=None) for start in starts}):
        previous = runs[-1][-1] if runs else None
        if previous is not None and wall == previous + delta and wall.date() == previous.date():
            runs[-1].append(wall)
        else:
            runs.append([wall])
    return [
        FreeWindow(day=run[0].date(), start=run[0].time(), end=(run[-1] + delta).time())
        for run in runs
    ]


@dataclass
class WindowScan:
    """What a scan found. `truncated`: more windows existed than `max_windows`."""

    windows: list[FreeWindow] = field(default_factory=list)
    truncated: bool = False


async def scan_free_windows(
    calendar: "CalendarService",
    *,
    span: DayRange,
    duration_minutes: int,
    holds: Sequence[Window] = (),
    max_windows: int = MAX_WINDOWS,
) -> WindowScan:
    """Free windows on `calendar` over `span`, earliest first, holds subtracted.

    Built ONLY from `services/availability.py`: one read finds the days with any free time
    (a fully booked or closed agenda costs a single calendar read and no more), then each of
    those days is read slot by slot - sequentially, because the Google client is not safe to
    share across threads - and the scan stops as soon as `max_windows` is exceeded.
    Raises `CalendarUnavailableError` exactly like the calendar does; the caller decides.
    """
    tz = calendar.tzinfo
    free_days = await days_with_free_slots(
        calendar,
        start=datetime(span.first.year, span.first.month, span.first.day, tzinfo=tz),
        window_days=span.days,
        duration_minutes=duration_minutes,
        holds=holds,
    )
    scan = WindowScan()
    for day in free_days:
        if not span.first <= day <= span.last:
            continue
        starts = await free_slots_for_day(
            calendar, day=day, duration_minutes=duration_minutes, holds=holds
        )
        for window in slots_to_windows(starts, duration_minutes=duration_minutes):
            if len(scan.windows) >= max_windows:
                scan.truncated = True
                return scan
            scan.windows.append(window)
    return scan
