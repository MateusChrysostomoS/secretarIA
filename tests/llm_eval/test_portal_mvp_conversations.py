"""Bateria opt-in: o modelo REAL escolhe a ferramenta certa dado o estado (MVP Portal).

Não roda em CI: `RUN_LLM_EVAL=1 OPENAI_API_KEY=... uv run python -m pytest tests/llm_eval -v`.
Mede só a primeira decisão (qual ferramenta, com quais argumentos) sobre o prompt de
produção + o bloco de estado — barato, sem DB nem calendário.
"""

import os
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from secretaria.ai.prompts import secretary_system_prompt
from secretaria.ai.tools import (
    list_patient_appointments,
    request_human_handoff,
    set_booking_draft,
    show_main_menu,
)
from secretaria.config import get_settings
from secretaria.services.tenant_config import TenantRuntimeConfig

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_LLM_EVAL") != "1", reason="set RUN_LLM_EVAL=1 (uses the real model)"
)

STATE = (
    "- Onde o paciente estava: no menu inicial — tocou em 'Outro' ou escreveu livremente\n"
    "- Convênios aceitos pela clínica: Unimed, Bradesco Saúde\n"
    "- Serviços de cada médico:\n"
    "  - Dra. Ana: Consulta Ortopédica\n"
    "  - Dr. Beto: Limpeza, Clareamento"
)


def _config() -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="Clínica Teste",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={"monday": [{"start": "08:00", "end": "17:00"}]},
        google_calendar_id="primary",
        google_refresh_token=None,
        conversation_state=STATE,
    )


async def _first_decision(history):
    s = get_settings()
    model = ChatOpenAI(model=s.OPENAI_SECRETARIA_MODEL, api_key=s.OPENAI_API_KEY).bind_tools(
        [set_booking_draft, request_human_handoff, show_main_menu, list_patient_appointments]
    )
    return await model.ainvoke(
        [SystemMessage(content=secretary_system_prompt(_config())), *history]
    )


def _tool_names(msg) -> list[str]:
    return [c["name"] for c in (msg.tool_calls or [])]


async def test_named_service_jumps_the_flow():
    msg = await _first_decision(
        [AIMessage(content="Como posso ajudar?"), HumanMessage(content="Quero marcar uma limpeza")]
    )
    assert "set_booking_draft" in _tool_names(msg)
    call = next(c for c in msg.tool_calls if c["name"] == "set_booking_draft")
    assert "limpeza" in str(call["args"]).lower()


async def test_symptom_is_related_to_a_service_not_to_a_human():
    msg = await _first_decision(
        [AIMessage(content="Como posso ajudar?"),
         HumanMessage(content="Estou com dor no joelho há uma semana")]
    )
    assert "request_human_handoff" not in _tool_names(msg)


async def test_bare_button_tap_gets_an_open_question_not_a_human():
    msg = await _first_decision([HumanMessage(content="Outro")])
    assert _tool_names(msg) == []
    assert "traz" in (msg.content or "").lower()


async def test_explicit_request_for_a_person_hands_off():
    msg = await _first_decision(
        [AIMessage(content="O que te traz à clínica?"),
         HumanMessage(content="Prefiro falar com uma pessoa, por favor")]
    )
    assert "request_human_handoff" in _tool_names(msg)
    call = next(c for c in msg.tool_calls if c["name"] == "request_human_handoff")
    assert call["args"].get("reason") == "patient_requested_human"


async def test_ordinary_clinic_question_is_answered_without_handoff():
    msg = await _first_decision([HumanMessage(content="Qual o horário de funcionamento?")])
    assert "request_human_handoff" not in _tool_names(msg)
    assert "17" in (msg.content or "") or "08" in (msg.content or "")
