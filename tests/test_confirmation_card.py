"""The confirmation card names the doctor on switched-on clinics (TASK-030 P3, spec §4.4.2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

from secretaria.ai.formatter import ButtonBubble  # noqa: E402
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router as fr  # noqa: E402
from tests.test_flow_router import _conversation, _FakeCalendar, _tenant  # noqa: E402

TAP = "🗓️ 08:00 (2026-10-05T08:00)"


def _doctor(name):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        context_doctor_message=None,
        appointment_types=None,
        business_hours=None,
    )


ANA = _doctor("Dra. Ana")
BETO = _doctor("Dr. Beto")


def _switched():
    tenant = _tenant()
    tenant.initial_flows = {**tenant.initial_flows, "ai_draft_v2": True}
    return tenant


def _at_slot(**kw):
    return _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SLOT,
        flow_selected_type="Primeira Consulta",
        flow_selected_day="2026-10-05",
        **kw,
    )


async def test_without_the_switch_the_card_is_byte_for_byte_todays():
    res = await fr.route(
        _at_slot(flow_selected_professional_id=ANA.id),
        _tenant(),
        _FakeCalendar(),
        TAP,
        professionals=[ANA, BETO],
    )
    assert res.flow_step == fr.STEP_AWAITING_CONFIRMATION
    assert res.bubbles[0].body == "Primeira Consulta\n05/10/2026 às 08:00"


async def test_with_the_switch_the_card_names_the_chosen_doctor():
    res = await fr.route(
        _at_slot(flow_selected_professional_id=ANA.id),
        _switched(),
        _FakeCalendar(),
        TAP,
        professionals=[ANA, BETO],
    )
    assert res.bubbles[0].body == "Primeira Consulta\nProfissional: Dra. Ana\n05/10/2026 às 08:00"
    assert res.flow_selected_slot == "2026-10-05T08:00"


async def test_a_single_professional_clinic_names_its_only_doctor():
    res = await fr.route(_at_slot(), _switched(), _FakeCalendar(), TAP, professionals=[ANA])
    assert res.bubbles[0].body == "Primeira Consulta\nProfissional: Dra. Ana\n05/10/2026 às 08:00"


async def test_the_doctor_comes_before_the_patient_line():
    res = await fr.route(
        _at_slot(flow_selected_professional_id=ANA.id, flow_attendee_name="Maria da Silva"),
        _switched(),
        _FakeCalendar(),
        TAP,
        professionals=[ANA, BETO],
    )
    assert res.bubbles[0].body == (
        "Primeira Consulta\nProfissional: Dra. Ana\nPaciente: Maria da Silva\n05/10/2026 às 08:00"
    )


async def test_a_clinic_without_professionals_has_no_doctor_line():
    res = await fr.route(_at_slot(), _switched(), _FakeCalendar(), TAP, professionals=[])
    assert res.bubbles[0].body == "Primeira Consulta\n05/10/2026 às 08:00"


async def test_the_resumed_card_reads_like_the_tapped_one():
    conversation = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Primeira Consulta",
        flow_selected_day="2026-10-05",
        flow_selected_slot="2026-10-05T08:00",
        flow_selected_professional_id=ANA.id,
    )
    res = await fr.resume_bubbles(conversation, _switched(), None, professionals=[ANA, BETO])
    assert res.bubbles[0].body == "Primeira Consulta\nProfissional: Dra. Ana\n05/10/2026 às 08:00"


def test_the_card_body_never_exceeds_the_interactive_limit():
    conversation = SimpleNamespace(flow_selected_type="X" * 2000, flow_attendee_name=None)
    card = fr._confirmation_card(conversation, datetime(2026, 10, 5, 8, 0), professional=ANA)
    assert isinstance(card, ButtonBubble)
    assert len(card.body) == MAX_INTERACTIVE_BODY_CHARS
    assert (card.confirm_label, card.cancel_label) == (fr.LABEL_CONFIRM, fr.LABEL_CANCEL)
