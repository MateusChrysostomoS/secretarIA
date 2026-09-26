"""TASK-006 part 1 - independent behaviour tests for the convênio-first booking flow.

Owner's order (2026-09-23): pra-quem -> convênio -> profissional -> serviço ->
detalhe "Sim, agendar" -> dia -> horário. The convênio answer never hides a
doctor: every active doctor is listed, and the ones who accept the patient's
catalog plan are MARKED (INSURANCE_ACCEPTED_MARK in the row description).

Pure router, no DB / no network: SimpleNamespace snapshots shaped like
workers/tasks.py::_flow_tenant_snapshot (with `insurance_plans` +
`insurance_accepted_by`) and a fake calendar. Every multi-turn test drives
`route()` turn by turn, feeding each result back as the next conversation the
way `_apply_flow_result` persists it, and taps rows by their real id/title
through `inbound_routing_text`.
"""

import os
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4
from zoneinfo import ZoneInfo

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.ai.formatter import SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.schemas.webhook import inbound_routing_text  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.attendee import LABEL_ATTENDEE_SELF  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    BTN_CHOOSE_PROFESSIONAL,
    BTN_CHOOSE_SERVICE,
    INSURANCE_ACCEPTED_MARK,
    LABEL_BOOK,
    LABEL_BOOK_SERVICE,
    LABEL_DONT_KNOW,
    LABEL_INSURANCE_OTHER,
    LABEL_INSURANCE_PARTICULAR,
    STEP_AWAITING_ATTENDEE_CHOICE,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SERVICE_CONFIRM,
    STEP_AWAITING_SLOT,
    enter_guided_booking,
    route,
)
from secretaria.services.insurance_catalog import catalog_id_for_slug  # noqa: E402

_TZ = ZoneInfo("America/Sao_Paulo")

UNIMED = {"id": str(catalog_id_for_slug("unimed")), "name": "Unimed"}
AMIL = {"id": str(catalog_id_for_slug("amil")), "name": "Amil"}
BRADESCO = {"id": str(catalog_id_for_slug("bradesco-saude")), "name": "Bradesco Saúde"}
CLINIC_PLANS = [AMIL, BRADESCO, UNIMED]


# --------------------------------------------------------------------------
# Snapshots + driving helpers
# --------------------------------------------------------------------------


def _tenant(*, collect_insurance=True, plans=None, accepted_by=None):
    """Tenant snapshot as the worker builds it (catalog-backed convênios)."""
    plans = list(CLINIC_PLANS if plans is None else plans)
    return SimpleNamespace(
        initial_flows={
            "enabled": True,
            "buttons": ["Serviços e Custo", "Horários", "Outro"],
            "menu_label": "Como posso ajudar?",
        },
        appointment_types=[
            {"name": "Consulta Geral", "duration_min": 30, "is_active": True, "sort_order": 0}
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        collect_insurance=collect_insurance,
        # The hub keeps the legacy strings mirrored during the transition.
        insurances=[plan["name"] for plan in plans],
        insurance_plans=plans,
        insurance_accepted_by={
            str(key): frozenset(value) for key, value in (accepted_by or {}).items()
        },
    )


def _doctor(name, specialty=None, services=None):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=specialty,
        about=None,
        appointment_types=services,
        business_hours=None,  # inherits the clinic's hours
    )


