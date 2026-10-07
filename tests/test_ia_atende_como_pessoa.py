"""TASK-038 (owner, 2026-10-07): the secretary serves like a person, a human comes last.

Live Portal tests on 2026-10-06 (docs/CHECKPOINT_outro_pergunta_fixa.md §2) found:
a question at the convênio step stored AS the convênio; "Não entendi a data" for a
parking question and "posso TER desconto?" read as a Tuesday; the agent's words
dropped whenever it handed the patient back to the buttons (the same list again, as
if unheard); a robotic "Não sei" opener. These pin the four fixes
(docs/CHECKPOINT_ia_atende_como_pessoa.md).
"""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import pytest  # noqa: E402

from secretaria.ai.formatter import TextBubble  # noqa: E402
from secretaria.ai.graph import (  # noqa: E402
    BOOKING_DRAFT_SENTINEL_PREFIX,
    HANDBACK_INTRO_PREFIX,
    SHOW_MAIN_MENU_SENTINEL,
    split_handback_intro,
    with_handback_intro,
)
from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.ai.scoped_help import ScopedHelpOutcome  # noqa: E402
from secretaria.ai.tools import (  # noqa: E402
    HANDBACK_MESSAGE_MAX,
    BookingDraftRequested,
    ShowMainMenuRequested,
    set_booking_draft,
    show_main_menu,
)
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    DAY_NOT_UNDERSTOOD_PREFIX,
    LABEL_INSURANCE_PARTICULAR,
    STEP_AWAITING_DAY,
    STEP_AWAITING_DAY_RETRY,
    STEP_AWAITING_INSURANCE,
    STEP_AWAITING_SERVICE_CONFIRM,
    STEP_SERVICE_HELP,
    route,
)
from tests import (
    test_flow_day_picker as day_fixtures,  # noqa: E402
    test_flow_router_insurance as ins_fixtures,  # noqa: E402
)
from tests.test_flow_router import _conversation, _tenant  # noqa: E402
from tests.test_prompts import _config  # noqa: E402

_NOW = datetime(2026, 10, 7, 10, 0, tzinfo=ZoneInfo("America/Sao_Paulo"))


# --------------------------------------------------------------------------
# 1. Free text that is talk, not an answer, reaches the model on the same step
# --------------------------------------------------------------------------


async def test_a_question_at_the_insurance_step_is_answered_not_stored():
    """T4 of the live tests: "qual o endereço?" used to become the convênio."""
    tenant = ins_fixtures._tenant(collect_insurance=True, insurances=["Unimed"])
    res = await route(
        ins_fixtures._conversation(flow_step=STEP_AWAITING_INSURANCE),
        tenant,
        ins_fixtures._FakeCalendar(),
        "Antes disso, qual o endereço da clínica?",
        professionals=ins_fixtures._professionals(1),
    )
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_INSURANCE  # the list is still the answer
    assert res.flow_selected_insurance is None


@pytest.mark.parametrize("typed", ["não tenho", "Não", "sem convênio", "Nenhum."])
async def test_saying_no_insurance_is_particular(typed):
    tenant = ins_fixtures._tenant(collect_insurance=True, insurances=["Unimed"])
    res = await route(
        ins_fixtures._conversation(flow_step=STEP_AWAITING_INSURANCE),
        tenant,
        ins_fixtures._FakeCalendar(),
        typed,
        professionals=ins_fixtures._professionals(1),
    )
    assert res.flow_selected_insurance == LABEL_INSURANCE_PARTICULAR


async def test_a_typed_plan_name_is_still_stored():
    tenant = ins_fixtures._tenant(collect_insurance=True, insurances=["Unimed"])
    res = await route(
        ins_fixtures._conversation(flow_step=STEP_AWAITING_INSURANCE),
        tenant,
        ins_fixtures._FakeCalendar(),
        "Porto Seguro",
        professionals=ins_fixtures._professionals(1),
    )
    assert res.action == "reply"
    assert res.flow_selected_insurance == "Porto Seguro"


@pytest.mark.parametrize(
    "talk",
    [
        "Vocês têm estacionamento aí?",
        "Posso ter desconto se pagar à vista?",
        "posso ter desconto",
        "sei lá",
        "qualquer dia",
    ],
)
async def test_talk_at_the_day_step_gets_one_model_turn_on_the_same_step(talk):
    """T10/T11: a question is not a failed date - and "ter" in a sentence is not Tuesday."""
    res = await route(
        day_fixtures._conversation(flow_step=STEP_AWAITING_DAY),
        day_fixtures._tenant(),
        day_fixtures._Calendar(),
        talk,
    )
    assert res.action == "delegate_llm"
    assert res.flow_state == FlowState.SERVICE_CATALOG
    assert res.flow_step == STEP_AWAITING_DAY


