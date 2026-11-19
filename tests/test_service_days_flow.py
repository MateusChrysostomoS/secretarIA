"""TASK-087 T4: the patient's button flow, reschedule, colleague offer and AI draft respect the
service's blocked weekdays (SPEC 5.2, R1/R4/R5).

Real native agenda (LocalCalendarService on the in-memory DB, open every day 08-18) behind
SimpleNamespace snapshots shaped like the worker's (`appointment_types` entries carrying
`blocked_weekdays`, `units`, `service_units`, `service_unit_terms`). The calendar filter itself is
T3's (`with_blocked_weekdays`); this file proves every patient path reaches it with the right
service and unit, and that a blocked day typed or confirmed is refused in plain words.
"""

import os
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("APP_ENV", "test")

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from secretaria.services.booking_draft import BookingDraft, resolve_booking_draft  # noqa: E402
from secretaria.services.flow_units import STEP_AWAITING_UNIT  # noqa: E402
from secretaria.services.manage_request import (  # noqa: E402
    FIELD_DAY,
    ManageRequest,
    resolve_manage_request,
)
from secretaria.services.service_terms import UnitTerms  # noqa: E402
from secretaria.services.unit_booking import UnitOption, service_day_refusal  # noqa: E402
from tests._local_agenda import (  # noqa: E402
    WEEK_HOURS,
    agenda,
    seed_tenant,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: E402,F401
from tests.test_flow_router_units import TZ, _conversation, _Gate, _unit_tap  # noqa: E402

CIRURGIA_ID = uuid4()
CONSULTA_ID = uuid4()
REFUSAL_MONDAY = "Cirurgia com Dra. Ana não está disponível às segundas. Posso ver outro dia?"


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):  # noqa: F811
    wire_local_agenda(monkeypatch, db)


def _entries(blocked: list[str] | None = None) -> list[dict]:
    cirurgia = {
        "name": "Cirurgia",
        "duration_min": 30,
        "is_active": True,
        "sort_order": 0,
        "service_id": str(CIRURGIA_ID),
    }
    if blocked is not None:
        cirurgia["blocked_weekdays"] = blocked
    consulta = {
        "name": "Consulta",
        "duration_min": 30,
        "is_active": True,
        "sort_order": 1,
        "service_id": str(CONSULTA_ID),
    }
    return [cirurgia, consulta]


def _doctor(blocked: list[str] | None = None, *, units=(), terms=None, name="Dra. Ana"):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        appointment_types=_entries(blocked),
        business_hours=WEEK_HOURS,
        min_booking_notice_hours=None,
        units=list(units),
        service_units={},
        service_unit_terms=terms or {},
    )


def _tenant(entries: list[dict] | None = None):
    return SimpleNamespace(
        initial_flows={"enabled": True},
        appointment_types=entries if entries is not None else _entries(),
        appointment_duration_min=30,
        business_hours=WEEK_HOURS,
        collect_insurance=False,
        insurance_mode=None,
        insurances=[],
        timezone="America/Sao_Paulo",
    )


def _unit(name: str) -> UnitOption:
    return UnitOption(id=uuid4(), name=name, address=None, business_hours=WEEK_HOURS)


async def _agenda(db):  # noqa: F811
    tenant = await seed_tenant(db)
    return agenda(tenant.id)


def _next(weekday: int) -> date:
    """The clinic-local date of the next `weekday` (0 = Monday) at least 2 days ahead."""
    first = datetime.now(TZ).date() + timedelta(days=2)
    return first + timedelta(days=(weekday - first.weekday()) % 7)


def _slots_bubble(result) -> SlotsBubble:
    [bubble] = [b for b in result.bubbles if isinstance(b, SlotsBubble)]
    return bubble


def _days(result) -> list[date]:
    """The dates offered by a day picker (its `day|<iso>|<page>` rows)."""
    return [
        date.fromisoformat(row[0].split("|")[1])
        for row in _slots_bubble(result).rows
        if row[0].startswith(fr.ROW_DAY_PREFIX)
    ]


def _weekdays(result) -> set[int]:
    days = _days(result)
    assert days, "the day picker offered no day at all"
    return {day.weekday() for day in days}


# --------------------------------------------------------------------------- booking: day picker


