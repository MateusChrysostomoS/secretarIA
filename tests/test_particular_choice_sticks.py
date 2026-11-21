"""TASK-088 A2: the patient's Particular choice travels with their booking."""

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest

from secretaria.models import Conversation, FlowState
from secretaria.schemas.webhook import inbound_routing_text
from secretaria.services import flow_router as fr, flow_units as fu
from secretaria.services.booking_draft import BookingDraft
from tests.test_booking_draft_resume_questions import (
    DAY as RESUME_DAY,
    FULL_DRAFT,
    _conversation,
    _hand_back,
    _onboard,
    _park,
    _seed_tenant,
    _wa_turn,
    db as _db_fixture,
    wired as _wired_fixture,
)
from tests.test_booking_order_compat import (
    _conv,
    _doc,
    _legacy_named,
    _resolve,
    _two_plans,
    _unit,
)
from tests.test_booking_order_service_first import (
    AMIL,
    BRADESCO,
    ECO_ID,
    UNIMED,
    _doctor,
    _list,
    _matrix,
    _tenant,
    _units,
    _Walk,
)
from tests.test_flow_simulation import _Calendars, _tenant as _simulation_tenant, _turn

db = _db_fixture
wired = _wired_fixture


@pytest.mark.parametrize("provenance", [False, None])
async def test_chosen_particular_survives_draft_changing_service_doctor_and_unit(provenance):
    centro, sul = _unit("Centro"), _unit("Sul")
    ana, bruno = _doc("Dra. Ana", centro), _doc("Dr. Bruno", sul)
    conv = _conv(
        flow_state=FlowState.LLM,
        flow_selected_type="Retorno",
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=centro.id,
        flow_selected_insurance="Particular",
        flow_insurance_auto=provenance,
    )
    resolved = await _resolve(
        BookingDraft(service="Consulta", professional_id=bruno.id),
        conv=conv,
        tenant=_two_plans(),
        pros=[ana, bruno],
    )
    assert resolved.landing_step == fr.STEP_AWAITING_DAY
    assert resolved.result.flow_selected_unit_id == sul.id
    assert resolved.result.flow_selected_insurance == "Particular"
    assert resolved.result.flow_insurance_auto is provenance


