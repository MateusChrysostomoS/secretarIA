"""TASK-006 part 1 - behaviour tests for the convênio on the booking flow.

Owner's order (2026-11-20, TASK-088): pra-quem -> serviço -> médico -> unidade -> convênio ->
dia -> horário. The convênio is the LAST question and its list shows only the plans accepted
for the combination (so a doctor's own plan list decides what is offered; a doctor who takes
none leads straight to Particular). It used to be the first question (owner, 2026-09-23), when
the answer only MARKED the doctors who take the plan (INSURANCE_ACCEPTED_MARK in the row
description); that marking code stays for callers that still hold an answer (an AI draft).

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
    LABEL_DONT_KNOW,
    LABEL_INSURANCE_OTHER,
    LABEL_INSURANCE_PARTICULAR,
    STEP_AWAITING_ATTENDEE_CHOICE,
    STEP_AWAITING_CATALOG_SERVICE,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_PROFESSIONAL,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SERVICE_CONFIRM,
    STEP_AWAITING_SERVICE_PROFESSIONAL,
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


def _tenant(*, collect_insurance=True, plans=None, accepted_by=None, insurance_mode="independent"):
    """Tenant snapshot as the worker builds it (catalog-backed convênios).

    TASK-008: `insurance_mode` must be non-None or `_insurance_step_skip_reason`
    now skips the whole step (SPEC §2 - no silent default). This file's fixture
    defaults to `"independent"` NOT because it models a doctor's own
    catalog-drawn list (structurally these snapshots still model one shared
    clinic list) - it is the one mode whose marking rule
    (`flow_router._accepts_plan`) matches what every test below already
    assumed: a professional ABSENT from `accepted_by` is simply not marked, no
    inheritance. `clinic_with_exceptions`' TASK-008 default (absent = inherits
    ALL clinic plans) would flip roughly half of this file's `not-accepts`
    expectations, and is exercised on its own, correctly, in
    tests/test_insurance_mode_matrix.py instead.
    """
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
        insurance_mode=insurance_mode,
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


async def _after_attendee(
    tenant, professionals, entry=LABEL_BOOK, start_state=FlowState.IDLE
):
    """Entry tap -> pra-quem "Sim". Returns the result after pra-quem (the booking's first list)."""
    asked = await _turn(_conversation(flow_state=start_state), tenant, entry, professionals)
    assert asked.flow_step == STEP_AWAITING_ATTENDEE_CHOICE, asked
    return await _turn(_advance(asked), tenant, LABEL_ATTENDEE_SELF, professionals)


async def _to_card(
    tenant, professionals, service="Consulta Geral", doctor=None, entry=LABEL_BOOK,
    start_state=FlowState.IDLE,
):
    """Entry -> pra-quem -> the SERVICE -> [the doctor, when several do it] -> the step after it.

    TASK-088 Task 8: there is no service card any more - the tap (or the doctor's) goes straight
    on: the convênio list (these doctors have no units), or the days when nothing is asked."""
    result = await _after_attendee(tenant, professionals, entry, start_state)
    result = await _turn(
        _advance(result), tenant, _tap(_row(result, lambda r: r[0] == f"svc|{service}")),
        professionals,
    )
    if result.flow_step == STEP_AWAITING_SERVICE_PROFESSIONAL:
        assert doctor is not None, "several doctors do it: name the one to tap"
        result = await _turn(
            _advance(result), tenant, _tap(_prof_row(result, doctor)), professionals
        )
    assert result.flow_step != STEP_AWAITING_SERVICE_CONFIRM, result
    return result


async def _to_insurance_list(tenant, professionals, **kw):
    """... -> where the convênio is asked (TASK-088: after the service, the doctor and the unit
    - these doctors have no units)."""
    return await _to_card(tenant, professionals, **kw)


def _doctor_rows(result):
    """The `prof|` rows of a doctor list (the fixed "Não sei" row excluded)."""
    return [row for row in result.bubbles[0].rows if row[0].startswith("prof|")]


def _marked_ids(result) -> set[str]:
    return {
        row[0].removeprefix("prof|")
        for row in _doctor_rows(result)
        if row[2] and str(row[2]).startswith(INSURANCE_ACCEPTED_MARK)
    }


def _plan_rows(result) -> list[str]:
    """The titles of a convênio list's rows, in order."""
    assert result.flow_step == STEP_AWAITING_INSURANCE, result
    # (the booking's back row - TASK-088 R18 - is not a plan row)
    return [row[1] for row in result.bubbles[0].rows if not row[0].startswith("dayback|")]


def _assert_full_list_with_marks(result, professionals, expected_marked):
    """The doctor list for a CALLER THAT STILL KNOWS THE CONVÊNIO (an AI draft, a parked chat):
    every doctor listed, marked exactly when accepting (marks are never the normal path any
    more - TASK-088 - but the marking code stays for those callers).

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
# 1. Symptom test - the convênio list is what THE DOCTOR accepts
# --------------------------------------------------------------------------
#
# TASK-088 (owner, 2026-11-20): the convênio is asked LAST, after service, doctor and unit, and
# its list shows only the plans accepted for that combination. What used to be a mark on the
# doctor list ("aceita seu convênio") is now the list itself; a doctor who takes none of the
# clinic's plans never reaches a list at all (Particular direct).


@pytest.mark.parametrize("entry", [BTN_CHOOSE_PROFESSIONAL, BTN_CHOOSE_SERVICE])
async def test_symptom_the_list_is_the_plans_the_doctor_accepts(entry):
    professionals, accepted = _roster()
    ana, bruno, carla, davi = professionals
    tenant = _tenant(accepted_by=accepted)

    # MENU -> "Escolher serviço" (or the retired "Escolher médico") -> pra-quem "Sim": the SERVICE
    # list comes first, with the clinic's whole catalog - never the convênio.
    first = await _after_attendee(tenant, professionals, entry=entry, start_state=FlowState.MENU)
    assert first.flow_step == STEP_AWAITING_CATALOG_SERVICE
    assert [row[0] for row in first.bubbles[0].rows] == [
        "svc|Consulta Cardio",
        "svc|Consulta Geral",
    ]
    assert first.flow_selected_insurance is None

    # Ana (Cardio only she does): Unimed and Amil, in the clinic's order.
    at_ana = await _to_insurance_list(
        tenant, professionals, service="Consulta Cardio", entry=entry, start_state=FlowState.MENU
    )
    assert _plan_rows(at_ana) == [
        "Amil",
        "Unimed",
        LABEL_INSURANCE_PARTICULAR,
        LABEL_INSURANCE_OTHER,
    ]
    assert at_ana.flow_selected_professional_id == ana.id

    # The Geral doctors: each gets his own list.
    for doctor, expected in ((bruno, ["Amil"]), (carla, ["Unimed"])):
        at_doctor = await _to_insurance_list(tenant, professionals, doctor=doctor)
        assert _plan_rows(at_doctor) == [
            *expected,
            LABEL_INSURANCE_PARTICULAR,
            LABEL_INSURANCE_OTHER,
        ]
        assert at_doctor.flow_selected_professional_id == doctor.id


async def test_symptom_a_doctor_who_takes_no_plan_goes_particular_direct():
    professionals, accepted = _roster()
    davi = professionals[3]
    tenant = _tenant(accepted_by=accepted)
    day = await _to_insurance_list(tenant, professionals, doctor=davi)
    # No list, no question: the days, as Particular, with one short line saying why.
    assert day.flow_step == STEP_AWAITING_DAY
    assert day.flow_selected_insurance == LABEL_INSURANCE_PARTICULAR
    assert day.bubbles[0].body.startswith("Para esse serviço, o atendimento é particular.")


async def test_the_doctor_list_of_a_service_is_only_who_does_it_with_no_marks():
    professionals, accepted = _roster()
    ana, bruno, carla, davi = professionals
    tenant = _tenant(accepted_by=accepted)
    result = await _after_attendee(tenant, professionals)
    doctors = await _turn(
        _advance(result),
        tenant,
        _tap(_row(result, lambda r: r[0] == "svc|Consulta Geral")),
        professionals,
    )
    assert doctors.flow_step == STEP_AWAITING_SERVICE_PROFESSIONAL
    # Ana does only Cardio: not listed for Geral. Nobody is marked (the convênio is not known yet).
    assert [row[0] for row in doctors.bubbles[0].rows] == [
        f"prof|{bruno.id}",
        f"prof|{carla.id}",
        f"prof|{davi.id}",
    ]
    assert _marked_ids(doctors) == set()
    assert "aceita" not in doctors.bubbles[0].body.lower()


async def test_a_caller_that_knows_the_convenio_still_gets_the_marked_doctor_list():
    """The AI draft's doctor question (and any parked chat) still passes the answer it holds."""
    professionals, accepted = _roster()
    ana, bruno, carla, _davi = professionals
    tenant = _tenant(accepted_by=accepted)
    unimed = flow_router._enter_professional_list(tenant, professionals, insurance="Unimed")
    _assert_full_list_with_marks(unimed, professionals, expected_marked=[ana, carla])
    assert "Unimed" in unimed.bubbles[0].body
    amil = flow_router._enter_professional_list(tenant, professionals, insurance="Amil")
    _assert_full_list_with_marks(amil, professionals, expected_marked=[ana, bruno])
    nobody = flow_router._enter_professional_list(tenant, professionals, insurance="Bradesco Saúde")
    _assert_full_list_with_marks(nobody, professionals, expected_marked=[])


async def test_bite_checker_catches_a_list_that_ignores_the_doctor(monkeypatch):
    """Proves the symptom test bites: with the acceptance check neutered, the list is wrong."""
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    monkeypatch.setattr(flow_router, "_accepts_plan", lambda *a, **k: True)
    at_ana = await _to_insurance_list(
        tenant, professionals, service="Consulta Cardio", start_state=FlowState.MENU,
        entry=BTN_CHOOSE_SERVICE,
    )
    assert "Bradesco Saúde" in _plan_rows(at_ana)
    assert _plan_rows(at_ana) != [
        "Amil",
        "Unimed",
        LABEL_INSURANCE_PARTICULAR,
        LABEL_INSURANCE_OTHER,
    ]


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
    doctor = professionals[0] if professionals else None
    asks_insurance = collect and plans_filled
    multi = professional_count >= 2

    after_attendee = await _after_attendee(tenant, professionals)
    # The first list is the SERVICE's, whatever the clinic: the catalog (several doctors) or the
    # one doctor's / the clinic's own list.
    assert after_attendee.flow_step == (
        STEP_AWAITING_CATALOG_SERVICE if multi else STEP_AWAITING_SERVICE
    )
    assert after_attendee.flow_selected_insurance is None
    assert all(not row[0].startswith("prof|") for row in after_attendee.bubbles[0].rows)

    after_card = await _to_card(tenant, professionals, doctor=doctor)

    if not asks_insurance:
        # No convênio question at all: the service tap goes straight to the days.
        assert after_card.flow_step == STEP_AWAITING_DAY
        assert after_card.flow_selected_insurance is None
    elif doctor is not None and not accepts:
        # The doctor takes none of the plans: nothing to ask - Particular.
        assert after_card.flow_step == STEP_AWAITING_DAY
        assert after_card.flow_selected_insurance == LABEL_INSURANCE_PARTICULAR
    else:
        # The list: what the doctor accepts (no doctor row at all: every plan).
        expected = ["Unimed"] if doctor is not None else ["Amil", "Bradesco Saúde", "Unimed"]
        assert _plan_rows(after_card) == [
            *expected,
            LABEL_INSURANCE_PARTICULAR,
            LABEL_INSURANCE_OTHER,
        ]
        answered = await _turn(
            _advance(after_card),
            tenant,
            _tap(_row_titled(after_card, "Unimed")),
            professionals,
        )
        assert answered.flow_step == STEP_AWAITING_DAY
        assert answered.flow_selected_insurance == "Unimed"


async def test_legacy_snapshot_without_catalog_asks_and_lists_every_name():
    """No `insurance_plans` on the snapshot -> legacy strings: asked, nothing to judge them by."""
    professionals, accepted = _roster()
    ana = professionals[0]
    tenant = _tenant(accepted_by=accepted)
    del tenant.insurance_plans
    tenant.insurances = ["Unimed", "Amil"]
    insurance_q = await _to_insurance_list(tenant, professionals, service="Consulta Cardio")
    assert _plan_rows(insurance_q) == [
        "Unimed",
        "Amil",
        LABEL_INSURANCE_PARTICULAR,
        LABEL_INSURANCE_OTHER,
    ]
    day = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Unimed")), professionals
    )
    assert day.flow_step == STEP_AWAITING_DAY
    assert day.flow_selected_insurance == "Unimed"
    assert day.flow_selected_professional_id == ana.id


