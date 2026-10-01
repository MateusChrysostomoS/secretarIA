"""O modo LLM não pode apagar o rascunho do agendamento (MVP Portal, Task 1)."""

import os
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.flow_router import LABEL_OTHER, route  # noqa: E402
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402


async def test_day_picker_outro_keeps_booking_draft():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=flow_router.BOOKING_DAY_BRANCH.day_escape_step,
        flow_selected_type="Primeira Consulta",
        flow_selected_insurance="Unimed",
        flow_attendee_name="Maria",
    )
    res = await route(conv, _tenant(), _FakeCalendar(), LABEL_OTHER)
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.LLM
    assert res.flow_selected_type == "Primeira Consulta"
    assert res.flow_selected_insurance == "Unimed"
    assert res.flow_attendee_name == "Maria"


async def test_menu_outro_keeps_existing_draft():
    pid = uuid4()
    conv = _conversation(
        flow_state=FlowState.IDLE,
        flow_selected_type="Primeira Consulta",
        flow_selected_professional_id=pid,
        flow_selected_insurance="Particular",
    )
    res = await route(conv, _tenant(), None, LABEL_OTHER)
    assert res.action == "delegate_llm"
    assert res.flow_selected_type == "Primeira Consulta"
    assert res.flow_selected_professional_id == pid
    assert res.flow_selected_insurance == "Particular"


async def test_llm_state_turn_keeps_type_day_and_attendee():
    conv = _conversation(
        flow_state=FlowState.LLM,
        flow_step="day",
        flow_selected_slot="2026-10-05T10:00:00-03:00",
        flow_selected_type="Primeira Consulta",
        flow_selected_day="2026-10-05",
        flow_selected_insurance="Unimed",
        flow_attendee_name="Maria",
    )
    res = await route(conv, _tenant(), None, "quero marcar")
    assert res.action == "delegate_llm"
    assert res.flow_selected_type == "Primeira Consulta"
    assert res.flow_selected_day == "2026-10-05"
    assert res.flow_selected_insurance == "Unimed"
    assert res.flow_attendee_name == "Maria"
    assert res.flow_step is None
    assert res.flow_selected_slot is None