@pytest.mark.parametrize("attempt", ["dia 35", "31/02"])
async def test_an_unreadable_date_attempt_still_gets_the_re_ask(attempt):
    res = await route(
        day_fixtures._conversation(flow_step=STEP_AWAITING_DAY),
        day_fixtures._tenant(),
        day_fixtures._Calendar(),
        attempt,
    )
    assert res.action == "reply"
    assert res.flow_step == STEP_AWAITING_DAY_RETRY
    assert res.bubbles[0].body.startswith(DAY_NOT_UNDERSTOOD_PREFIX)


@pytest.mark.parametrize(
    "answer, weekday",
    [
        ("ter", 1),
        ("Ter.", 1),
        ("na sex", 4),
        ("pode ser qui", 3),
        ("seg que vem", 0),
        # With a time, as patients answer (review of TASK-038): still that day.
        ("qua às 10", 2),
        ("seg 14h", 0),
        ("sex de manhã", 4),
        ("qui à tarde", 3),
        ("na sex pela manha", 4),
        ("ter ou qua", 1),
    ],
)
def test_a_bare_weekday_abbreviation_is_still_a_day(answer, weekday):
    parsed = flow_router._parse_day(answer, _NOW)
    assert parsed is not None and parsed.weekday() == weekday


@pytest.mark.parametrize(
    "sentence", ["posso ter desconto", "Posso ter desconto se pagar à vista?", "vou sair dom"]
)
def test_an_abbreviation_inside_a_sentence_is_not_a_day(sentence):
    assert flow_router._parse_day(sentence, _NOW) is None


# --------------------------------------------------------------------------
# 2. The agent's words travel with its hand-back to the buttons
# --------------------------------------------------------------------------


def test_the_intro_envelope_round_trips_and_keeps_the_sentinel_exact():
    text = "Claro! Para isso o ideal é uma consulta.\nEscolha abaixo."
    wrapped = with_handback_intro(SHOW_MAIN_MENU_SENTINEL, text)
    assert wrapped.startswith(HANDBACK_INTRO_PREFIX)
    assert split_handback_intro(wrapped) == (text, SHOW_MAIN_MENU_SENTINEL)


def test_no_words_means_the_bare_sentinel_as_before():
    assert with_handback_intro(SHOW_MAIN_MENU_SENTINEL, None) == SHOW_MAIN_MENU_SENTINEL
    assert with_handback_intro(SHOW_MAIN_MENU_SENTINEL, "   ") == SHOW_MAIN_MENU_SENTINEL
    assert split_handback_intro(SHOW_MAIN_MENU_SENTINEL) == (None, SHOW_MAIN_MENU_SENTINEL)


def test_a_malformed_envelope_drops_the_words_never_the_card():
    broken = f"{HANDBACK_INTRO_PREFIX}not-json\n{BOOKING_DRAFT_SENTINEL_PREFIX}{{}}"
    assert split_handback_intro(broken) == (None, f"{BOOKING_DRAFT_SENTINEL_PREFIX}{{}}")


async def test_a_hand_back_tool_carries_its_message_as_the_intro():
    with pytest.raises(ShowMainMenuRequested) as raised:
        await show_main_menu.ainvoke({"message": "  Sem problema! Aqui estão as opções.  "})
    assert raised.value.intro == "Sem problema! Aqui estão as opções."

    with pytest.raises(ShowMainMenuRequested) as silent:
        await show_main_menu.ainvoke({})
    assert silent.value.intro is None


async def test_the_draft_tool_caps_the_message():
    from secretaria.ai import tools

    token = tools._tenant_id_ctx.set("tenant")
    try:
        with pytest.raises(BookingDraftRequested) as raised:
            await set_booking_draft.ainvoke({"message": "a" * 5000})
    finally:
        tools._tenant_id_ctx.reset(token)
    assert len(raised.value.intro) == HANDBACK_MESSAGE_MAX