# --------------------------------------------------------------------------
# 3. Particular / Outro convênio -> the days
# --------------------------------------------------------------------------


async def test_particular_goes_to_the_days():
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    insurance_q = await _to_insurance_list(tenant, professionals, service="Consulta Cardio")
    day = await _turn(
        _advance(insurance_q),
        tenant,
        _tap(_row_titled(insurance_q, LABEL_INSURANCE_PARTICULAR)),
        professionals,
    )
    assert day.flow_step == STEP_AWAITING_DAY
    assert day.flow_selected_insurance == LABEL_INSURANCE_PARTICULAR


async def test_other_plan_typed_name_goes_to_the_days():
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    insurance_q = await _to_insurance_list(tenant, professionals, service="Consulta Cardio")
    asked_name = await _turn(
        _advance(insurance_q),
        tenant,
        _tap(_row_titled(insurance_q, LABEL_INSURANCE_OTHER)),
        professionals,
    )
    assert asked_name.flow_step == STEP_AWAITING_INSURANCE
    assert isinstance(asked_name.bubbles[0], TextBubble)

    day = await _turn(_advance(asked_name), tenant, "Plano Saúde Local", professionals)
    assert day.flow_step == STEP_AWAITING_DAY
    assert day.flow_selected_insurance == "Plano Saúde Local"


