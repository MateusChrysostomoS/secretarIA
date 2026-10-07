"""Bloco de estado da conversa e sua injeção no prompt (MVP Portal, Task 2)."""

import os
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.models import FlowState  # noqa: E402
from secretaria.services import (
    flow_router as fr,  # noqa: E402
    llm_context,  # noqa: E402
)
from secretaria.services.attendee import ATTENDEE_SELF  # noqa: E402
from secretaria.services.llm_context import build_conversation_state  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402
from tests.test_flow_router import _conversation, _tenant  # noqa: E402


def _professional(name, services):
    return SimpleNamespace(
        id=uuid4(),
        name=name,
        specialty=None,
        about=None,
        appointment_types=[
            {"name": s, "is_active": True, "duration_min": 30, "sort_order": i}
            for i, s in enumerate(services)
        ],
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
    )


def test_state_names_where_the_patient_was_and_what_is_chosen():
    doctor = _professional("Dra. Ana", ["Consulta Ortopédica"])
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_SERVICE,
        flow_selected_type="Consulta Ortopédica",
        flow_selected_professional_id=doctor.id,
        flow_selected_insurance="Unimed",
    )
    tenant = _tenant()
    tenant.insurances = ["Unimed"]
    text = build_conversation_state(conv, tenant, [doctor])
    assert "lista de serviços" in text
    assert "Dra. Ana" in text
    assert "Consulta Ortopédica" in text
    assert "Convênio já informado (valor omitido)." in text
    assert "Unimed" not in text


def test_state_lists_services_per_doctor_on_multi_professional_clinic():
    a = _professional("Dra. Ana", ["Consulta Ortopédica"])
    b = _professional("Dr. Beto", ["Limpeza"])
    text = build_conversation_state(_conversation(), _tenant(), [a, b])
    assert "Dra. Ana: Consulta Ortopédica" in text
    assert "Dr. Beto: Limpeza" in text


def test_state_never_carries_the_attendee_name():
    conv = _conversation(flow_attendee_name="Joaquim Segredo")
    text = build_conversation_state(conv, _tenant(), [])
    assert "Joaquim Segredo" not in text
    assert "OUTRA pessoa" in text


def test_state_for_llm_mode_points_at_history():
    conv = _conversation(flow_state=FlowState.LLM)
    assert "histórico" in build_conversation_state(conv, _tenant(), [])


def _config(**kw) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="Clínica Teste",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
        **kw,
    )


def test_prompt_renders_state_block_only_when_present():
    base = secretary_system_prompt(_config())
    assert "ESTADO DA CONVERSA" not in base
    with_state = secretary_system_prompt(
        _config(conversation_state="- Médico já escolhido: Dra. Ana")
    )
    assert "ESTADO DA CONVERSA" in with_state
    assert "Dra. Ana" in with_state
    assert "set_booking_draft" in with_state
    assert "O que te traz à clínica?" in with_state
    assert "request_human_handoff" in with_state


def test_state_block_is_turn_scoped_replace():
    cfg = _config()
    assert replace(cfg, conversation_state="x").conversation_state == "x"


def test_no_snapshot_has_no_state():
    assert build_conversation_state(None, _tenant(), []) is None


def test_prompt_omits_patient_and_attendee_identifiers():
    conv = _conversation(flow_attendee_name="Joaquim Segredo")
    conv.patient_name = "Paciente Confidencial"
    conv.email = "patient@example.test"
    conv.phone = "+5511999999999"
    state = build_conversation_state(conv, _tenant(), None)
    prompt = secretary_system_prompt(_config(conversation_state=state))
    for value in (conv.flow_attendee_name, conv.patient_name, conv.email, conv.phone):
        assert value not in prompt
    assert "OUTRA pessoa" in prompt


def test_state_exposes_insurance_catalog_and_day():
    tenant = _tenant()
    tenant.insurances = ["Unimed", "Particular"]
    conv = _conversation(flow_selected_day="2026-10-05")
    state = build_conversation_state(conv, tenant, [])
    assert fr.insurance_plan_names(tenant) == ["Unimed", "Particular"]
    assert "Convênios cadastrados na clínica: 2" in state
    assert "Unimed" not in state
    assert "Particular" not in state
    assert "Dia já escolhido: 2026-10-05" in state


@pytest.mark.parametrize("selected", [False, True])
def test_legacy_insurance_pii_never_reaches_full_system_prompt(selected):
    name = "Paciente Sintetico"
    email = "legacy-patient@example.test"
    legacy_plan = f"{name} {email}"
    tenant = _tenant()
    tenant.insurance_plans = [{"id": None, "name": legacy_plan, "note": None}]
    conv = _conversation(flow_selected_insurance=legacy_plan if selected else None)
    state = build_conversation_state(conv, tenant, [])
    prompt = secretary_system_prompt(_config(conversation_state=state))
    for value in (name, email):
        assert value not in prompt
    assert "Convênios cadastrados na clínica: 1" in prompt
    if selected:
        assert "Convênio já informado (valor omitido)." in prompt


