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


def test_edit_detour_explains_the_parked_confirmation_without_identifiers():
    conv = _conversation(flow_state=FlowState.EDIT_BOOKING, flow_step=fr.STEP_EDIT_CONFIRM)
    conv.flow_edit_draft = {"current": {
        "service": "Retorno", "start_at": "2030-10-15T16:00", "insurance": "Plano Confidencial",
        "attendee_name": "Joaquim Segredo",
    }}
    text = build_conversation_state(conv, _tenant(), [])
    assert "confirmação das alterações" in text
    assert "Retorno" in text and "15/10/2030 às 16:00" in text
    assert "OUTRA pessoa" in text
    assert "Joaquim Segredo" not in text and "Plano Confidencial" not in text


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


# --- TASK-083 follow-up A1: the model knows doctor x service x unit ---------------------------

from secretaria.services.unit_booking import UnitOption  # noqa: E402

_OPEN = {"monday": [{"start": "08:00", "end": "12:00"}]}


def _unit(name, address, hours=None):
    return UnitOption(
        id=uuid4(), name=name, address=address, business_hours=_OPEN if hours is None else hours
    )


def _doctor_at(name, services, units, *, specialty=None, restrict=None):
    """A roster professional with `units` and `restrict` = {service name: [units]}."""
    doctor = _professional(name, [])
    doctor.specialty = specialty
    entries = []
    service_units = {}
    for index, service in enumerate(services):
        service_id = uuid4()
        entries.append(
            {
                "name": service, "is_active": True, "duration_min": 30, "sort_order": index,
                "service_id": str(service_id),
            }
        )
        if restrict and service in restrict:
            service_units[service_id] = frozenset(unit.id for unit in restrict[service])
    doctor.appointment_types = entries
    doctor.units = units
    doctor.service_units = service_units
    return doctor


def test_topology_lists_units_and_services_per_doctor():
    sede = _unit("Unidade Sede", "Rua das Flores, 100")
    centro = _unit("Unidade Centro", "Av. Brasil, 50")
    ana = _doctor_at(
        "Dra. Ana", ["Consulta", "Retorno"], [sede, centro],
        specialty="Oftalmologia", restrict={"Consulta": [sede]},
    )
    bruno = _doctor_at("Dr. Bruno", ["Retorno"], [centro])
    text = build_conversation_state(_conversation(), _tenant(), [ana, bruno])
    assert (
        "- Unidades: Unidade Sede (Rua das Flores, 100) | Unidade Centro (Av. Brasil, 50)"
    ) in text
    assert (
        "  - Dra. Ana (Oftalmologia): Unidade Sede: Consulta, Retorno | Unidade Centro: Retorno"
    ) in text
    assert "  - Dr. Bruno: Unidade Centro: Retorno" in text
    # Each address once, never repeated in the doctors' lines.
    assert text.count("Rua das Flores, 100") == 1 and text.count("Av. Brasil, 50") == 1
    assert (
        "- Ofereça só as combinações médico × unidade × serviço listadas a seguir; se o "
        "paciente quiser outra unidade, diga em quais o serviço é feito com esse médico ou "
        "quais médicos fazem o serviço lá."
    ) in text
    # The guidance and the units come BEFORE the roster (the bounded state cuts from the end).
    assert text.index("Ofereça só") < text.index("- Unidades:") < text.index("vários médicos")


def test_topology_service_without_marks_in_every_unit():
    sede = _unit("Sede", "Rua A, 1")
    centro = _unit("Centro", "Rua B, 2")
    ana = _doctor_at("Dra. Ana", ["Consulta"], [sede, centro])
    # An entry with no service_id at all is unrestricted as well.
    ana.appointment_types.append(
        {"name": "Avulso", "is_active": True, "duration_min": 30, "sort_order": 9}
    )
    other = _doctor_at("Dr. Bruno", ["Limpeza"], [sede])
    text = build_conversation_state(_conversation(), _tenant(), [ana, other])
    assert "Sede: Consulta, Avulso | Centro: Consulta, Avulso" in text


def test_topology_ignores_a_unit_without_open_hours():
    sede = _unit("Sede", "Rua A, 1")
    closed = _unit("Fechada", "Rua C, 3", hours={})
    ana = _doctor_at("Dra. Ana", ["Consulta"], [sede, closed])
    other = _doctor_at("Dr. Bruno", ["Limpeza"], [sede])
    text = build_conversation_state(_conversation(), _tenant(), [ana, other])
    assert "Fechada" not in text


def test_topology_without_units_is_unchanged():
    a = _professional("Dra. Ana", ["Consulta Ortopédica"])
    b = _professional("Dr. Beto", ["Limpeza", "Retorno"])
    b.specialty = "Dentista"
    multi = (
        "- A clínica tem vários médicos. Especialidade e serviços de cada um:\n"
        "  - Dra. Ana: Consulta Ortopédica\n"
        "  - Dr. Beto (Dentista): Limpeza, Retorno"
    )
    assert multi in build_conversation_state(_conversation(), _tenant(), [a, b])
    # Doctors whose unit list is present but empty / closed render exactly the same way.
    a.units, a.service_units = [], {}
    b.units, b.service_units = [_unit("Fechada", "X", hours={})], {}
    text = build_conversation_state(_conversation(), _tenant(), [a, b])
    assert multi in text
    assert "Ofereça só as combinações" not in text
    single = build_conversation_state(_conversation(), _tenant(), [a])
    assert single.endswith(
        "- A clínica atende com um só médico: Dra. Ana. Serviços: Consulta Ortopédica."
    )