# --------------------------------------------------------------------------
# 4. Order of the steps, and the answer carried to the appointment
# --------------------------------------------------------------------------


@pytest.mark.parametrize("doctor_index", [0, 3], ids=["takes-plans", "takes-none"])
async def test_order_service_doctor_insurance_day(doctor_index):
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    doctor = professionals[doctor_index]
    service = "Consulta Cardio" if doctor_index == 0 else "Consulta Geral"

    catalog = await _after_attendee(tenant, professionals)
    assert catalog.flow_step == STEP_AWAITING_CATALOG_SERVICE
    picked = await _turn(
        _advance(catalog),
        tenant,
        _tap(_row(catalog, lambda row: row[0] == f"svc|{service}")),
        professionals,
    )
    if doctor_index == 0:
        # Exactly one doctor does it: straight on, the doctor already selected.
        detail = picked
    else:
        assert picked.flow_step == STEP_AWAITING_SERVICE_PROFESSIONAL
        assert picked.flow_selected_type == service
        detail = await _turn(
            _advance(picked), tenant, _tap(_prof_row(picked, doctor)), professionals
        )
    assert detail.flow_step != STEP_AWAITING_SERVICE_CONFIRM
    assert detail.flow_selected_type == service
    assert detail.flow_selected_professional_id == doctor.id

    # The pick -> the convênio (or, with nothing accepted, straight on) -> the DAY step.
    after_card = detail
    if doctor_index == 0:
        assert after_card.flow_step == STEP_AWAITING_INSURANCE
        day = await _turn(
            _advance(after_card),
            tenant,
            _tap(_row_titled(after_card, "Unimed")),
            professionals,
        )
        assert day.flow_selected_insurance == "Unimed"
    else:
        day = after_card
        assert day.flow_selected_insurance == LABEL_INSURANCE_PARTICULAR
    assert day.flow_step == STEP_AWAITING_DAY
    assert day.flow_selected_type == service
    assert day.flow_selected_professional_id == doctor.id


