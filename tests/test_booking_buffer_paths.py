# ruff: noqa: F811
"""TASK-088 §B, Task 5: the doctor's buffer ("intervalo entre consultas") is wired into EVERY
path that offers or confirms a time to a PATIENT - and into none of the clinic's own.

Task 4 (tests/test_booking_buffer.py) tests the rule in the availability layer. Here the
callers are tested: the guided day list and slot list, the reschedule (the moved booking never
pads itself), the "Confirmar" recheck, the AI's availability tool and draft resolver, the v1 AI
booking tools, "Alterar dados", the Pix / code-gate promotion and a paid service change. The
hub (staff) is tested to stay free.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import datetime as dt  # noqa: E402
from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import (  # noqa: E402
    appointment_edit_flow as edit_flow,
    availability,
    booking_draft as bd,
    flow_router as fr,
)
from secretaria.services.availability_windows import DayRange, scan_free_windows  # noqa: E402
from secretaria.services.booking_draft import BookingDraft, resolve_booking_draft  # noqa: E402
from tests.test_flow_router_insurance import _conversation, _tenant  # noqa: E402

TZ = ZoneInfo("America/Sao_Paulo")
# The day picker's window starts at the REAL today: the agenda is anchored there. Days D1 and D2
# are tomorrow and the day after (never "today", so no slot is already past).
_TODAY = datetime.now(TZ).date()
D1, D2 = _TODAY + timedelta(days=1), _TODAY + timedelta(days=2)
NOW = datetime(_TODAY.year, _TODAY.month, _TODAY.day, 9, 0, tzinfo=TZ)


def _win(hhmm: str, day=D1, minutes: int = 30) -> tuple[datetime, datetime]:
    hour, minute = (int(part) for part in hhmm.split(":"))
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    return start, start + timedelta(minutes=minutes)


def _times(slots) -> list[str]:
    return [s.strftime("%H:%M") for s in slots]


class _Agenda:
    """Open `hours` (30-minute slots) on `open_days`. `consultations` are windows of booked
    visits - [(start, end)] or [(start, end, event_id)] - and are busy as well; `busy` adds
    non-consultation busy time (a block). Every read of the seam is recorded, with the event id
    it was asked to ignore."""

    tzinfo = TZ

    def __init__(self, *, open_days=(D1, D2), hours=(8, 10), consultations=(), busy=(),
                 seam_fails=False):
        self.open_days = list(open_days)
        self.hours = hours
        self.consultations = [(c + (None,))[:3] for c in consultations]
        self.busy = [(b + (None,))[:3] for b in busy]
        self.seam_reads: list[str | None] = []
        self.seam_fails = seam_fails
        self.created: list[datetime] = []
        self.updated: list[tuple[str, datetime]] = []
        self.consultation_windows = self._consultation_windows

    async def _consultation_windows(self, start, end, *, ignore_event_id=None):
        self.seam_reads.append(ignore_event_id)
        if self.seam_fails:
            raise RuntimeError("database down")
        return [
            (s, e)
            for s, e, event in self.consultations
            if not (ignore_event_id is not None and event == ignore_event_id)
            and availability.overlaps(start, end, s, e)
        ]

    def _taken(self, start, end, ignore=None):
        return any(
            not (ignore is not None and event == ignore)
            and availability.overlaps(start, end, s, e)
            for s, e, event in [*self.consultations, *self.busy]
        )

    async def list_available_days(self, start_day, days, slot_minutes=None):
        first = start_day.date()
        return [
            datetime(d.year, d.month, d.day, tzinfo=TZ)
            for d in self.open_days
            if first <= d < first + timedelta(days=days)
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6, **_):
        d = day.date()
        if d not in self.open_days:
            return []
        length = timedelta(minutes=slot_minutes or 30)
        slots, t = [], datetime(d.year, d.month, d.day, self.hours[0], tzinfo=TZ)
        stop = datetime(d.year, d.month, d.day, self.hours[1], tzinfo=TZ)
        while t + length <= stop and len(slots) < max_slots:
            if not self._taken(t, t + length):
                slots.append({"start": t.isoformat(), "end": (t + length).isoformat(),
                              "label": t.strftime("%H:%M")})
            t += timedelta(minutes=30)
        return slots

    async def has_conflict(self, start, end, ignore_event_id=None):
        return self._taken(start, end, ignore_event_id)

    async def is_slot_free(self, start, end, ignore_event_id=None):
        return not self._taken(start, end, ignore_event_id)

    async def create_event(self, start, end, summary, description="", **_):
        self.created.append(start)
        return {"id": "evt-new", "htmlLink": "https://cal/evt",
                "start": {"dateTime": start.isoformat()}, "end": {"dateTime": end.isoformat()}}

    async def update_event(self, event_id, start, end, **_):
        self.updated.append((event_id, start))
        return {"id": event_id}


def _doctor(buffer_minutes, **extra):
    return SimpleNamespace(
        id=uuid4(), name="Dr. Carlos", specialty=None, about=None,
        context_doctor_message=None, appointment_types=None, business_hours=None,
        min_booking_notice_hours=0, buffer_minutes=buffer_minutes, **extra,
    )


@pytest.fixture(autouse=True)
def _pinned_clock(monkeypatch):
    monkeypatch.setattr(fr, "_notice_now", lambda tz: NOW.astimezone(tz))


async def _day_list(calendar, doctor, branch=fr.BOOKING_DAY_BRANCH, **kw) -> list[str]:
    result = await fr.enter_day_picker(
        _conversation(flow_step=fr.STEP_AWAITING_DAY), _tenant(), calendar,
        duration_minutes=30, branch=branch, professionals=[doctor], **kw,
    )
    return [
        row[0].split("|")[1] for row in result.bubbles[0].rows if row[0].startswith("day|")
    ]


def _day_tap(day) -> str:
    """What arrives when a day row is tapped: its label plus "(<id payload>)"."""
    return f"Dia ({day.isoformat()}|0)"


def _slot_rows(result) -> list[str]:
    """The HH:MM of every slot row of the slot picker (the id carries the ISO start)."""
    rows = [row for b in result.bubbles for row in getattr(b, "rows", [])]
    return [row[0].split("T")[1][:5] for row in rows if row[0].startswith("slot|")]


# --- the guided booking: day list, slot list --------------------------------------------------


async def test_flow_day_picker_respects_buffer():
    # D1 is open 08:00-09:00 and has a visit 08:30-09:00: its only free time (08:00) sits right
    # next to it. With a 30-minute buffer the day is not offered; D2 (nothing booked) still is.
    visit = [_win("08:30")]
    kwargs = dict(open_days=(D1, D2), hours=(8, 9), consultations=visit)
    assert await _day_list(_Agenda(**kwargs), _doctor(0)) == [D1.isoformat(), D2.isoformat()]
    assert await _day_list(_Agenda(**kwargs), _doctor(30)) == [D2.isoformat()]


async def test_flow_slot_picker_respects_buffer():
    visit = [_win("08:30")]  # 08:30-09:00; the day is open 08:00-10:00
    day = datetime(D1.year, D1.month, D1.day)

    async def offered(doctor):
        result = await fr._enter_slot_picker(
            _conversation(flow_step=fr.STEP_AWAITING_DAY), _tenant(),
            _Agenda(consultations=visit), day, duration_minutes=30,
            branch=fr.BOOKING_DAY_BRANCH, professionals=[doctor],
        )
        return _slot_rows(result)

    assert await offered(_doctor(0)) == ["08:00", "09:00", "09:30"]
    # 08:00 ends where the visit starts, 09:00 starts where it ends: both are inside the buffer.
    assert await offered(_doctor(30)) == ["09:30"]


# --- reschedule -------------------------------------------------------------------------------

_APPT_ID = uuid4()


def _appointment(doctor, start, *, event_id="evt-mine"):
    return {
        "id": str(_APPT_ID), "google_event_id": event_id, "appointment_type": "Consulta",
        "start_at": start, "end_at": start + timedelta(minutes=30),
        "professional_id": str(doctor.id),
    }


def _managing(step, slot=None):
    return SimpleNamespace(
        id=uuid4(), flow_state=FlowState.MANAGE_BOOKING, flow_step=step, flow_selected_type=None,
        flow_selected_day=slot[:10] if slot else None, flow_selected_slot=slot,
        flow_managing_appointment_id=_APPT_ID, flow_selected_professional_id=None,
        flow_selected_insurance=None, patient_id=None,
    )


def _reschedule_agenda(**kw):
    # My visit 08:30-09:00 (the one being moved) and somebody else's 10:00-10:30; open 08-11.
    return _Agenda(
        hours=(8, 11),
        consultations=[(*_win("08:30"), "evt-mine"), (*_win("10:00"), "evt-other")],
        **kw,
    )


async def test_reschedule_respects_buffer_and_excludes_moved_event():
    doctor = _doctor(30)
    agenda = _reschedule_agenda()
    appt = _appointment(doctor, _win("08:30")[0])
    result = await fr.route(
        _managing(fr.STEP_MANAGE_DAY), _tenant(), agenda, _day_tap(D1),
        upcoming_appointments=[appt], professionals=[doctor],
    )
    # Raw free: 08:00, 09:00, 09:30, 10:30. The other visit (10:00-10:30) pads 09:30 and 10:30
    # away. MY OWN old window never pads: 08:00 and 09:00, right next to where I am now, stay.
    assert _slot_rows(result) == ["08:00", "09:00"]
    assert "evt-mine" in agenda.seam_reads

    # The day list is built the same way: ignoring the moved visit, D1 keeps its free time.
    narrow = _Agenda(hours=(8, 9), consultations=[(*_win("08:30"), "evt-mine")])
    days = await fr.enter_manage_action(
        "reschedule", _tenant(), [appt], [doctor], calendar=narrow, preselected_id=_APPT_ID
    )
    assert D1.isoformat() in [r[0].split("|")[1] for r in days.bubbles[0].rows
                              if r[0].startswith("day|")]

    # And the confirm: moving to 09:00 is fine (only MY old window is adjacent) ...
    slot = f"{D1.isoformat()}T09:00"
    done = await fr.route(
        _managing(fr.STEP_MANAGE_CONFIRM, slot), _tenant(), _reschedule_agenda(),
        "Confirmar", upcoming_appointments=[appt], professionals=[doctor],
    )
    assert done.appointment_reschedule is not None
    # ... while 09:30 touches the OTHER visit (10:00): refused like a slot taken meanwhile.
    refused_agenda = _reschedule_agenda()
    slot = f"{D1.isoformat()}T09:30"
    refused = await fr.route(
        _managing(fr.STEP_MANAGE_CONFIRM, slot), _tenant(), refused_agenda,
        "Confirmar", upcoming_appointments=[appt], professionals=[doctor],
    )
    assert refused.appointment_reschedule is None and refused_agenda.updated == []
    assert fr.SLOT_TAKEN_AT_CONFIRM_MESSAGE in " ".join(
        getattr(b, "body", "") or "" for b in refused.bubbles
    )


# --- confirm ----------------------------------------------------------------------------------


async def _confirm(slot_hhmm: str, doctor, agenda):
    slot = f"{D1.isoformat()}T{slot_hhmm}:00"
    conversation = _conversation(
        flow_step=fr.STEP_AWAITING_CONFIRMATION, flow_selected_slot=slot,
        flow_selected_day=slot[:10],
    )
    return await fr.route(conversation, _tenant(), agenda, fr.LABEL_CONFIRM,
                          professionals=[doctor])


async def test_confirm_recheck_rejects_slot_inside_buffer():
    visit = [_win("08:30")]
    # 09:00 starts exactly where the visit ends: gap 0 < 30. Nothing is created, the day is
    # offered again behind the "just taken" line (R35).
    agenda = _Agenda(consultations=visit)
    refused = await _confirm("09:00", _doctor(30), agenda)
    assert agenda.created == []
    assert refused.flow_step == fr.STEP_AWAITING_DAY
    text = " ".join(getattr(b, "body", "") or "" for b in refused.bubbles)
    assert fr.SLOT_TAKEN_AT_CONFIRM_MESSAGE in text
    # 09:30 leaves exactly 30 minutes: allowed.
    agenda = _Agenda(consultations=visit)
    await _confirm("09:30", _doctor(30), agenda)
    assert len(agenda.created) == 1
    # No buffer configured: the adjacent time books, and the seam is never read.
    agenda = _Agenda(consultations=visit)
    await _confirm("09:00", _doctor(0), agenda)
    assert len(agenda.created) == 1 and agenda.seam_reads == []


# --- no extra read without a buffer (R36) -----------------------------------------------------


async def test_no_extra_read_without_a_buffer_on_a_wired_path():
    visit = [_win("08:30")]
    for buffer_minutes in (0, None):
        agenda = _Agenda(consultations=visit)
        doctor = _doctor(buffer_minutes)
        await _day_list(agenda, doctor)
        await fr._enter_slot_picker(
            _conversation(flow_step=fr.STEP_AWAITING_DAY), _tenant(), agenda,
            datetime(D1.year, D1.month, D1.day), duration_minutes=30,
            branch=fr.BOOKING_DAY_BRANCH, professionals=[doctor],
        )
        await _confirm("09:30", doctor, agenda)
        await fr.route(
            _managing(fr.STEP_MANAGE_DAY), _tenant(), agenda, _day_tap(D1),
            upcoming_appointments=[_appointment(doctor, _win("08:30")[0])],
            professionals=[doctor],
        )
        assert agenda.seam_reads == []


# --- R34: an offer survives a failing consultation read ---------------------------------------


async def test_offer_path_survives_a_failing_consultation_read(monkeypatch):
    warnings = []
    monkeypatch.setattr(
        availability.logger, "warning", lambda event, **fields: warnings.append((event, fields))
    )
    visit = [_win("08:30")]
    # Day list and slot list: offered WITHOUT the padding (the calendar's own busy check stays).
    assert await _day_list(_Agenda(hours=(8, 9), consultations=visit, seam_fails=True),
                           _doctor(30)) == [D1.isoformat(), D2.isoformat()]
    result = await fr._enter_slot_picker(
        _conversation(flow_step=fr.STEP_AWAITING_DAY), _tenant(),
        _Agenda(consultations=visit, seam_fails=True), datetime(D1.year, D1.month, D1.day),
        duration_minutes=30, branch=fr.BOOKING_DAY_BRANCH, professionals=[_doctor(30)],
    )
    assert _slot_rows(result) == ["08:00", "09:00", "09:30"]
    # The AI's scan, too.
    scan = await scan_free_windows(
        _Agenda(consultations=visit, seam_fails=True),
        span=DayRange(first=D1, last=D1), duration_minutes=30,
        buffer_minutes=30,
    )
    assert scan.windows
    assert warnings and all(event == "booking_buffer_offer_read_failed" for event, _ in warnings)
    assert all(set(fields) <= {"error_type", "buffer_minutes"} for _, fields in warnings)


# --- the AI --------------------------------------------------------------------------------


async def test_ai_availability_tool_respects_buffer(monkeypatch):
    from secretaria.ai import availability_tool as at

    class _Gate:
        def __init__(self, **_):
            pass

        async def busy_windows(self, _owner):
            return []

    monkeypatch.setattr(at, "BookingGate", _Gate)
    monkeypatch.setattr(at, "_clinic_today", lambda tz: _TODAY)

    async def read(buffer_minutes):
        doctor = _doctor(buffer_minutes)
        agenda = _Agenda(consultations=[_win("08:30")])

        async def target(*_a):
            return at._Target(calendar=agenda, professional=doctor, owner_id=doctor.id,
                              slot_minutes=30)

        monkeypatch.setattr(at, "_resolve_target", target)
        result = await at._read_availability(
            uuid4(), "Dr. Carlos", "", D1.isoformat(), D1.isoformat()
        )
        return [w["start"] for w in result["windows"]], agenda

    # 08:00-09:00 | visit 08:30-09:00 | 09:00-10:00: without a buffer two windows are told...
    plain, agenda = await read(0)
    assert plain == ["08:00", "09:00"] and agenda.seam_reads == []
    # ... with 30 minutes, only 09:30 stays (08:00 and 09:00 touch the visit).
    padded, agenda = await read(30)
    assert padded == ["09:30"] and agenda.seam_reads


async def test_booking_draft_resolver_respects_buffer():
    day = D1

    class _Cal(_Agenda):
        def __init__(self):
            super().__init__(consultations=[_win("08:30", day)])

    async def source(_professional):
        return _Cal()

    sole = _doctor(30)
    tenant = SimpleNamespace(
        initial_flows={}, appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "sort_order": 0}],
        appointment_duration_min=30,
        business_hours={
            day: [{"start": "08:00", "end": "12:00"}]
            for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
                        "sunday")
        },
        collect_insurance=False,
        insurance_mode=None, insurances=[], insurance_plans=None, insurance_accepted_by={},
    )
    conversation = SimpleNamespace(
        id=uuid4(), tenant_id=None, flow_state=FlowState.LLM, flow_step=None,
        flow_selected_type=None, flow_selected_day=None, flow_selected_slot=None,
        flow_selected_professional_id=None, flow_selected_insurance=None,
        flow_managing_appointment_id=None, flow_attendee_name="__self__", flow_draft=None,
    )
    from secretaria.services.attendee import ATTENDEE_SELF

    conversation.flow_attendee_name = ATTENDEE_SELF
    resolution = await resolve_booking_draft(
        BookingDraft(service="Consulta", day=day, time=dt.time(9, 0)),
        conversation=conversation, tenant=tenant, professional_rows=[sole], service_catalog=[],
        tenant_insurance=None, calendar=source, now=NOW,
    )
    # 09:00 touches the visit: it is not "free" for the draft, so the time is dropped and the
    # slot list of the day lands without it.
    assert bd.FIELD_TIME not in resolution.accepted
    assert _slot_rows(resolution.result) == ["09:30"]


# --- AI v1 booking tools ----------------------------------------------------------------------


async def test_v1_ai_booking_tool_recheck_respects_buffer():
    from secretaria.ai import tools as ai_tools

    visit = [_win("08:30")]
    start, end = _win("09:00")
    with pytest.raises(ai_tools.BookingDraftRequested):
        await ai_tools._hand_back_if_slot_taken(
            _Agenda(consultations=visit), start, end, tool_name="create_event",
            appointment_type=None, buffer_minutes=30,
        )
    free_start, free_end = _win("09:30")
    await ai_tools._hand_back_if_slot_taken(
        _Agenda(consultations=visit), free_start, free_end, tool_name="create_event",
        appointment_type=None, buffer_minutes=30,
    )
    # No buffer: the adjacent time passes and nothing is read.
    agenda = _Agenda(consultations=visit)
    await ai_tools._hand_back_if_slot_taken(
        agenda, start, end, tool_name="create_event", appointment_type=None
    )
    assert agenda.seam_reads == []


async def test_v1_ai_free_slot_lists_respect_buffer():
    from secretaria.ai import tools as ai_tools

    day = datetime(D1.year, D1.month, D1.day)
    agenda = _Agenda(consultations=[_win("08:30")])
    slots = await ai_tools.buffered_free_slots(
        agenda, day=day, max_slots=10, held=[], buffer_minutes=30
    )
    assert [s["label"] for s in slots] == ["09:30"]
    # A held window (another patient's pending Pix) pads too.
    slots = await ai_tools.buffered_free_slots(
        _Agenda(), day=day, max_slots=10, held=[_win("08:30")], buffer_minutes=30
    )
    assert [s["label"] for s in slots] == ["09:30"]
    # The cap is applied AFTER the buffer, so the list still fills.
    slots = await ai_tools.buffered_free_slots(
        _Agenda(hours=(8, 12), consultations=[_win("08:00")]), day=day, max_slots=2, held=[],
        buffer_minutes=30,
    )
    assert [s["label"] for s in slots] == ["09:00", "09:30"]
    # Buffer 0: today's call, no extra read.
    plain = _Agenda(consultations=[_win("08:30")])
    slots = await ai_tools.buffered_free_slots(
        plain, day=day, max_slots=10, held=[], buffer_minutes=0
    )
    assert [s["label"] for s in slots] == ["08:00", "09:00", "09:30"] and plain.seam_reads == []


# --- "Alterar dados" --------------------------------------------------------------------------


async def test_edit_flow_new_time_respects_buffer_with_moved_event_ignored():
    doctor = _doctor(30)
    pid = str(doctor.id)
    agenda = _Agenda(
        hours=(8, 11),
        consultations=[(*_win("08:30"), "evt-mine"), (*_win("10:00"), "evt-other")],
    )

    async def free(hhmm, ignore="evt-mine"):
        start, end = _win(hhmm)
        return await edit_flow._slot_free(agenda, start, end, ignore, pid, [doctor])

    # Next to the OTHER visit (10:00): refused. Next to my own old window: it is ignored.
    assert await free("09:30") is False
    assert await free("09:00") is True
    # Without the moved event being ignored (a different calendar), the same time is padded.
    assert await free("09:00", ignore=None) is False
    # No buffer: today's behaviour (09:30 only needs to be free of overlaps).
    start, end = _win("09:30")
    assert await edit_flow._slot_free(agenda, start, end, "evt-mine", pid, [_doctor(0)]) is True


async def test_edit_flow_pickers_ignore_the_moved_event():
    doctor = _doctor(30)
    agenda = _reschedule_agenda()
    appt = _appointment(doctor, _win("08:30")[0])
    conversation = SimpleNamespace(
        id=uuid4(), flow_step=fr.STEP_EDIT_DAY, flow_edit_draft=None,
        flow_managing_appointment_id=_APPT_ID,
    )
    carrier = fr._DayPickerState(
        id=conversation.id, flow_selected_professional_id=doctor.id,
        flow_managing_appointment_id=_APPT_ID,
    )
    with fr.moving_event_scope(appt["google_event_id"]):
        result = await fr._enter_slot_picker(
            carrier, _tenant(), agenda, datetime(D1.year, D1.month, D1.day),
            duration_minutes=30, branch=fr.EDIT_DAY_BRANCH, professionals=[doctor],
        )
    assert _slot_rows(result) == ["08:00", "09:00"]
    assert "evt-mine" in agenda.seam_reads
    # Outside the scope nothing is ignored: my old window pads its own surroundings.
    agenda = _reschedule_agenda()
    result = await fr._enter_slot_picker(
        carrier, _tenant(), agenda, datetime(D1.year, D1.month, D1.day),
        duration_minutes=30, branch=fr.EDIT_DAY_BRANCH, professionals=[doctor],
    )
    assert _slot_rows(result) == []  # nothing left: the day picker comes back instead


async def test_every_edit_entry_opens_the_moving_scope(monkeypatch):
    """`edit_step`, `redisplay_edit` and `apply_ai_edit` publish the appointment being edited
    for every picker under them, and always take it down again."""
    doctor = _doctor(30)
    appt = _appointment(doctor, _win("08:30")[0])
    conversation = SimpleNamespace(flow_managing_appointment_id=_APPT_ID, flow_edit_draft=None)
    seen: list = []

    async def _inner(*_args, **_kwargs):
        seen.append(fr._MOVING.get())
        return "result"

    for name in ("_edit_step_inner", "_redisplay_edit_inner", "_apply_ai_edit_inner"):
        monkeypatch.setattr(edit_flow, name, _inner)
    tenant = _tenant()
    await edit_flow.edit_step(conversation, tenant, "", [appt], [doctor], None)
    await edit_flow.redisplay_edit(conversation, tenant, [appt], [doctor], None)
    await edit_flow.apply_ai_edit(conversation, tenant, {}, [appt], [doctor], None)
    assert [rule.moved_event_id for rule in seen] == ["evt-mine"] * 3
    assert fr._MOVING.get() is None


# --- Pix / code-gate promotion ---------------------------------------------------------------


async def test_promotion_recheck_respects_buffer(monkeypatch):
    from secretaria.workers.shared import booking_hold as worker_holds

    async def _no_pix_hold(*_a, **_k):
        return False

    async def _buffer_of_30(_reply, _tenant, _held):
        return 30, []

    monkeypatch.setattr(worker_holds, "live_hold_overlaps", _no_pix_hold)
    monkeypatch.setattr(worker_holds, "_held_buffer_rule", _buffer_of_30)
    reply = SimpleNamespace(conversation_id=uuid4())
    tenant = SimpleNamespace(id=uuid4())

    def held(hhmm):
        start, end = _win(hhmm)
        return SimpleNamespace(start_at=start, end_at=end, professional_id=uuid4())

    visit = [_win("08:30")]
    assert await worker_holds._held_slot_unavailable(
        reply, tenant, held("09:00"), _Agenda(consultations=visit)
    ) is True
    assert await worker_holds._held_slot_unavailable(
        reply, tenant, held("09:30"), _Agenda(consultations=visit)
    ) is False

    async def _no_buffer(_reply, _tenant, _held):
        return 0, []

    monkeypatch.setattr(worker_holds, "_held_buffer_rule", _no_buffer)
    agenda = _Agenda(consultations=visit)
    assert await worker_holds._held_slot_unavailable(
        reply, tenant, held("09:00"), agenda
    ) is False and agenda.seam_reads == []


# --- a paid service change ---------------------------------------------------------------------


class _Session:
    def __init__(self, professional):
        self.professional = professional

    async def get(self, _model, _key):
        return self.professional


async def test_service_change_agenda_write_respects_buffer_only_for_the_patient(monkeypatch):
    from secretaria.services.calendar import CalendarSlotTakenError
    from secretaria.services.payments import service_change

    async def _no_hold(*_a, **_k):
        return False

    async def _no_fields(*_a, **_k):
        return {}

    async def _no_windows(*_a, **_k):
        return []

    monkeypatch.setattr(service_change.booking_hold, "live_hold_overlaps", _no_hold)
    monkeypatch.setattr(service_change.booking_hold, "held_windows", _no_windows)
    monkeypatch.setattr(service_change.unit_booking, "booking_unit_fields", _no_fields)
    updates: list = []

    class _Cal(_Agenda):
        async def update_event_details(self, event_id, start, end, summary, description="", **_):
            updates.append(event_id)
            return {"id": event_id}

    start, end = _win("09:00")
    appointment = SimpleNamespace(
        google_event_id="evt-mine", start_at=start, end_at=end, appointment_type="Consulta",
        insurance=None, attendee_name=None, unit_id=None, professional_id=uuid4(),
    )
    tenant = SimpleNamespace(id=uuid4())
    session = _Session(_doctor(30))
    visit = [(*_win("08:30"), "evt-other")]

    with pytest.raises(CalendarSlotTakenError):
        await service_change.sync_agenda_event(
            session, tenant, appointment, _Cal(consultations=visit), patient_name="Rui",
            window_changed=True, buffer_for_patient=True,
        )
    assert updates == []
    # The clinic's own agenda edit (the default) is never limited.
    await service_change.sync_agenda_event(
        session, tenant, appointment, _Cal(consultations=visit), patient_name="Rui",
        window_changed=True,
    )
    assert updates == ["evt-mine"]
