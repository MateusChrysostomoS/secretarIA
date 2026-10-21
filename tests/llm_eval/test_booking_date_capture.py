"""Opt-in: the real model transfers the user's stated date and attendee.

Only the model's first decision is evaluated. No tool is executed, no database,
Calendar, patient conversation or remote configuration is touched.
"""

import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from secretaria.ai.chat_model import build_chat_model, system_message
from secretaria.ai.graph import base_tools_for
from secretaria.ai.prompts import secretary_system_prompt
from secretaria.config import get_settings
from secretaria.plugins.multi_professional import MULTI_PROFESSIONAL_SPEC
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_MULTI
from secretaria.workers.shared.llm_context import _flow_handback_tools
from tests.test_prompts import _config

pytestmark = pytest.mark.skipif(os.environ.get("RUN_LLM_EVAL") != "1", reason="real model opt-in")
NOW = datetime(2026, 10, 7, 1, 30, tzinfo=UTC)
STATE = (
    "- Onde o paciente estava: no menu inicial — tocou em 'Outro'\n"
    "- Pra quem é a consulta: ainda não respondido.\n"
    "- A clínica tem vários médicos. Serviços de cada médico:\n"
    "  - Dr. Diogo Raposo: Cirurgia de Catarata\n"
    "  - Dr. Rafael Teixeira: Cirurgia de Catarata\n"
)


def _require_provider_key() -> None:
    """The provider LLM_PROVIDER picks (ai/chat_model.py) needs its own key."""
    s = get_settings()
    key = s.ANTHROPIC_API_KEY if s.LLM_PROVIDER == "anthropic" else s.OPENAI_API_KEY
    if not key or key == "test-openai-key":
        pytest.skip(f"{s.LLM_PROVIDER} API key missing; real-model behavior was not evaluated")


@pytest.mark.parametrize("date_phrase, expected", [
    ("07/10", "2026-10-07"),
    ("amanhã", "2026-10-07"),
    ("quinta da semana que vem", "2026-10-15"),
])
@pytest.mark.parametrize("run", range(3))
async def test_real_model_transfers_the_stated_fields(date_phrase, expected, run):
    _require_provider_key()
    extras = _flow_handback_tools(
        SimpleNamespace(initial_flows={}), BOOKING_TOPOLOGY_MULTI,
        list(MULTI_PROFESSIONAL_SPEC.agent_tools),
    )
    model = build_chat_model(timeout=60).bind_tools(
        [*base_tools_for(BOOKING_TOPOLOGY_MULTI), *extras]
    )
    prompt = secretary_system_prompt(_config(conversation_state=STATE), now=NOW)
    message = await model.ainvoke([
        system_message(prompt),
        HumanMessage(content=(
            f"Eu gostaria de marcar uma consulta para o dia {date_phrase}, com o médico Diogo, "
            "com o serviço de cirurgia de catarata, "
            "saiba que nao tenho convenio e será para mim mesmo"
        )),
    ])
    calls = message.tool_calls or []
    assert [c["name"] for c in calls] == ["set_booking_draft"]
    args = calls[0]["args"]
    assert args["day"] == expected
    assert args["for_whom"] == "me"
    assert args["insurance"].casefold() == "particular"
    assert args["service"] == "Cirurgia de Catarata"
    assert "Diogo" in args["professional"]
    assert not args.get("time"), "never invent the missing time"


@pytest.mark.parametrize("run", range(3))
async def test_real_model_keeps_selected_day_when_answering_only_the_time(run):
    _require_provider_key()
    state = (
        "- Onde o paciente estava: a escolha do horário do agendamento\n"
        "- Médico já escolhido: Dr. Diogo Raposo\n"
        "- Serviço já escolhido: Cirurgia de Catarata\n"
        "- Pra quem é a consulta: para o próprio paciente.\n"
        "- Convênio já informado (valor omitido).\n"
        "- Dia já escolhido: 2026-10-07\n" + STATE.split("- A clínica", 1)[1]
    )
    extras = _flow_handback_tools(
        SimpleNamespace(initial_flows={}), BOOKING_TOPOLOGY_MULTI,
        list(MULTI_PROFESSIONAL_SPEC.agent_tools),
    )
    model = build_chat_model(timeout=60).bind_tools(
        [*base_tools_for(BOOKING_TOPOLOGY_MULTI), *extras]
    )
    msg = await model.ainvoke([
        system_message(secretary_system_prompt(_config(conversation_state=state), now=NOW)),
        HumanMessage(content="07/10, Cirurgia de Catarata, Diogo, Particular, para mim."),
        AIMessage(content="Horários livres em 07/10: 14:00 e 14:40. Qual você prefere?"),
        HumanMessage(content="14:40"),
    ])
    assert [c["name"] for c in msg.tool_calls] == ["set_booking_draft"]
    assert msg.tool_calls[0]["args"]["day"] == "2026-10-07"
    assert msg.tool_calls[0]["args"]["time"] == "14:40"