async def test_chosen_particular_survives_back_from_a_unit_where_no_plan_is_accepted():
    centro, sul = _units()
    ana = _doctor(units=[centro, sul])
    tenant = _tenant(
        "shared",
        _matrix(
            "shared", ana,
            (ECO_ID, sul, UNIMED), (ECO_ID, sul, BRADESCO), (ECO_ID, sul, AMIL),
        ),
        [ana],
    )
    walk = _Walk(
        tenant, [ana],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type="Ecocardiograma",
        flow_selected_unit_id=sul.id,
        flow_selected_insurance="Particular",
        flow_insurance_auto=False,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    result = await walk.say(inbound_routing_text('Outra unidade', 'dayback|unit'))
    assert result.flow_step == fu.STEP_AWAITING_UNIT
    assert result.flow_selected_insurance == "Particular"
    assert result.flow_insurance_auto is False


async def test_named_particular_is_saved_as_chosen_when_parked_draft_resumes(wired):
    tenant = await _seed_tenant(wired, collect=True)
    await _onboard(tenant)
    await _hand_back(wired, tenant, FULL_DRAFT)
    parked = await _conversation(wired, tenant)
    assert parked.flow_step == fr.STEP_AWAITING_INSURANCE

    await _wa_turn(tenant, "não tenho")

    current = await _conversation(wired, tenant)
    assert current.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert current.flow_selected_insurance == "Particular"
    assert current.flow_insurance_auto is False


async def test_previously_chosen_particular_survives_a_parked_service_answer(wired):
    tenant = await _seed_tenant(wired, collect=True)
    await _onboard(tenant)
    await _park(
        wired,
        tenant,
        step=fr.STEP_AWAITING_SERVICE,
        draft=BookingDraft(day=RESUME_DAY, time=time(10)),
        saved_at=datetime.now(UTC),
    )
    parked = await _conversation(wired, tenant)
    async with wired() as session:
        row = await session.get(Conversation, parked.id)
        row.flow_selected_insurance = "Particular"
        row.flow_insurance_auto = False
        await session.commit()

    await _wa_turn(tenant, "Primeira Consulta")

    current = await _conversation(wired, tenant)
    assert current.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert current.flow_selected_insurance == "Particular"
    assert current.flow_insurance_auto is False
    assert current.flow_draft is None


async def test_legacy_ai_particular_choice_is_saved_as_chosen(wired):
    async def payload(_tenant):
        return {"t": "Primeira Consulta", "i": "particular"}

    landed = await _legacy_named(
        wired, [("Dra. Ana", ["Primeira Consulta", "Retorno"])], payload, stored_type="Retorno"
    )
    assert landed.flow_selected_insurance == "Particular"
    assert landed.flow_insurance_auto is False


async def test_simulator_carries_the_automatic_answer_then_clears_it_on_back():
    tenant = _simulation_tenant(collect_insurance=True, insurance_plans=[])
    calendars = _Calendars(tenant)
    state = {
        "flow_state": FlowState.SERVICE_CATALOG.value,
        "flow_step": fr.STEP_AWAITING_DAY,
        "flow_selected_type": "Primeira Consulta",
        "flow_selected_insurance": "Particular",
        "flow_insurance_auto": True,
        "flow_attendee_name": fr.ATTENDEE_SELF,
    }
    turn = await _turn(tenant, calendars, state=state, text="Outra data")
    assert turn.state["flow_selected_insurance"] == "Particular"
    assert turn.state["flow_insurance_auto"] is True
    back = next(
        opt for opt in turn.bubbles[-1]["options"]
        if opt["id"].startswith(fr.ROW_DAY_BACK_PREFIX)
    )
    rewind = await _turn(tenant, calendars, state=turn.state, tap=back)
    assert rewind.state["flow_step"] == fr.STEP_AWAITING_SERVICE
    assert rewind.state["flow_selected_insurance"] is None
    assert rewind.state["flow_insurance_auto"] is None



@pytest.mark.parametrize("automatic", [True, False])
async def test_catalog_changes_away_from_the_unit_where_particular_was_recorded(automatic):
    removed, replacement = _units()
    doctor = _doctor(units=[replacement])
    walk = _Walk(
        _tenant(doctors=[doctor]), [doctor],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type="Ecocardiograma",
        flow_selected_unit_id=removed.id,
        flow_selected_insurance="Particular",
        flow_insurance_auto=automatic,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    result = await walk.say(fr.LABEL_OTHER_DATE)
    assert result.flow_step == (
        fr.STEP_AWAITING_INSURANCE if automatic else fr.STEP_AWAITING_DAY
    )
    assert result.flow_selected_unit_id == replacement.id
    assert result.flow_selected_insurance == (None if automatic else "Particular")


async def test_draft_compares_the_recorded_unit_even_after_that_unit_is_removed():
    removed, replacement = _unit("Centro"), _unit("Sul")
    doctor = _doc("Dra. Ana", replacement)
    conv = _conv(
        flow_state=FlowState.LLM,
        flow_selected_type="Consulta",
        flow_selected_professional_id=doctor.id,
        flow_selected_unit_id=removed.id,
        flow_selected_insurance="Particular",
        flow_insurance_auto=True,
    )
    resolved = await _resolve(
        BookingDraft(service="Consulta"),
        conv=conv,
        tenant=_two_plans(),
        pros=[doctor],
    )
    assert resolved.landing_step == fr.STEP_AWAITING_INSURANCE
    assert resolved.result.flow_selected_unit_id == replacement.id
    assert resolved.result.flow_selected_insurance is None



async def _opening_a_change_picker():
    centro, _ = _units()
    doctor = _doctor(units=[centro])
    tenant = _tenant(doctors=[doctor])
    walk = _Walk(tenant, [doctor])
    await walk.to_service_card()
    await walk.tap("ins|particular")
    await walk.tap("dayback|insurance")
    assert walk.last.flow_step == fr.STEP_AWAITING_INSURANCE
    return walk, centro, doctor


async def test_continuing_an_open_insurance_picker_keeps_the_previous_choice():
    walk, _, _ = await _opening_a_change_picker()
    reopened = await fr.resume_bubbles(walk.conversation(), walk.tenant, walk.agenda, walk.doctors)
    assert reopened.flow_step == fr.STEP_AWAITING_INSURANCE
    assert reopened.flow_selected_insurance == "Particular"
    assert reopened.flow_insurance_auto is False


async def test_empty_reply_to_stale_insurance_picker_does_not_convert_chosen_particular():
    walk, centro, doctor = await _opening_a_change_picker()
    walk.tenant.insurance_refused = _matrix(
        "shared", doctor,
        (ECO_ID, centro, UNIMED), (ECO_ID, centro, BRADESCO), (ECO_ID, centro, AMIL),
    )
    reopened = await walk.say("")
    assert reopened.flow_step == fr.STEP_AWAITING_DAY
    assert reopened.flow_selected_insurance == "Particular"
    assert reopened.flow_insurance_auto is False



async def test_draft_compares_the_recorded_doctor_even_after_that_doctor_is_removed():
    # A different doctor is now the only one in the clinic, both before/after had no units.
    replacement = _doc("Dra. A")
    conv = _conv(
        flow_state=FlowState.LLM,
        flow_selected_type="Consulta",
        flow_selected_professional_id=uuid4(),
        flow_selected_insurance="Particular",
        flow_insurance_auto=True,
    )
    resolved = await _resolve(
        BookingDraft(service="Consulta"),
        conv=conv,
        tenant=_two_plans(),
        pros=[replacement],
    )
    assert resolved.landing_step == fr.STEP_AWAITING_INSURANCE
    assert resolved.result.flow_selected_insurance is None



async def test_last_unit_removed_clears_pick_and_allows_a_date_tap():
    removed, _ = _units()
    doctor = _doctor(units=[])
    tenant = _tenant("independent", doctors=[doctor], accepted_by={})
    walk = _Walk(
        tenant, [doctor],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type="Ecocardiograma",
        flow_selected_unit_id=removed.id,
        flow_selected_insurance="Particular",
        flow_insurance_auto=True,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    day = await walk.say(fr.LABEL_OTHER_DATE)
    assert day.flow_step == fr.STEP_AWAITING_DAY
    assert day.flow_selected_unit_id is None
    assert day.flow_insurance_auto is True

    await walk.tap(_list(day).rows[0][0])

    assert walk.last.flow_step == fr.STEP_AWAITING_SLOT
    assert walk.last.flow_selected_unit_id is None


async def test_first_implied_unit_added_rejudges_automatic_particular():
    added, _ = _units()
    doctor = _doctor(units=[added])
    walk = _Walk(
        _tenant(doctors=[doctor]), [doctor],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_type="Ecocardiograma",
        flow_selected_insurance="Particular",
        flow_insurance_auto=True,
        flow_attendee_name=fr.ATTENDEE_SELF,
    )
    reopened = await fr.resume_bubbles(walk.conversation(), walk.tenant, walk.agenda, walk.doctors)
    assert reopened.flow_step == fr.STEP_AWAITING_INSURANCE
    assert reopened.flow_selected_unit_id == added.id
    assert reopened.flow_selected_insurance is None

    tomorrow = (datetime.now(walk.agenda.tzinfo) + timedelta(days=1)).date()
    tap = inbound_routing_text(tomorrow.strftime("%d/%m"), f"day|{tomorrow.isoformat()}|0")
    result = await walk.say(tap)
    assert result.flow_step == fr.STEP_AWAITING_INSURANCE
    assert result.flow_selected_unit_id == added.id
    assert result.flow_selected_insurance is None