def _roster():
    """4 doctors, mixed acceptance of Unimed (the plan the patient will pick)."""
    ana = _doctor(
        "Dra. Ana",
        "Cardiologia",
        [{"name": "Consulta Cardio", "duration_min": 45, "is_active": True, "sort_order": 0}],
    )
    bruno = _doctor("Dr. Bruno", "Dermatologia")  # accepts Amil only -> NOT marked
    carla = _doctor("Dra. Carla")  # no specialty, accepts Unimed -> marked
    davi = _doctor("Dr. Davi", "Ortopedia")  # accepts nothing -> NOT marked
    accepted = {
        ana.id: {UNIMED["id"], AMIL["id"]},
        bruno.id: {AMIL["id"]},
        carla.id: {UNIMED["id"]},
    }
    return [ana, bruno, carla, davi], accepted


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_managing_appointment_id=None,
        flow_attendee_name=None,
        patient_id=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _advance(result):
    """The conversation as `_apply_flow_result` leaves it after `result`."""
    return _conversation(
        flow_state=result.flow_state,
        flow_step=result.flow_step,
        flow_selected_type=result.flow_selected_type,
        flow_selected_day=result.flow_selected_day,
        flow_selected_slot=result.flow_selected_slot,
        flow_selected_professional_id=result.flow_selected_professional_id,
        flow_selected_insurance=result.flow_selected_insurance,
        flow_managing_appointment_id=result.flow_managing_appointment_id,
        flow_attendee_name=result.flow_attendee_name,
    )


def _tap(row):
    """What the router reads for a tap on list row `(id, title[, description])`."""
    return inbound_routing_text(row[1], row[0])


def _row(result, predicate):
    rows = result.bubbles[0].rows
    matches = [row for row in rows if predicate(row)]
    assert matches, f"no row matched in {rows!r}"
    return matches[0]


def _row_titled(result, title):
    return _row(result, lambda row: row[1] == title)


def _prof_row(result, professional):
    return _row(result, lambda row: row[0] == f"prof|{professional.id}")


class _FakeCalendar:
    tzinfo = _TZ

    async def create_event(self, start, end, summary, description=""):
        return {
            "id": "evt-conv",
            "htmlLink": "https://cal/evt-conv",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }

    async def list_available_days(self, start_day, days, slot_minutes=None):
        base = start_day.replace(hour=0, minute=0, second=0, microsecond=0)
        return [base + timedelta(days=offset) for offset in range(days)]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return [{"start": "2026-10-05T08:00", "end": "2026-10-05T08:30", "label": "08:00"}]


async def _turn(conversation, tenant, body, professionals, **kw):
    return await route(
        conversation, tenant, _FakeCalendar(), body, professionals=professionals, **kw
    )


async def _to_insurance_list(tenant, professionals, entry=LABEL_BOOK, start_state=FlowState.IDLE):
    """Entry tap -> pra-quem "Sim". Returns the result after pra-quem."""
    asked = await _turn(_conversation(flow_state=start_state), tenant, entry, professionals)
    assert asked.flow_step == STEP_AWAITING_ATTENDEE_CHOICE, asked
    return await _turn(_advance(asked), tenant, LABEL_ATTENDEE_SELF, professionals)


def _doctor_rows(result):
    """The `prof|` rows of a doctor list (the fixed "Não sei" row excluded)."""
    return [row for row in result.bubbles[0].rows if row[0].startswith("prof|")]


def _marked_ids(result) -> set[str]:
    return {
        row[0].removeprefix("prof|")
        for row in _doctor_rows(result)
        if row[2] and str(row[2]).startswith(INSURANCE_ACCEPTED_MARK)
    }


def _assert_full_list_with_marks(result, professionals, expected_marked):
    """THE symptom contract: every doctor listed, marked exactly when accepting.

    Unmarked doctors keep their plain specialty description (never a mark,
    never removed). "Não sei" stays the fixed last row.
    """
    assert result.flow_step == STEP_AWAITING_PROFESSIONAL
    bubble = result.bubbles[0]
    assert isinstance(bubble, SlotsBubble)
    rows = bubble.rows
    assert len(rows) == len(professionals) + 1
    assert rows[-1][1] == LABEL_DONT_KNOW
    assert {row[0] for row in _doctor_rows(result)} == {f"prof|{p.id}" for p in professionals}
    expected = {str(p.id) for p in expected_marked}
    assert _marked_ids(result) == expected
    for professional in professionals:
        description = _prof_row(result, professional)[2]
        if str(professional.id) in expected:
            assert description.startswith(INSURANCE_ACCEPTED_MARK)
            if professional.specialty:
                # The specialty is still there, after the mark.
                assert professional.specialty in description
        else:
            assert description == professional.specialty