async def test_insurance_survives_to_the_booked_appointment():
    """convênio -> day -> slot -> Confirmar: the appointment row carries the convênio."""
    professionals, accepted = _roster()
    tenant = _tenant(accepted_by=accepted)
    ana = professionals[0]

    result = await _to_insurance_list(tenant, professionals, service="Consulta Cardio")
    result = await _turn(
        _advance(result), tenant, _tap(_row_titled(result, "Unimed")), professionals
    )
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


async def test_single_doctor_order_services_then_insurance_then_day():
    """1 doctor: service list (no doctor list) -> detail -> convênio -> day."""
    professionals = [_doctor("Dra. Única", "Clínica geral")]
    tenant = _tenant(accepted_by={professionals[0].id: {UNIMED["id"]}})
    services = await _after_attendee(tenant, professionals)
    assert services.flow_step == STEP_AWAITING_SERVICE
    assert services.flow_selected_insurance is None
    detail = await _turn(
        _advance(services),
        tenant,
        _tap(_row(services, lambda row: row[0].startswith("svc|"))),
        professionals,
    )
    assert detail.flow_step == STEP_AWAITING_INSURANCE  # TASK-088 Task 8: no card in between
    insurance_q = detail
    assert _plan_rows(insurance_q) == ["Unimed", LABEL_INSURANCE_PARTICULAR, LABEL_INSURANCE_OTHER]
    day = await _turn(
        _advance(insurance_q), tenant, _tap(_row_titled(insurance_q, "Unimed")), professionals
    )
    assert day.flow_step == STEP_AWAITING_DAY
    assert day.flow_selected_insurance == "Unimed"


# --------------------------------------------------------------------------
# 5. enter_guided_booking (LLM hand-back)
# --------------------------------------------------------------------------


def _accepting_unimed(professionals):
    return _tenant(accepted_by={p.id: {UNIMED["id"]} for p in professionals})


@pytest.mark.parametrize("professional_count", [1, 3])
async def test_guided_booking_unanswered_insurance_is_asked_then_day(professional_count):
    professionals = [_doctor(f"Dr(a). {i}") for i in range(professional_count)]
    tenant = _accepting_unimed(professionals)
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
        _accepting_unimed(professionals),
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
    tenant = _accepting_unimed([doctor])
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
    asked = await _to_insurance_list(tenant, professionals, service="Consulta Cardio")
    res = await _turn(_advance(asked), tenant, "   ", professionals)
    assert res.flow_step == STEP_AWAITING_INSURANCE
    assert res.flow_selected_insurance is None
    assert isinstance(res.bubbles[0], SlotsBubble)
