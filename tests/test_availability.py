"""Free time on one agenda minus held slots: one definition for every reader (TASK-030 P2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.core.whatsapp_limits import EMOJI_SCHEDULE, decorate  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import availability, flow_router as fr  # noqa: E402
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
DAY = date(2026, 10, 5)


class _Calendar:
    """`list_free_slots` answers per day; every read is recorded."""

    def __init__(self, free: dict[date, list[str]]):
        self.tzinfo = TZ
        self._free = free
        self.slot_reads: list[tuple[date, int]] = []
        self.day_scans = 0

    async def list_available_days(self, start_day, days, slot_minutes=None):
        self.day_scans += 1
        return [datetime(d.year, d.month, d.day, tzinfo=TZ) for d in sorted(self._free)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        self.slot_reads.append((day.date(), max_slots))
        return [
            {"start": f"{day.date().isoformat()}T{hhmm}", "end": "", "label": hhmm}
            for hhmm in self._free.get(day.date(), [])[:max_slots]
        ]


def _held(hhmm: str, minutes: int = 30, day: date = DAY) -> tuple[datetime, datetime]:
    hour, minute = (int(part) for part in hhmm.split(":"))
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    return start, start + timedelta(minutes=minutes)


def test_slot_start_reads_a_naive_slot_in_the_clinic_timezone():
    assert availability.slot_start("2026-10-05T08:00", TZ) == datetime(2026, 10, 5, 8, 0, tzinfo=TZ)
    aware = datetime(2026, 10, 5, 11, 0, tzinfo=ZoneInfo("UTC"))
    assert availability.slot_start(aware, TZ) is aware


def test_without_holds_drops_only_the_overlapping_slots():
    slots = [
        {"start": "2026-10-05T08:00", "label": "08:00"},
        {"start": "2026-10-05T08:30", "label": "08:30"},
    ]
    kept = availability.without_holds(slots, [_held("08:00")], duration_minutes=30, tz=TZ)
    # Half-open windows: 08:30 touches the 08:00-08:30 hold, it does not overlap it.
    assert [slot["label"] for slot in kept] == ["08:30"]
    assert availability.without_holds(slots, [], duration_minutes=30, tz=TZ) == slots


async def test_free_slots_for_day_reads_the_whole_day_and_subtracts_holds():
    cal = _Calendar({DAY: ["08:00", "08:30", "10:00"]})
    free = await availability.free_slots_for_day(
        cal, day=DAY, duration_minutes=30, holds=[_held("10:00")]
    )
    assert free == [
        datetime(2026, 10, 5, 8, 0, tzinfo=TZ),
        datetime(2026, 10, 5, 8, 30, tzinfo=TZ),
    ]
    # The picker reads 8 slots; "is 16:00 free?" has to see the whole day.
    assert cal.slot_reads == [(DAY, availability.FREE_SLOT_SCAN_MAX)]


async def test_days_with_free_slots_drops_a_day_whose_every_slot_is_held():
    other = DAY + timedelta(days=1)
    cal = _Calendar({DAY: ["08:00"], other: ["09:00"]})
    days = await availability.days_with_free_slots(
        cal,
        start=datetime(2026, 10, 5, 7, 0, tzinfo=TZ),
        window_days=20,
        duration_minutes=30,
        holds=[_held("08:00")],
    )
    assert days == [other]
    # Only the day a hold touches is re-read slot by slot.
    assert [day for day, _max in cal.slot_reads] == [DAY]


async def test_without_holds_the_day_scan_is_one_calendar_read():
    cal = _Calendar({DAY: ["08:00"]})
    days = await availability.days_with_free_slots(
        cal, start=datetime(2026, 10, 5, 7, 0, tzinfo=TZ), window_days=20, duration_minutes=30
    )
    assert days == [DAY]
    assert cal.day_scans == 1
    assert cal.slot_reads == []


# --------------------------------------------------------------------------
# The two pickers keep behaving byte for byte (they now read through the module)
# --------------------------------------------------------------------------


class _Gate:
    """Duck-typed BookingGate: answers `busy_windows` per agenda, records who asked."""

    armed = False

    def __init__(self, windows=None):
        self._windows = windows or {}
        self.asked: list = []

    async def busy_windows(self, professional_id):
        self.asked.append(professional_id)
        return list(self._windows.get(professional_id, []))


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


_SLOTS = [
    {"start": "2026-10-05T08:00", "end": "2026-10-05T08:40", "label": "08:00"},
    {"start": "2026-10-05T08:40", "end": "2026-10-05T09:20", "label": "08:40"},
]
_DAY_TAP = f"{fr._day_row_label(datetime(2026, 10, 5))} (2026-10-05|0)"
_HELD_0800 = (datetime(2026, 10, 5, 8, 0, tzinfo=TZ), datetime(2026, 10, 5, 8, 40, tzinfo=TZ))


def _at_day_step():
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type="Primeira Consulta",
    )


async def test_the_day_picker_is_byte_identical_after_the_extraction():
    days = [datetime(2026, 10, 5, tzinfo=TZ), datetime(2026, 10, 6, tzinfo=TZ)]
    cal = _FakeCalendar(days=days)
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="Primeira Consulta",
    )
    res = await fr.route(conv, _tenant(), cal, fr.LABEL_BOOK_SERVICE)
    (bubble,) = res.bubbles
    assert isinstance(bubble, SlotsBubble)
    assert bubble.body == fr.DAY_PICKER_BODY
    assert bubble.rows == [
        ("day|2026-10-05|0", fr._day_row_label(days[0])),
        ("day|2026-10-06|0", fr._day_row_label(days[1])),
        ("dayback|service", fr.LABEL_ANOTHER_SERVICE),
    ]
    assert (bubble.button_label, bubble.section_title) == ("Ver dias", "Dias disponíveis")
    # One calendar read, same window, the service's own 40 minutes.
    assert [(n, minutes) for _start, n, minutes in cal.day_scans] == [
        (fr.DAY_PICKER_WINDOW_DAYS, 40)
    ]


async def test_the_slot_picker_is_byte_identical_after_the_extraction():
    gate = _Gate()
    res = await fr.route(
        _at_day_step(), _tenant(), _FakeCalendar(slots=_SLOTS), _DAY_TAP, gate=gate
    )
    (bubble,) = res.bubbles
    assert bubble.body == "Horários livres em 05/10:"
    assert bubble.rows == [
        ("slot|2026-10-05T08:00", decorate(EMOJI_SCHEDULE, "08:00")),
        ("slot|2026-10-05T08:40", decorate(EMOJI_SCHEDULE, "08:40")),
        ("dayagain|0", fr.LABEL_ANOTHER_DAY),
        ("dayback|service", fr.LABEL_ANOTHER_SERVICE),
    ]
    assert (res.flow_step, res.flow_selected_day) == (fr.STEP_AWAITING_SLOT, "2026-10-05")
    assert gate.asked == [None]


async def test_a_held_slot_is_hidden_from_the_slot_picker(monkeypatch):
    log = _Log()
    monkeypatch.setattr(fr, "logger", log)
    res = await fr.route(
        _at_day_step(),
        _tenant(),
        _FakeCalendar(slots=_SLOTS),
        _DAY_TAP,
        gate=_Gate({None: [_HELD_0800]}),
    )
    assert [row[0] for row in res.bubbles[0].rows][0] == "slot|2026-10-05T08:40"
    assert [f["hidden"] for e, f in log.events if e == "flow_slots_hidden_by_hold"] == [1]


async def test_a_sole_professionals_hold_is_looked_up_by_their_id():
    """Holds are PLACED with the sole professional as owner (`resolve_booking_owner_id`);
    the picker used to look them up under None and never saw them."""
    sole = SimpleNamespace(
        id=uuid4(),
        name="Dra. Única",
        specialty=None,
        about=None,
        appointment_types=None,
        business_hours=None,
    )
    gate = _Gate({sole.id: [_HELD_0800]})
    res = await fr.route(
        _at_day_step(),
        _tenant(),
        _FakeCalendar(slots=_SLOTS),
        _DAY_TAP,
        professionals=[sole],
        gate=gate,
    )
    assert gate.asked == [sole.id]
    assert "slot|2026-10-05T08:00" not in [row[0] for row in res.bubbles[0].rows]