async def test_a_help_pick_puts_its_reason_before_the_card(monkeypatch):
    async def _pick(**kwargs):
        return ScopedHelpOutcome(
            kind="pick",
            choice="Primeira Consulta",
            message="Pelo que você contou, a primeira consulta é o caminho.",
        )

    monkeypatch.setattr(flow_router, "run_service_help", _pick)
    res = await route(
        _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_SERVICE_HELP),
        _tenant(),
        None,
        "é a minha primeira vez",
    )
    assert res.flow_step == STEP_AWAITING_SERVICE_CONFIRM
    assert isinstance(res.bubbles[0], TextBubble)
    assert res.bubbles[0].body == "Pelo que você contou, a primeira consulta é o caminho."
    assert len(res.bubbles) >= 2  # the service card still follows


async def test_a_help_reason_claiming_an_unproven_action_is_dropped(monkeypatch):
    async def _pick(**kwargs):
        return ScopedHelpOutcome(
            kind="pick", choice="Primeira Consulta", message="Seu pagamento foi confirmado!"
        )

    monkeypatch.setattr(flow_router, "run_service_help", _pick)
    res = await route(
        _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=STEP_SERVICE_HELP),
        _tenant(),
        None,
        "primeira vez",
    )
    assert res.flow_step == STEP_AWAITING_SERVICE_CONFIRM
    assert all("pagamento" not in getattr(b, "body", "") for b in res.bubbles)


# --------------------------------------------------------------------------
# 3 + 4. The "Não sei" opener and the order of service in the prompt
# --------------------------------------------------------------------------


def test_the_dont_know_openers_sound_like_a_person_and_ask_no_symptoms():
    for opener in (flow_router.PROFESSIONAL_HELP_OPENER, flow_router.SERVICE_HELP_OPENER):
        assert opener.startswith("Sem problema, eu te ajudo a escolher!")
        assert "sentindo" not in opener and "sintoma" not in opener


def test_the_prompt_states_the_order_of_service_for_every_clinic():
    prompt = secretary_system_prompt(_config())
    block = prompt[prompt.index("COMO ATENDER (ordem de prioridade)") :]
    first, then, last = (block.index(f"{n}) ") for n in (1, 2, 3))
    assert first < then < last
    assert "secretária de verdade" in block[first:then]
    assert "`message`" in block[then:last]
    assert "POR ÚLTIMO, chamar uma pessoa da equipe" in block[last:]
    assert "Não ofereça o que você não pode fazer neste turno" in block


def test_hand_back_words_with_an_unresolved_token_are_dropped():
    """A pseudonym the map could not resolve must never be printed to the patient."""
    from secretaria.ai.tools import handback_message

    assert handback_message("Claro, [PACIENTE_ab12]! Escolha abaixo.") is None
    assert handback_message("  Claro! Escolha abaixo.  ") == "Claro! Escolha abaixo."


@pytest.mark.parametrize("kind", ["draft", "professional"])
async def test_run_agent_puts_the_intro_in_front_of_every_hand_back(monkeypatch, kind):
    from uuid import uuid4

    from langchain_core.messages import HumanMessage

    from secretaria.ai import graph
    from secretaria.ai.graph import SELECT_PROFESSIONAL_SENTINEL_PREFIX, run_agent
    from secretaria.ai.tools import SelectProfessionalRequested

    professional_id = uuid4()

    async def _history(conversation_id):
        return [HumanMessage(content="quero marcar")]

    async def _raise(messages, conversation_id):
        if kind == "draft":
            raise BookingDraftRequested(None, None, None, intro="Claro! Vamos lá.")
        raise SelectProfessionalRequested(professional_id, "Dra. Ana", intro="Claro! Vamos lá.")

    monkeypatch.setattr(graph, "_load_history", _history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _raise)
    reply = await run_agent("quero marcar", context={"conversation_id": str(uuid4())})

    intro, sentinel = split_handback_intro(reply)
    assert intro == "Claro! Vamos lá."
    prefix = (
        BOOKING_DRAFT_SENTINEL_PREFIX if kind == "draft" else SELECT_PROFESSIONAL_SENTINEL_PREFIX
    )
    assert sentinel.startswith(prefix)


def test_held_words_go_out_once_and_never_outlive_the_turn():
    from secretaria.services import turn_safety_net as net

    net.hold_intro("fora de um turno")  # no turn open: nothing is held
    assert net.take_held_intro() is None

    token = net.begin_turn()
    net.hold_intro("Claro!")
    assert net.take_held_intro() == "Claro!"
    assert net.take_held_intro() is None  # only the first batch gets them
    net.hold_intro("de novo")
    net.end_turn(token)
    assert net.take_held_intro() is None  # the safety-net apology never carries them
