"""The router keeps "Marcar outra" only inside the booking (TASK-032 R3, spec §4.3)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402

ORIGINAL = uuid4()


def _in_booking(**kw):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_replaces_appointment_id=ORIGINAL, **kw
    )


def test_a_booking_step_keeps_the_marker():
    result = fr.FlowRouterResult(
        action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_step=fr.STEP_AWAITING_DAY
    )
    assert fr._carry_replacement(_in_booking(), result).flow_replaces_appointment_id == ORIGINAL


@pytest.mark.parametrize("state", [FlowState.LLM, FlowState.AWAITING_EMAIL_CODE])
def test_the_ai_and_the_portal_code_wait_keep_the_marker(state):
    result = fr.FlowRouterResult(action="delegate_llm", flow_state=state)
    assert fr._carry_replacement(_in_booking(), result).flow_replaces_appointment_id == ORIGINAL


@pytest.mark.parametrize("state", [FlowState.MENU, FlowState.IDLE, FlowState.MANAGE_BOOKING])
def test_leaving_the_booking_drops_the_marker(state):
    result = fr.FlowRouterResult(action="reply", flow_state=state)
    assert fr._carry_replacement(_in_booking(), result).flow_replaces_appointment_id is None


def test_a_marker_left_on_an_idle_conversation_never_reaches_a_new_booking():
    stale = _conversation(flow_state=FlowState.IDLE, flow_replaces_appointment_id=ORIGINAL)
    result = fr.FlowRouterResult(action="reply", flow_state=FlowState.SERVICE_CATALOG)
    assert fr._carry_replacement(stale, result).flow_replaces_appointment_id is None


def test_a_booked_result_never_carries_the_marker():
    result = fr.FlowRouterResult(
        action="reply", flow_state=FlowState.SERVICE_CATALOG, appointment={"google_event_id": "e"}
    )
    assert fr._carry_replacement(_in_booking(), result).flow_replaces_appointment_id is None


def test_a_marker_the_result_names_is_kept_as_named():
    other = uuid4()
    result = fr.FlowRouterResult(
        action="reply", flow_state=FlowState.SERVICE_CATALOG, flow_replaces_appointment_id=other
    )
    assert fr._carry_replacement(_conversation(), result).flow_replaces_appointment_id == other


async def test_route_from_idle_drops_a_stale_marker():
    result = await fr.route(
        _conversation(flow_replaces_appointment_id=ORIGINAL), _tenant(), None, fr.LABEL_BOOK
    )
    assert result.flow_state == FlowState.SERVICE_CATALOG
    assert result.flow_replaces_appointment_id is None


async def test_cancelar_on_the_card_keeps_the_marker_for_the_retry():
    conversation = _in_booking(
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_slot="2026-10-06T09:00:00-03:00",
    )
    result = await fr.route(conversation, _tenant(), _FakeCalendar(), fr.LABEL_CANCEL)
    assert result.flow_step == fr.STEP_AWAITING_RETRY
    assert result.flow_replaces_appointment_id == ORIGINAL


def test_the_confirmation_card_warns_that_the_old_appointment_goes():
    start = datetime(2026, 10, 6, 9, 0)
    marked = fr._recap_text(_in_booking(flow_selected_type="Consulta"), start)
    plain = fr._recap_text(_conversation(flow_selected_type="Consulta"), start)
    assert marked == "Consulta\n06/10/2026 às 09:00\n\n" + fr.REPLACEMENT_NOTICE
    assert plain == "Consulta\n06/10/2026 às 09:00"


async def test_the_idle_menu_is_the_generic_menu():
    result = await fr.route(_conversation(), _tenant(), None, "oi")
    assert result.flow_state == FlowState.MENU
    assert fr.is_generic_menu_result(result) is True


async def test_a_clear_request_is_not_the_generic_menu():
    result = await fr.route(_conversation(), _tenant(), None, fr.LABEL_BOOK)
    assert fr.is_generic_menu_result(result) is False


def test_the_decline_question_and_answer_are_not_the_generic_menu():
    assert fr.is_generic_menu_result(fr.enter_decline_reasons(uuid4())) is False
    answered = fr._handle_decline_reason(
        _conversation(flow_managing_appointment_id=uuid4()), "Não preciso mais"
    )
    assert fr.is_generic_menu_result(answered) is False
