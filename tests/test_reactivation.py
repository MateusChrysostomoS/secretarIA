"""Tests for the returning-patient reactivation ("welcome back" / resume).

Pure-logic, no DB / network — mirrors test_flow_router.py: the conversation,
tenant and patient are SimpleNamespace stand-ins and the calendar is faked. The
worker DB path (_persist_inbound_message / _send_bot_reply) is exercised manually
(see the plan's verification section), as the suite has no live Postgres.
"""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai.formatter import ButtonBubble, SlotsBubble, TextBubble  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    DEFAULT_CONTINUE_PROMPT,
    DEFAULT_REACTIVATION_GAP_MINUTES,
    LABEL_BOOK,
    LABEL_MANAGE_APPOINTMENT,
    LABEL_OTHER,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_DAY,
    STEP_AWAITING_RETRY,
    STEP_AWAITING_SERVICE,
    STEP_AWAITING_SERVICE_CONFIRM,
    STEP_AWAITING_SLOT,
    MenuBubble,
    classify_yes_no,
    reactivation_choice_buttons,
    reactivation_continue_prompt,
    reactivation_enabled,
    reactivation_gap_minutes,
    resume_bubbles,
)
from secretaria.workers.tasks import (  # noqa: E402
    _as_utc,
    _reactivation_offer,
)

SERVICE_NAME = "Primeira Consulta"


def _tenant(reactivation=None, **overrides):
    initial_flows = {
        "enabled": True,
        "buttons": ["Serviços e Custo", "Horários", "Outro"],
        "menu_label": "Como posso ajudar?",
    }
    if reactivation is not None:
        initial_flows["reactivation"] = reactivation
    base = dict(
        initial_flows=initial_flows,
        returning_greeting_message="Oi de novo, {{name}}!",
        greeting_message="Olá! Bem-vindo.",
        greeting_buttons=["Agendar", "Horários"],
        appointment_types=[
            {
                "name": SERVICE_NAME,
                "duration_min": 40,
                "is_active": True,
                "sort_order": 0,
                "price": "R$ 250",
                "long_description": "Avaliação completa.",
            }
        ],
        appointment_duration_min=30,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _conversation(**kw):
    base = dict(
        id=uuid4(),
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
        reactivation_origin=None,
        patient_id=uuid4(),
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _patient(name="Maria"):
    return SimpleNamespace(id=uuid4(), name=name, wa_id="5511999")


class _FakeCalendar:
    def __init__(self, slots=None):
        self._slots = slots or []
        self.tzinfo = ZoneInfo("America/Sao_Paulo")

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        return self._slots


# --------------------------------------------------------------------------
# Config helpers
# --------------------------------------------------------------------------


def test_reactivation_disabled_by_default():
    assert reactivation_enabled(_tenant()) is False


def test_reactivation_enabled_when_configured():
    assert reactivation_enabled(_tenant(reactivation={"enabled": True})) is True


def test_gap_minutes_default_and_custom():
    assert reactivation_gap_minutes(_tenant()) == DEFAULT_REACTIVATION_GAP_MINUTES
    assert reactivation_gap_minutes(_tenant(reactivation={"gap_minutes": 30})) == 30


def test_gap_minutes_falls_back_on_garbage():
    assert (
        reactivation_gap_minutes(_tenant(reactivation={"gap_minutes": "abc"}))
        == DEFAULT_REACTIVATION_GAP_MINUTES
    )


def test_continue_prompt_default_and_custom():
    assert reactivation_continue_prompt(_tenant()) == DEFAULT_CONTINUE_PROMPT
    assert (
        reactivation_continue_prompt(_tenant(reactivation={"continue_prompt": "Segue?"}))
        == "Segue?"
    )


def test_choice_buttons_default_and_truncate():
    assert reactivation_choice_buttons(_tenant()) == ["Sim", "Não"]
    buttons = reactivation_choice_buttons(
        _tenant(reactivation={"buttons": ["A", "B", "C", "D"]})
    )
    assert buttons == ["A", "B", "C"]  # capped at 3


# --------------------------------------------------------------------------
# classify_yes_no
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "body,expected",
    [
        ("Sim", "yes"),
        ("sim", "yes"),
        ("  SIM  ", "yes"),
        ("Não", "no"),
        ("nao", "other"),  # default labels are exact; "nao" != "Não"
        ("Talvez", "other"),
        ("", "other"),
        (None, "other"),
    ],
)
def test_classify_yes_no_default_labels(body, expected):
    assert classify_yes_no(body, _tenant()) == expected


def test_classify_yes_no_custom_labels():
    tenant = _tenant(reactivation={"buttons": ["Continuar", "Recomeçar"]})
    assert classify_yes_no("Continuar", tenant) == "yes"
    assert classify_yes_no("Recomeçar", tenant) == "no"
    assert classify_yes_no("Sim", tenant) == "other"


def test_classify_yes_no_matches_20char_truncation():
    # WhatsApp truncates button titles to 20 chars; the tap echoes the prefix.
    long_no = "Não quero continuar agora mesmo"
    tenant = _tenant(reactivation={"buttons": ["Sim", long_no]})
    assert classify_yes_no(long_no[:20], tenant) == "no"


# --------------------------------------------------------------------------
# resume_bubbles
# --------------------------------------------------------------------------


async def test_resume_menu_state_reshows_menu():
    result = await resume_bubbles(_conversation(flow_state=FlowState.MENU), _tenant(), None)
    assert result.action == "reply"
    assert result.flow_state == FlowState.MENU
    assert isinstance(result.bubbles[0], MenuBubble)


async def test_resume_awaiting_service_reshows_list():
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_SERVICE)
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.flow_state == FlowState.SERVICE_CATALOG
    assert result.flow_step == STEP_AWAITING_SERVICE
    assert isinstance(result.bubbles[0], SlotsBubble)