def test_single_doctor_topology_with_units():
    sede = _unit("Sede", "Rua A, 1")
    centro = _unit("Centro", "Rua B, 2")
    ana = _doctor_at(
        "Dra. Ana", ["Consulta", "Retorno"], [sede, centro],
        specialty="Oftalmologia", restrict={"Consulta": [sede]},
    )
    text = build_conversation_state(_conversation(), _tenant(), [ana])
    assert (
        "- A clínica atende com um só médico: Dra. Ana (Oftalmologia). "
        "Unidades e serviços: Sede: Consulta, Retorno | Centro: Retorno."
    ) in text
    assert "- Unidades: Sede (Rua A, 1) | Centro (Rua B, 2)" in text
    assert "Ofereça só as combinações" in text


def test_edit_state_names_current_unit():
    sede = _unit("Unidade Sede", "Rua A, 1")
    centro = _unit("Unidade Centro", "Rua B, 2")
    ana = _doctor_at("Dra. Ana", ["Consulta"], [sede, centro])
    conv = _conversation(flow_state=FlowState.EDIT_BOOKING, flow_step=fr.STEP_EDIT_CONFIRM)
    conv.flow_edit_draft = {
        "current": {
            "service": "Consulta", "professional_id": str(ana.id), "unit_id": str(centro.id),
            "start_at": "2030-10-15T16:00",
        }
    }
    text = build_conversation_state(conv, _tenant(), [ana])
    assert "- Unidade atual da consulta: Unidade Centro" in text
    # No unit on the draft: the line is absent (today's text).
    conv.flow_edit_draft = {"current": {"service": "Consulta", "start_at": "2030-10-15T16:00"}}
    assert "Unidade atual" not in build_conversation_state(conv, _tenant(), [ana])


def test_a_doctor_with_every_service_on_closed_units_has_none_to_offer():
    sede = _unit("Sede", "Rua A, 1")
    closed = _unit("Fechada", "Rua C, 3", hours={})
    # Consulta is restricted to the closed unit: nothing is bookable for her right now.
    ana = _doctor_at("Dra. Ana", ["Consulta"], [sede, closed], restrict={"Consulta": [closed]})
    bruno = _doctor_at("Dr. Bruno", ["Retorno"], [sede])
    text = build_conversation_state(_conversation(), _tenant(), [ana, bruno])
    assert "  - Dra. Ana: sem serviço em unidade aberta no momento" in text
    assert "Dra. Ana: Consulta" not in text
    assert "Fechada" not in text
    single = build_conversation_state(_conversation(), _tenant(), [ana])
    assert "Unidades e serviços: sem serviço em unidade aberta no momento." in single


def test_the_unit_guidance_survives_the_bounded_state_on_a_large_clinic():
    from secretaria.ai.prompts_v2 import STATE_MAX_CHARS, _bounded_state

    units = [
        _unit(f"Unidade {n}", f"Rua Muito Longa Numero {n}, {n}00 - Bairro Distante")
        for n in range(3)
    ]
    doctors = [
        _doctor_at(
            f"Dr. Fulano de Tal Numero {n}",
            [f"Serviço com nome comprido {m}" for m in range(8)],
            units,
            specialty="Oftalmologia Geral",
        )
        for n in range(15)
    ]
    text = build_conversation_state(_conversation(), _tenant(), doctors)
    assert len(text) > STATE_MAX_CHARS  # the synthetic roster really overflows
    bounded = _bounded_state(text)
    assert "Ofereça só as combinações médico × unidade × serviço" in bounded
    assert "- Unidades: " in bounded


# --- Follow-up final review: what is pending in "Alterar dados" / the unit of a booking -------


def test_edit_state_tells_the_pending_choice_and_the_listed_unit():
    centro = _unit("Centro", "Rua A, 10")
    sul = _unit("Zona Sul", "Av. B, 20")
    ana = _doctor_at("Dra. Ana", ["Consulta"], [centro])
    beto = _doctor_at("Dr. Beto", ["Consulta", "Exame"], [centro, sul])
    conv = _conversation(flow_state=FlowState.EDIT_BOOKING, flow_step=fr.STEP_EDIT_SERVICE)
    current = {
        "service": "Consulta", "professional_id": str(ana.id), "unit_id": str(centro.id),
        "start_at": "2030-10-15T16:00",
    }
    conv.flow_edit_draft = {
        "current": current,
        "stage": {"pending_doctor": str(beto.id), "at_unit": str(sul.id)},
    }
    text = build_conversation_state(conv, _tenant(), [ana, beto])
    assert "- Escolha pendente na alteração (ainda não aplicada): novo médico Dr. Beto." in text
    assert "- Unidade da lista aberta: Zona Sul" in text

    conv.flow_edit_draft = {
        "current": current,
        "stage": {"pending_service": "Exame", "unit_purpose": "service"},
    }
    text = build_conversation_state(conv, _tenant(), [ana, beto])
    assert (
        "- Escolha pendente na alteração (ainda não aplicada): novo serviço Exame, "
        "falta escolher a unidade." in text
    )
    assert "Unidade da lista aberta" not in text

    # Nothing staged: neither line (today's text).
    conv.flow_edit_draft = {"current": current, "stage": {}}
    text = build_conversation_state(conv, _tenant(), [ana, beto])
    assert "Escolha pendente" not in text and "lista aberta" not in text


def test_booking_in_progress_names_the_unit_already_chosen():
    centro = _unit("Centro", "Rua A, 10")
    ana = _doctor_at("Dra. Ana", ["Consulta"], [centro])
    conv = _conversation(
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=fr.STEP_AWAITING_DAY,
        flow_selected_professional_id=ana.id,
        flow_selected_unit_id=centro.id,
    )
    assert "- Unidade já escolhida: Centro" in build_conversation_state(conv, _tenant(), [ana])
    conv.flow_selected_unit_id = None
    assert "Unidade já escolhida" not in build_conversation_state(conv, _tenant(), [ana])