# --------------------------------------------------------------------------
# 1. Symptom test - the doctor list after a convênio answer
# --------------------------------------------------------------------------


@pytest.mark.parametrize("entry", [BTN_CHOOSE_PROFESSIONAL, BTN_CHOOSE_SERVICE])
async def test_symptom_all_doctors_listed_accepting_ones_marked(entry):
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)

    # MENU -> "Escolher médico"/"Escolher serviço" -> pra-quem "Sim"
    insurance_q = await _to_insurance_list(
        tenant, professionals, entry=entry, start_state=FlowState.MENU
    )
    # -> the convênio list comes FIRST, with the clinic's catalog plans
    assert insurance_q.flow_step == STEP_AWAITING_INSURANCE
    assert [row[1] for row in insurance_q.bubbles[0].rows] == [
        "Amil",
        "Bradesco Saúde",
        "Unimed",
        LABEL_INSURANCE_PARTICULAR,
        LABEL_INSURANCE_OTHER,
    ]

    # -> tap "Unimed"
    doctors = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Unimed")), professionals
    )
    ana, bruno, carla, davi = professionals
    _assert_full_list_with_marks(doctors, professionals, expected_marked=[ana, carla])
    assert doctors.flow_selected_insurance == "Unimed"
    # Nobody is absent: exactly the roster count of doctor rows.
    assert len(_doctor_rows(doctors)) == len(professionals)
    # The list tells the patient what the mark means.
    assert "Unimed" in doctors.bubbles[0].body


async def test_symptom_other_plan_marks_a_different_subset():
    """Amil is accepted by Ana and Bruno - the marks follow the plan, not the doctor."""
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    insurance_q = await _to_insurance_list(tenant, professionals)
    doctors = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Amil")), professionals
    )
    ana, bruno, _carla, _davi = professionals
    _assert_full_list_with_marks(doctors, professionals, expected_marked=[ana, bruno])


async def test_symptom_plan_nobody_accepts_lists_everyone_unmarked():
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    insurance_q = await _to_insurance_list(tenant, professionals)
    doctors = await _turn(
        _advance(insurance_q),
        tenant,
        _tap(_row_titled(insurance_q, "Bradesco Saúde")),
        professionals,
    )
    _assert_full_list_with_marks(doctors, professionals, expected_marked=[])


async def test_bite_checker_catches_dropped_marks(monkeypatch):
    """Proves test 1 bites: with the acceptance check neutered, the check fails."""
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    monkeypatch.setattr(flow_router, "_accepts_plan", lambda *a, **k: False)
    insurance_q = await _to_insurance_list(tenant, professionals)
    doctors = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Unimed")), professionals
    )
    assert _marked_ids(doctors) == set()
    with pytest.raises(AssertionError):
        _assert_full_list_with_marks(
            doctors, professionals, expected_marked=[professionals[0], professionals[2]]
        )


async def test_bite_checker_catches_filtered_doctors(monkeypatch):
    """Proves test 1 bites: a router that HIDES non-accepting doctors fails it."""
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    original = flow_router._professional_rows

    def _filtering(tenant_, profs, insurance):
        rows, plan = original(tenant_, profs, insurance)
        return [row for row in rows if row[2] and INSURANCE_ACCEPTED_MARK in row[2]], plan

    monkeypatch.setattr(flow_router, "_professional_rows", _filtering)
    insurance_q = await _to_insurance_list(tenant, professionals)
    doctors = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Unimed")), professionals
    )
    with pytest.raises(AssertionError):
        _assert_full_list_with_marks(
            doctors, professionals, expected_marked=[professionals[0], professionals[2]]
        )


# --------------------------------------------------------------------------
# 2. The table
# --------------------------------------------------------------------------