async def test_resume_service_confirm_reshows_detail():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type=SERVICE_NAME,
    )
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.flow_step == STEP_AWAITING_SERVICE_CONFIRM
    assert isinstance(result.bubbles[0], ButtonBubble)


async def test_resume_service_confirm_unknown_type_falls_back_to_list():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SERVICE_CONFIRM,
        flow_selected_type="No Such Service",
    )
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.flow_step == STEP_AWAITING_SERVICE  # reset back to the list
    assert isinstance(result.bubbles[0], SlotsBubble)


async def test_resume_awaiting_day_reasks():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_DAY,
        flow_selected_type=SERVICE_NAME,
    )
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.flow_step == STEP_AWAITING_DAY
    assert isinstance(result.bubbles[0], TextBubble)


async def test_resume_awaiting_slot_relists_fresh_slots():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SLOT,
        flow_selected_type=SERVICE_NAME,
        flow_selected_day="2026-06-15",
    )
    calendar = _FakeCalendar(slots=[{"start": "2026-06-15T08:00", "label": "08:00"}])
    result = await resume_bubbles(conv, _tenant(), calendar)
    assert result.flow_step == STEP_AWAITING_SLOT
    assert isinstance(result.bubbles[0], SlotsBubble)


async def test_resume_awaiting_slot_without_calendar_delegates():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_SLOT,
        flow_selected_type=SERVICE_NAME,
        flow_selected_day="2026-06-15",
    )
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.action == "delegate_llm"


async def test_resume_awaiting_confirmation_reshows_recap():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type=SERVICE_NAME,
        flow_selected_slot="2026-06-15T08:00",
    )
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.flow_step == STEP_AWAITING_CONFIRMATION
    assert isinstance(result.bubbles[0], ButtonBubble)


async def test_resume_awaiting_confirmation_without_slot_reasks_day():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type=SERVICE_NAME,
        flow_selected_slot=None,
    )
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.flow_step == STEP_AWAITING_DAY
    assert isinstance(result.bubbles[0], TextBubble)