@pytest.mark.parametrize("value", ["person@example.test", "Joaquim Segredo", "+5511999999999"])
async def test_free_text_insurance_never_reaches_system_prompt(value):
    tenant = _tenant()
    tenant.insurances = ["Unimed"]
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_INSURANCE,
    )
    result = await fr._handle_insurance(conv, tenant, value)
    assert result.flow_selected_insurance == value
    conv.flow_selected_insurance = result.flow_selected_insurance
    state = build_conversation_state(conv, tenant, [])
    prompt = secretary_system_prompt(_config(conversation_state=state))
    assert value not in state
    assert value not in prompt
    assert "Convênio já informado (valor omitido)." in state


@pytest.mark.parametrize("step", [fr.STEP_AWAITING_DAY_ESCAPE, fr.STEP_AWAITING_DAY_RETRY])
def test_day_picker_escape_and_retry_keep_day_context(step):
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=step)
    state = build_conversation_state(conv, _tenant(), [])
    assert "escolha do dia" in state
    assert "menu inicial" not in state


# --------------------------------------------------------------------------
# TASK-030 P2: what the model sees about the workflow
# --------------------------------------------------------------------------


@pytest.mark.parametrize("step", fr.ATTENDEE_STEPS)
@pytest.mark.parametrize("marker", [fr.ATTENDEE_NEXT_BOOK, fr.ATTENDEE_NEXT_CATALOG])
def test_no_internal_marker_ever_reaches_the_model_text(step, marker):
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=step, flow_selected_type=marker
    )
    state = build_conversation_state(conv, _tenant(), [])
    prompt = secretary_system_prompt(_config(conversation_state=state))
    assert "__" not in state
    assert "attendee_next" not in prompt
    assert "Serviço já escolhido" not in state


def test_every_workflow_step_has_a_label():
    steps = {value for name, value in vars(fr).items() if name.startswith("STEP_")}
    assert steps <= set(llm_context._STEP_LABELS)


@pytest.mark.parametrize(
    "step",
    [
        fr.STEP_AWAITING_ATTENDEE_CHOICE,
        fr.STEP_MANAGE_ACTION,
        fr.STEP_MANAGE_SLOT,
        fr.STEP_DECLINE_REASON,
        fr.STEP_AWAITING_CATALOG_SERVICE,
    ],
)
def test_steps_that_used_to_read_as_the_menu_are_named(step):
    conv = _conversation(flow_state=FlowState.SERVICE_CATALOG, flow_step=step)
    assert "menu inicial" not in build_conversation_state(conv, _tenant(), [])


def test_the_pra_quem_question_is_named():
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG, flow_step=fr.STEP_AWAITING_ATTENDEE_CHOICE
    )
    assert "Essa consulta é pra você?" in build_conversation_state(conv, _tenant(), [])


@pytest.mark.parametrize(
    "attendee, expected",
    [
        (None, "Pra quem é a consulta: ainda não respondido."),
        (ATTENDEE_SELF, "Pra quem é a consulta: para o próprio paciente."),
        ("Joaquim Segredo", "Pra quem é a consulta: OUTRA pessoa"),
    ],
)
def test_the_pra_quem_answer_is_spelled_out_without_the_name(attendee, expected):
    state = build_conversation_state(_conversation(flow_attendee_name=attendee), _tenant(), [])
    assert expected in state
    assert "Joaquim" not in state


def test_an_unanswered_pra_quem_tells_the_model_not_to_ask_it():
    """Owner, 2026-10-06: the bare "ainda não respondido" made the model open the
    free chat with "Essa consulta é pra você?"; the guided flow asks it itself."""
    state = build_conversation_state(_conversation(flow_attendee_name=None), _tenant(), [])
    assert "Não pergunte isso você: o fluxo guiado pergunta na hora de marcar." in state
    answered = build_conversation_state(
        _conversation(flow_attendee_name=ATTENDEE_SELF), _tenant(), []
    )
    assert "Não pergunte isso você" not in answered


def test_the_chosen_time_is_shown():
    conv = _conversation(flow_selected_day="2026-10-08", flow_selected_slot="2026-10-08T10:00")
    assert "Horário já escolhido: 08/10/2026 às 10:00" in build_conversation_state(
        conv, _tenant(), []
    )


def test_a_single_doctor_clinic_says_so_with_its_services():
    doctor = _professional("Dra. Ana", ["Consulta Ortopédica", "Retorno"])
    state = build_conversation_state(_conversation(), _tenant(), [doctor])
    assert "um só médico: Dra. Ana. Serviços: Consulta Ortopédica, Retorno." in state


def test_a_clinic_without_professionals_lists_its_services():
    state = build_conversation_state(_conversation(), _tenant(), [])
    assert "Serviços da clínica: Primeira Consulta." in state