@pytest.mark.parametrize("accepts", [True, False], ids=["accepts", "not-accepts"])
@pytest.mark.parametrize("professional_count", [0, 1, 2, 3])
@pytest.mark.parametrize("plans_filled", [True, False], ids=["plans", "no-plans"])
@pytest.mark.parametrize("collect", [True, False], ids=["collect", "no-collect"])
async def test_table(collect, plans_filled, professional_count, accepts):
    professionals = [
        _doctor(f"Dr(a). {index}", f"Especialidade {index}") for index in range(professional_count)
    ]
    accepted: dict = {}
    if professionals:
        # doctor 0 is the one whose acceptance varies; the rest take Amil only.
        if accepts:
            accepted[professionals[0].id] = {UNIMED["id"]}
        for other in professionals[1:]:
            accepted[other.id] = {AMIL["id"]}
    tenant = _tenant(
        collect_insurance=collect,
        plans=CLINIC_PLANS if plans_filled else [],
        accepted_by=accepted,
    )

    after_attendee = await _to_insurance_list(tenant, professionals)
    asks_insurance = collect and plans_filled
    multi = professional_count >= 2

    if asks_insurance:
        assert after_attendee.flow_step == STEP_AWAITING_INSURANCE
        nxt = await _turn(
            _advance(after_attendee),
            tenant,
            _tap(_row_titled(after_attendee, "Unimed")),
            professionals,
        )
        assert nxt.flow_selected_insurance == "Unimed"
    else:
        # No convênio question: pra-quem goes straight on.
        assert after_attendee.flow_step != STEP_AWAITING_INSURANCE
        nxt = after_attendee
        assert nxt.flow_selected_insurance is None

    if multi:
        expected = [professionals[0]] if (asks_insurance and accepts) else []
        _assert_full_list_with_marks(nxt, professionals, expected_marked=expected)
    else:
        # 0/1 doctor: the doctor list is skipped, the service list opens.
        assert nxt.flow_state == FlowState.SERVICE_CATALOG
        assert nxt.flow_step == STEP_AWAITING_SERVICE
        assert all(not row[0].startswith("prof|") for row in nxt.bubbles[0].rows)


async def test_legacy_snapshot_without_catalog_asks_but_never_marks():
    """No `insurance_plans` on the snapshot -> legacy strings: asked, zero marks."""
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    del tenant.insurance_plans
    tenant.insurances = ["Unimed", "Amil"]
    insurance_q = await _to_insurance_list(tenant, professionals)
    assert insurance_q.flow_step == STEP_AWAITING_INSURANCE
    doctors = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Unimed")), professionals
    )
    _assert_full_list_with_marks(doctors, professionals, expected_marked=[])
    assert doctors.flow_selected_insurance == "Unimed"


# --------------------------------------------------------------------------
# 3. Particular / Outro convênio -> everyone, no marks
# --------------------------------------------------------------------------


async def test_particular_lists_all_doctors_without_marks():
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    insurance_q = await _to_insurance_list(tenant, professionals)
    doctors = await _turn(
        _advance(insurance_q),
        tenant,
        _tap(_row_titled(insurance_q, LABEL_INSURANCE_PARTICULAR)),
        professionals,
    )
    _assert_full_list_with_marks(doctors, professionals, expected_marked=[])
    assert doctors.flow_selected_insurance == LABEL_INSURANCE_PARTICULAR


async def test_other_plan_typed_name_lists_all_doctors_without_marks():
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    insurance_q = await _to_insurance_list(tenant, professionals)
    asked_name = await _turn(
        _advance(insurance_q),
        tenant,
        _tap(_row_titled(insurance_q, LABEL_INSURANCE_OTHER)),
        professionals,
    )
    assert asked_name.flow_step == STEP_AWAITING_INSURANCE
    assert isinstance(asked_name.bubbles[0], TextBubble)

    doctors = await _turn(_advance(asked_name), tenant, "Plano Saúde Local", professionals)
    _assert_full_list_with_marks(doctors, professionals, expected_marked=[])
    assert doctors.flow_selected_insurance == "Plano Saúde Local"


# --------------------------------------------------------------------------
# 4. Order after convênio, and the answer carried on every step
# --------------------------------------------------------------------------