async def test_resume_awaiting_retry_reshows_choice():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_RETRY
    )
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.flow_step == STEP_AWAITING_RETRY
    assert isinstance(result.bubbles[0], MenuBubble)


async def test_resume_unknown_step_reshows_menu():
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step="bogus")
    result = await resume_bubbles(conv, _tenant(), None)
    assert result.flow_state == FlowState.MENU
    assert isinstance(result.bubbles[0], MenuBubble)


# --------------------------------------------------------------------------
# _as_utc + _reactivation_offer
# --------------------------------------------------------------------------


def test_as_utc_normalises_naive_and_passes_aware():
    naive = datetime(2026, 6, 10, 12, 0, 0)
    assert _as_utc(naive).tzinfo is UTC
    aware = datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC)
    assert _as_utc(aware) is aware


def _offer_tenant():
    return _tenant(reactivation={"enabled": True, "gap_minutes": 360})


def test_offer_none_when_no_prior_activity():
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_DAY)
    assert _reactivation_offer(conv, _offer_tenant(), _patient(), "5511999", "oi", None) is None


def test_offer_none_when_gap_below_threshold():
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_DAY)
    recent = datetime.now(UTC) - timedelta(minutes=5)
    assert _reactivation_offer(conv, _offer_tenant(), _patient(), "5511999", "oi", recent) is None
    assert conv.reactivation_origin is None  # gate NOT armed


def test_offer_arms_gate_and_builds_prompt_for_resumable_state():
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_AWAITING_DAY)
    stale = datetime.now(UTC) - timedelta(hours=10)
    offer = _reactivation_offer(conv, _offer_tenant(), _patient("Maria"), "5511999", "oi", stale)
    assert offer is not None
    assert conv.reactivation_origin == FlowState.SERVICE_CATALOG.value  # gate armed
    assert "Oi de novo, Maria!" in offer.greeting_override
    assert DEFAULT_CONTINUE_PROMPT in offer.greeting_override
    assert offer.greeting_buttons == ["Sim", "Não"]
    assert offer.reactivation is None  # the OFFER reuses the greeting path


def test_offer_arms_gate_for_llm_origin():
    conv = _conversation(flow_state=FlowState.LLM)
    stale = datetime.now(UTC) - timedelta(hours=10)
    offer = _reactivation_offer(conv, _offer_tenant(), _patient(), "5511999", "oi", stale)
    assert offer is not None
    assert conv.reactivation_origin == FlowState.LLM.value


def test_offer_idle_sends_plain_greeting_and_menu_without_arming():
    conv = _conversation(flow_state=FlowState.IDLE)
    stale = datetime.now(UTC) - timedelta(hours=10)
    offer = _reactivation_offer(conv, _offer_tenant(), _patient("Ana"), "5511999", "oi", stale)
    assert offer is not None
    assert conv.reactivation_origin is None  # nothing to resume -> gate NOT armed
    assert offer.greeting_override == "Oi de novo, Ana!"
    # Fixed greeting trio (fixed-greeting-buttons round; consolidated to
    # Agendar/Gerenciar consulta/Outro in the trio-gerenciar round), not the
    # tenant's configured initial_flows.buttons menu anymore - see
    # docs/CHECKPOINT_fixed_greeting_buttons.md.
    assert offer.greeting_buttons == [LABEL_BOOK, LABEL_MANAGE_APPOINTMENT, LABEL_OTHER]


def test_offer_idle_without_any_greeting_returns_none():
    tenant = _tenant(
        reactivation={"enabled": True, "gap_minutes": 360},
        returning_greeting_message=None,
        greeting_message=None,
    )
    conv = _conversation(flow_state=FlowState.IDLE)
    stale = datetime.now(UTC) - timedelta(hours=10)
    assert _reactivation_offer(conv, tenant, _patient(), "5511999", "oi", stale) is None