async def test_day_picker_hides_blocked_day(db):  # noqa: F811
    doctor = _doctor(["monday"])
    result = await fr.route(
        _conversation(flow_selected_type="Cirurgia", flow_selected_professional_id=doctor.id),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_BOOK_SERVICE,
        professionals=[doctor],
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert 0 not in _weekdays(result)


async def test_other_service_still_shows_monday(db):  # noqa: F811
    doctor = _doctor(["monday"])
    result = await fr.route(
        _conversation(flow_selected_type="Consulta", flow_selected_professional_id=doctor.id),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_BOOK_SERVICE,
        professionals=[doctor],
    )
    assert 0 in _weekdays(result)


async def test_unit_own_days_apply_after_unit_pick(db):  # noqa: F811
    """U1 blocks Friday for the service (its own list), U2 inherits the empty base."""
    centro, sul = _unit("Centro"), _unit("Sul")
    doctor = _doctor(
        units=(centro, sul),
        terms={CIRURGIA_ID: {centro.id: UnitTerms(None, None, frozenset({"friday"}))}},
    )
    cal = await _agenda(db)

    async def pick(unit):
        return await fr.route(
            _conversation(
                flow_step=STEP_AWAITING_UNIT,
                flow_selected_type="Cirurgia",
                flow_selected_professional_id=doctor.id,
            ),
            _tenant(doctor.appointment_types),
            cal,
            _unit_tap(unit),
            professionals=[doctor],
        )

    at_centro = await pick(centro)
    assert at_centro.flow_selected_unit_id == centro.id
    assert 4 not in _weekdays(at_centro)
    at_sul = await pick(sul)
    assert 4 in _weekdays(at_sul)


async def test_later_turns_keep_the_units_days(db):  # noqa: F811
    """The day picker re-rendered on a later turn (unit already stored) still hides the day."""
    centro, sul = _unit("Centro"), _unit("Sul")
    doctor = _doctor(
        units=(centro, sul),
        terms={CIRURGIA_ID: {centro.id: UnitTerms(None, None, frozenset({"friday"}))}},
    )
    result = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_DAY,
            flow_selected_type="Cirurgia",
            flow_selected_professional_id=doctor.id,
            flow_selected_unit_id=centro.id,
        ),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_OTHER_DATE,
        professionals=[doctor],
    )
    assert 4 not in _weekdays(result)


async def test_typed_blocked_date_refused(db):  # noqa: F811
    doctor = _doctor(["monday"])
    monday = _next(0)
    result = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_DAY,
            flow_selected_type="Cirurgia",
            flow_selected_professional_id=doctor.id,
        ),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        monday.strftime("%d/%m"),
        professionals=[doctor],
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert _slots_bubble(result).body.startswith(REFUSAL_MONDAY)
    assert 0 not in _weekdays(result)


async def test_typed_open_date_lists_its_times(db):  # noqa: F811
    """Characterization: an allowed day typed still opens its slot list."""
    doctor = _doctor(["monday"])
    result = await fr.route(
        _conversation(
            flow_step=fr.STEP_AWAITING_DAY,
            flow_selected_type="Cirurgia",
            flow_selected_professional_id=doctor.id,
        ),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        _next(1).strftime("%d/%m"),
        professionals=[doctor],
    )
    assert result.flow_step == fr.STEP_AWAITING_SLOT


# --------------------------------------------------------------------------- booking: confirmation


def _at_confirmation(doctor, day: date):
    return _conversation(
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Cirurgia",
        flow_selected_professional_id=doctor.id,
        flow_selected_day=day.isoformat(),
        flow_selected_slot=f"{day.isoformat()}T09:00",
    )


async def test_confirm_on_blocked_day_refused(db):  # noqa: F811
    doctor = _doctor(["monday"])
    gate = _Gate(armed=False)
    result = await fr.route(
        _at_confirmation(doctor, _next(0)),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_CONFIRM,
        professionals=[doctor],
        gate=gate,
    )
    assert gate.decided == []  # nothing held, nothing booked
    assert result.appointment is None
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert _slots_bubble(result).body.startswith(REFUSAL_MONDAY)
    assert 0 not in _weekdays(result)


async def test_confirm_on_blocked_day_at_the_unit_refused(db):  # noqa: F811
    centro, sul = _unit("Centro"), _unit("Sul")
    doctor = _doctor(
        units=(centro, sul),
        terms={CIRURGIA_ID: {centro.id: UnitTerms(None, None, frozenset({"friday"}))}},
    )
    gate = _Gate(armed=False)
    conversation = _at_confirmation(doctor, _next(4))
    conversation.flow_selected_unit_id = centro.id
    result = await fr.route(
        conversation,
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_CONFIRM,
        professionals=[doctor],
        gate=gate,
    )
    assert gate.decided == []
    assert _slots_bubble(result).body.startswith(
        "Cirurgia com Dra. Ana não está disponível às sextas. Posso ver outro dia?"
    )