@pytest.mark.parametrize("doctor_index", [0, 3], ids=["marked-doctor", "unmarked-doctor"])
async def test_order_after_insurance_reaches_day_not_insurance_again(doctor_index):
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    doctor = professionals[doctor_index]

    insurance_q = await _to_insurance_list(tenant, professionals)
    doctors = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Unimed")), professionals
    )
    assert doctors.flow_selected_insurance == "Unimed"

    # doctor tap -> THAT doctor's service list
    services = await _turn(
        _advance(doctors), tenant, _tap(_prof_row(doctors, doctor)), professionals
    )
    assert services.flow_step == STEP_AWAITING_SERVICE
    assert services.flow_selected_professional_id == doctor.id
    assert services.flow_selected_insurance == "Unimed"
    service_rows = [row for row in services.bubbles[0].rows if row[0].startswith("svc|")]
    expected_service = "Consulta Cardio" if doctor_index == 0 else "Consulta Geral"
    assert [row[0] for row in service_rows] == [f"svc|{expected_service}"]

    # service -> detail card
    detail = await _turn(_advance(services), tenant, _tap(service_rows[0]), professionals)
    assert detail.flow_step == STEP_AWAITING_SERVICE_CONFIRM
    assert detail.flow_selected_type == expected_service
    assert detail.flow_selected_professional_id == doctor.id
    assert detail.flow_selected_insurance == "Unimed"

    # "Sim, agendar" -> the DAY step, never the convênio again
    day = await _turn(_advance(detail), tenant, LABEL_BOOK_SERVICE, professionals)
    assert day.flow_step == STEP_AWAITING_DAY
    assert day.flow_step != STEP_AWAITING_INSURANCE
    assert day.flow_selected_insurance == "Unimed"
    assert day.flow_selected_type == expected_service
    assert day.flow_selected_professional_id == doctor.id


async def test_insurance_survives_to_the_booked_appointment():
    """day -> slot -> Confirmar: the appointment row carries the convênio."""
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    ana = professionals[0]

    result = await _to_insurance_list(tenant, professionals)
    result = await _turn(
        _advance(result), tenant, _tap(_row_titled(result, "Unimed")), professionals
    )
    result = await _turn(_advance(result), tenant, _tap(_prof_row(result, ana)), professionals)
    result = await _turn(
        _advance(result),
        tenant,
        _tap(_row(result, lambda row: row[0].startswith("svc|"))),
        professionals,
    )
    result = await _turn(_advance(result), tenant, LABEL_BOOK_SERVICE, professionals)
    assert result.flow_step == STEP_AWAITING_DAY
    result = await _turn(_advance(result), tenant, _tap(result.bubbles[0].rows[0]), professionals)
    assert result.flow_step == STEP_AWAITING_SLOT
    assert result.flow_selected_insurance == "Unimed"
    result = await _turn(_advance(result), tenant, _tap(result.bubbles[0].rows[0]), professionals)
    assert result.flow_step == STEP_AWAITING_CONFIRMATION
    assert result.flow_selected_insurance == "Unimed"
    booked = await _turn(_advance(result), tenant, "Confirmar", professionals, patient_name="João")
    assert booked.appointment is not None
    assert booked.appointment["insurance"] == "Unimed"
    assert booked.appointment["professional_id"] == ana.id


async def test_single_doctor_order_insurance_then_services_then_day():
    """1 doctor: convênio -> service list (no doctor list) -> detail -> day."""
    professionals = [_doctor("Dra. Única", "Clínica geral")]
    tenant = _tenant(accepted_by={professionals[0].id: {UNIMED["id"]}})
    insurance_q = await _to_insurance_list(tenant, professionals)
    assert insurance_q.flow_step == STEP_AWAITING_INSURANCE
    services = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Unimed")), professionals
    )
    assert services.flow_step == STEP_AWAITING_SERVICE
    assert services.flow_selected_insurance == "Unimed"
    detail = await _turn(
        _advance(services),
        tenant,
        _tap(_row(services, lambda row: row[0].startswith("svc|"))),
        professionals,
    )
    assert detail.flow_step == STEP_AWAITING_SERVICE_CONFIRM
    assert detail.flow_selected_insurance == "Unimed"
    day = await _turn(_advance(detail), tenant, LABEL_BOOK_SERVICE, professionals)
    assert day.flow_step == STEP_AWAITING_DAY
    assert day.flow_selected_insurance == "Unimed"


# --------------------------------------------------------------------------
# 5. enter_guided_booking (LLM hand-back)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("professional_count", [1, 3])
async def test_guided_booking_unanswered_insurance_is_asked_then_day(professional_count):
    professionals = [_doctor(f"Dr(a). {i}") for i in range(professional_count)]
    tenant = _tenant()
    selected = professionals[0].id if professional_count > 1 else None
    asked = await enter_guided_booking(
        tenant,
        _FakeCalendar(),
        "Consulta Geral",
        conversation_id=uuid4(),
        professional_id=selected,
        professionals=professionals,
    )
    assert asked.flow_step == STEP_AWAITING_INSURANCE
    assert asked.flow_selected_type == "Consulta Geral"
    assert asked.flow_selected_professional_id == selected

    answered = await _turn(
        _advance(asked), tenant, _tap(_row_titled(asked, "Unimed")), professionals
    )
    # The service is already chosen: the answer goes to the day, NOT to the doctor list.
    assert answered.flow_step == STEP_AWAITING_DAY
    assert answered.flow_step != STEP_AWAITING_PROFESSIONAL
    assert answered.flow_selected_insurance == "Unimed"
    assert answered.flow_selected_type == "Consulta Geral"
    assert answered.flow_selected_professional_id == selected


async def test_guided_booking_already_answered_goes_straight_to_day():
    professionals = [_doctor("Dra. Única")]
    res = await enter_guided_booking(
        _tenant(),
        _FakeCalendar(),
        "Consulta Geral",
        conversation_id=uuid4(),
        insurance="Unimed",
        professionals=professionals,
    )
    assert res.flow_step == STEP_AWAITING_DAY
    assert res.flow_selected_insurance == "Unimed"
    assert res.flow_selected_type == "Consulta Geral"


@pytest.mark.parametrize("plans", [CLINIC_PLANS, []], ids=["plans", "no-plans"])
async def test_guided_booking_clinic_not_collecting_goes_to_day(plans):
    professionals = [_doctor("Dra. Única")]
    res = await enter_guided_booking(
        _tenant(collect_insurance=False, plans=plans),
        _FakeCalendar(),
        "Consulta Geral",
        conversation_id=uuid4(),
        professionals=professionals,
    )
    assert res.flow_step == STEP_AWAITING_DAY
    assert res.flow_selected_insurance is None


# --------------------------------------------------------------------------
# Reviewer findings (TASK-006 REVIEW.md): typeless hand-back, empty answer
# --------------------------------------------------------------------------


async def test_guided_booking_without_a_catalog_still_books_typeless_after_the_convenio():
    """A clinic with no services: `start_guided_booking` hands back with no
    type. The convênio answer must continue to the day (typeless, as before
    the convênio moved to the front) - never "não há serviços"."""
    doctor = _doctor("Dra. Única", services=[])
    tenant = _tenant()
    tenant.appointment_types = []
    asked = await enter_guided_booking(
        tenant, _FakeCalendar(), None, conversation_id=uuid4(), professionals=[doctor]
    )
    assert asked.flow_step == STEP_AWAITING_INSURANCE
    answered = await _turn(_advance(asked), tenant, _tap(_row_titled(asked, "Unimed")), [doctor])
    assert answered.flow_state == FlowState.SERVICE_CATALOG
    assert answered.flow_step == STEP_AWAITING_DAY
    assert answered.flow_selected_insurance == "Unimed"


async def test_empty_answer_at_the_convenio_step_re_asks_it_there():
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    asked = await _to_insurance_list(tenant, professionals)
    res = await _turn(_advance(asked), tenant, "   ", professionals)
    assert res.flow_step == STEP_AWAITING_INSURANCE
    assert res.flow_selected_insurance is None
    assert isinstance(res.bubbles[0], SlotsBubble)