async def test_confirm_on_open_day_books(db):  # noqa: F811
    """Characterization: an allowed day goes on to the gate as before."""
    doctor = _doctor(["monday"])
    gate = _Gate(armed=False)
    await fr.route(
        _at_confirmation(doctor, _next(1)),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_CONFIRM,
        professionals=[doctor],
        gate=gate,
    )
    assert len(gate.decided) == 1


# --------------------------------------------------------------------------- other booking entries


async def test_resumed_slot_step_on_blocked_day_falls_back_to_days(db):  # noqa: F811
    """A returning patient parked on a Monday's times (rule changed meanwhile): no Monday."""
    doctor = _doctor(["monday"])
    monday = _next(0)
    result = await fr.resume_bubbles(
        _conversation(
            flow_step=fr.STEP_AWAITING_SLOT,
            flow_selected_type="Cirurgia",
            flow_selected_professional_id=doctor.id,
            flow_selected_day=monday.isoformat(),
        ),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        professionals=[doctor],
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert 0 not in _weekdays(result)


async def test_portal_reoffer_after_the_code_hides_blocked_day(db):  # noqa: F811
    doctor = _doctor(["monday"])
    tuesday_9 = datetime.combine(_next(1), datetime.min.time(), TZ).replace(hour=9)
    result = await fr.reoffer_taken_booking_slot(
        _conversation(flow_selected_type="Cirurgia", flow_selected_professional_id=doctor.id),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        tuesday_9,
        duration_minutes=30,
        professionals=[doctor],
    )
    assert 0 not in _weekdays(result)


async def test_ai_guided_booking_hand_back_hides_blocked_day(db):  # noqa: F811
    doctor = _doctor(["monday"])
    result = await fr.enter_guided_booking(
        _tenant(doctor.appointment_types),
        await _agenda(db),
        "Cirurgia",
        conversation_id=uuid4(),
        professional_id=doctor.id,
        professionals=[doctor],
    )
    assert result.flow_step == fr.STEP_AWAITING_DAY
    assert 0 not in _weekdays(result)


# --------------------------------------------------------------------------- reschedule


def _appointment(doctor, *, unit_id=None, service="Cirurgia") -> dict:
    start = datetime.combine(_next(1), datetime.min.time(), TZ).replace(hour=10)
    return {
        "id": str(uuid4()),
        "start_at": start,
        "end_at": start + timedelta(minutes=30),
        "appointment_type": service,
        "professional_id": str(doctor.id),
        "unit_id": str(unit_id) if unit_id else None,
        "google_event_id": "evt-reschedule",
    }


async def test_reschedule_hides_blocked_day(db):  # noqa: F811
    doctor = _doctor(["monday"])
    appt = _appointment(doctor)
    result = await fr.route(
        _conversation(
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=fr.STEP_MANAGE_ACTION,
            flow_selected_type=None,
            flow_managing_appointment_id=fr._appt_uuid(appt),
        ),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_RESCHEDULE,
        upcoming_appointments=[appt],
        professionals=[doctor],
    )
    assert result.flow_step == fr.STEP_MANAGE_DAY
    assert 0 not in _weekdays(result)


async def test_reschedule_of_another_service_keeps_monday(db):  # noqa: F811
    doctor = _doctor(["monday"])
    appt = _appointment(doctor, service="Consulta")
    result = await fr.route(
        _conversation(
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=fr.STEP_MANAGE_ACTION,
            flow_selected_type=None,
            flow_managing_appointment_id=fr._appt_uuid(appt),
        ),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_RESCHEDULE,
        upcoming_appointments=[appt],
        professionals=[doctor],
    )
    assert 0 in _weekdays(result)


async def test_reschedule_typed_blocked_date_refused(db):  # noqa: F811
    doctor = _doctor(["monday"])
    appt = _appointment(doctor)
    result = await fr.route(
        _conversation(
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=fr.STEP_MANAGE_DAY,
            flow_selected_type=None,
            flow_managing_appointment_id=fr._appt_uuid(appt),
        ),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        _next(0).strftime("%d/%m"),
        upcoming_appointments=[appt],
        professionals=[doctor],
    )
    assert result.flow_step == fr.STEP_MANAGE_DAY
    assert _slots_bubble(result).body.startswith(REFUSAL_MONDAY)


async def test_reschedule_confirm_on_blocked_day_refused(db):  # noqa: F811
    doctor = _doctor(["monday"])
    appt = _appointment(doctor)
    monday = _next(0)
    result = await fr.route(
        _conversation(
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=fr.STEP_MANAGE_CONFIRM,
            flow_selected_type=None,
            flow_managing_appointment_id=fr._appt_uuid(appt),
            flow_selected_day=monday.isoformat(),
            flow_selected_slot=f"{monday.isoformat()}T09:00",
        ),
        _tenant(doctor.appointment_types),
        await _agenda(db),
        fr.LABEL_CONFIRM,
        upcoming_appointments=[appt],
        professionals=[doctor],
    )
    assert result.appointment_reschedule is None
    assert result.flow_step == fr.STEP_MANAGE_DAY
    assert _slots_bubble(result).body.startswith(REFUSAL_MONDAY)
    assert 0 not in _weekdays(result)


async def test_reschedule_at_the_appointments_unit_uses_its_days(db):  # noqa: F811
    centro, sul = _unit("Centro"), _unit("Sul")
    doctor = _doctor(
        units=(centro, sul),
        terms={CIRURGIA_ID: {centro.id: UnitTerms(None, None, frozenset({"friday"}))}},
    )
    cal = await _agenda(db)

    async def reschedule(unit):
        appt = _appointment(doctor, unit_id=unit.id)
        return await fr.route(
            _conversation(
                flow_state=FlowState.MANAGE_BOOKING,
                flow_step=fr.STEP_MANAGE_ACTION,
                flow_selected_type=None,
                flow_managing_appointment_id=fr._appt_uuid(appt),
            ),
            _tenant(doctor.appointment_types),
            cal,
            fr.LABEL_RESCHEDULE,
            upcoming_appointments=[appt],
            professionals=[doctor],
        )

    assert 4 not in _weekdays(await reschedule(centro))
    assert 4 in _weekdays(await reschedule(sul))


async def test_reschedule_without_unit_blocks_a_day_no_open_unit_allows(db):  # noqa: F811
    """Review P2 / R1: Centro opens Mon-Fri and blocks Monday (own list); Norte opens Tue-Fri and
    inherits the empty base. An appointment without a unit is offered within the UNION of the
    units' hours (Monday comes from Centro), yet no unit does Cirurgia on Monday: no Monday row,
    and a forged confirmation on a Monday is refused."""
    mon_fri = ("monday", "tuesday", "wednesday", "thursday", "friday")
    centro = UnitOption(
        id=uuid4(), name="Centro", address=None, business_hours={d: WEEK_HOURS[d] for d in mon_fri}
    )
    norte = UnitOption(
        id=uuid4(),
        name="Norte",
        address=None,
        business_hours={d: WEEK_HOURS[d] for d in mon_fri[1:]},
    )
    doctor = _doctor(
        units=(centro, norte),
        terms={CIRURGIA_ID: {centro.id: UnitTerms(None, None, frozenset({"monday"}))}},
    )
    appt = _appointment(doctor)  # no unit: from before the units, or a reception "encaixe"
    cal = await _agenda(db)

    listed = await fr.route(
        _conversation(
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=fr.STEP_MANAGE_ACTION,
            flow_selected_type=None,
            flow_managing_appointment_id=fr._appt_uuid(appt),
        ),
        _tenant(doctor.appointment_types),
        cal,
        fr.LABEL_RESCHEDULE,
        upcoming_appointments=[appt],
        professionals=[doctor],
    )
    assert listed.flow_step == fr.STEP_MANAGE_DAY
    assert 0 not in _weekdays(listed)

    monday = _next(0)
    forged = await fr.route(
        _conversation(
            flow_state=FlowState.MANAGE_BOOKING,
            flow_step=fr.STEP_MANAGE_CONFIRM,
            flow_selected_type=None,
            flow_managing_appointment_id=fr._appt_uuid(appt),
            flow_selected_day=monday.isoformat(),
            flow_selected_slot=f"{monday.isoformat()}T09:00",
        ),
        _tenant(doctor.appointment_types),
        cal,
        fr.LABEL_CONFIRM,
        upcoming_appointments=[appt],
        professionals=[doctor],
    )
    assert forged.appointment_reschedule is None
    assert forged.flow_step == fr.STEP_MANAGE_DAY
    assert _slots_bubble(forged).body.startswith(
        service_day_refusal("Cirurgia", "Dra. Ana", frozenset({"monday"}))
    )


# --------------------------------------------------------------------------- colleague offer (R1)


async def _rebook(db, colleague, *, same_professional=False):  # noqa: F811
    return await fr.enter_rebooking(
        _conversation(flow_state=FlowState.IDLE, flow_step=None, flow_selected_type=None),
        _tenant(colleague.appointment_types),
        await _agenda(db),
        professionals=[colleague],
        cancelled_professional_id=colleague.id if same_professional else uuid4(),
        service_name="Cirurgia",
        same_professional=same_professional,
        candidates=None if same_professional else [colleague],
    )


async def test_rebooking_union_across_units(db):  # noqa: F811
    """R1: no unit chosen yet - a day stays while ANY unit of the service allows it."""
    centro, sul = _unit("Centro"), _unit("Sul")
    only_centro = _doctor(
        units=(centro, sul),
        terms={CIRURGIA_ID: {centro.id: UnitTerms(None, None, frozenset({"monday"}))}},
        name="Dr. Leo",
    )
    assert 0 in _weekdays(await _rebook(db, only_centro))

    both = _doctor(
        units=(centro, sul),
        terms={
            CIRURGIA_ID: {
                centro.id: UnitTerms(None, None, frozenset({"monday"})),
                sul.id: UnitTerms(None, None, frozenset({"monday"})),
            }
        },
        name="Dr. Leo",
    )
    assert 0 not in _weekdays(await _rebook(db, both))


async def test_rebooking_with_the_same_doctor_hides_blocked_day(db):  # noqa: F811
    doctor = _doctor(["monday"])
    assert 0 not in _weekdays(await _rebook(db, doctor, same_professional=True))


# --------------------------------------------------------------------------- AI hand-backs


async def test_manage_request_new_day_blocked(db):  # noqa: F811
    doctor = _doctor(["monday"])
    appt = _appointment(doctor)
    resolution = await resolve_manage_request(
        ManageRequest("reschedule", day=_next(0)),
        tenant=_tenant(doctor.appointment_types),
        appointments=[appt],
        professionals=[doctor],
        calendar=await _agenda(db),
        conversation_id=uuid4(),
        tz=TZ,
        now=datetime.now(UTC),
    )
    assert FIELD_DAY in resolution.dropped
    assert resolution.result.flow_step == fr.STEP_MANAGE_DAY
    assert _slots_bubble(resolution.result).body.startswith(REFUSAL_MONDAY)
    assert 0 not in _weekdays(resolution.result)


async def test_manage_request_open_day_lists_its_times(db):  # noqa: F811
    doctor = _doctor(["monday"])
    appt = _appointment(doctor)
    resolution = await resolve_manage_request(
        ManageRequest("reschedule", day=_next(2)),
        tenant=_tenant(doctor.appointment_types),
        appointments=[appt],
        professionals=[doctor],
        calendar=await _agenda(db),
        conversation_id=uuid4(),
        tz=TZ,
        now=datetime.now(UTC),
    )
    assert FIELD_DAY not in resolution.dropped
    assert resolution.result.flow_step == fr.STEP_MANAGE_SLOT


async def test_ai_draft_on_blocked_day_refused(db):  # noqa: F811
    doctor = _doctor(["monday"])
    cal = await _agenda(db)

    async def calendar(_professional):
        return cal

    resolution = await resolve_booking_draft(
        BookingDraft(service="Cirurgia", day=_next(0), attendee="self"),
        conversation=_conversation(
            flow_state=FlowState.LLM,
            flow_step=None,
            flow_selected_type=None,
            tenant_id=None,
            flow_draft=None,
        ),
        tenant=_tenant(doctor.appointment_types),
        professional_rows=[doctor],
        service_catalog=[],
        tenant_insurance=None,
        calendar=calendar,
        now=datetime.now(UTC),
    )
    assert FIELD_DAY in resolution.dropped
    assert resolution.result.flow_step == fr.STEP_AWAITING_DAY
    assert _slots_bubble(resolution.result).body.startswith(REFUSAL_MONDAY)
    assert 0 not in _weekdays(resolution.result)
