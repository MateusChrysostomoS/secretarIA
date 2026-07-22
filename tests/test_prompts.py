"""Tests for ai/prompts.py's per-professional "SOBRE O PROFISSIONAL" injection
(contract v1 §10 item D) — only rendered when `load_tenant_config` populated
the professional-context fields on `TenantRuntimeConfig`.
"""

import os
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from secretaria.ai.prompts import (  # noqa: E402
    _format_appointment_context,
    secretary_system_prompt,
)
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402


def _config(**overrides) -> TenantRuntimeConfig:
    fields = dict(
        tenant_id=uuid4(),
        clinic_name="Clínica Teste",
        greeting_message=None,
        persona_notes=None,
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
    )
    fields.update(overrides)
    return TenantRuntimeConfig(**fields)


def test_no_professional_section_when_nothing_set():
    prompt = secretary_system_prompt(_config())
    assert "SOBRE O PROFISSIONAL" not in prompt


def test_no_professional_section_for_multi_professional_tenant():
    """load_tenant_config leaves these fields unset for a multi-professional
    tenant - the base prompt must stay silent about any specific doctor."""
    config = _config(professional_id=None, context_doctor_message=None)
    prompt = secretary_system_prompt(config)
    assert "SOBRE O PROFISSIONAL" not in prompt


def test_context_doctor_message_appears_in_professional_section():
    config = _config(
        professional_id=uuid4(),
        context_doctor_message="Prefere que pacientes cheguem 10 minutos antes.",
    )
    prompt = secretary_system_prompt(config)
    assert "SOBRE O PROFISSIONAL" in prompt
    assert "Prefere que pacientes cheguem 10 minutos antes." in prompt


def test_professional_section_instructs_interpretation_not_verbatim_recitation():
    config = _config(professional_id=uuid4(), context_doctor_message="Nota interna.")
    prompt = secretary_system_prompt(config)
    assert "NÃO as recite literalmente" in prompt


def test_specialty_and_about_render_without_context_message():
    config = _config(
        professional_id=uuid4(),
        specialty="Cardiologia",
        about="Atende há 15 anos na região.",
        context_doctor_message=None,
    )
    prompt = secretary_system_prompt(config)
    assert "SOBRE O PROFISSIONAL" in prompt
    assert "Cardiologia" in prompt
    assert "Atende há 15 anos na região." in prompt


def test_persona_notes_and_professional_section_coexist_in_order():
    config = _config(
        persona_notes="Seja bem-humorada.",
        professional_id=uuid4(),
        context_doctor_message="Fala pausadamente com pacientes idosos.",
    )
    prompt = secretary_system_prompt(config)
    assert "INSTRUÇÕES DE PERSONA" in prompt
    assert "SOBRE O PROFISSIONAL" in prompt
    # Persona comes first (existing behaviour), professional context follows.
    assert prompt.index("INSTRUÇÕES DE PERSONA") < prompt.index("SOBRE O PROFISSIONAL")


def test_no_post_consult_knowledge_section_when_none():
    prompt = secretary_system_prompt(_config())
    assert "CONHECIMENTO PÓS-CONSULTA" not in prompt


def test_post_consult_knowledge_section_present_when_set():
    config = _config(
        post_consult_knowledge="Retorno em 7 dias. Resultados de exame saem em 48h pelo portal."
    )
    prompt = secretary_system_prompt(config)
    assert "CONHECIMENTO PÓS-CONSULTA" in prompt
    assert "Retorno em 7 dias. Resultados de exame saem em 48h pelo portal." in prompt
    assert "NÃO as recite" in prompt


def test_persona_professional_and_post_consult_sections_coexist_in_order():
    config = _config(
        persona_notes="Seja bem-humorada.",
        professional_id=uuid4(),
        context_doctor_message="Fala pausadamente com pacientes idosos.",
        post_consult_knowledge="Retorno em 7 dias.",
    )
    prompt = secretary_system_prompt(config)
    assert "INSTRUÇÕES DE PERSONA" in prompt
    assert "SOBRE O PROFISSIONAL" in prompt
    assert "CONHECIMENTO PÓS-CONSULTA" in prompt
    # persona -> professional -> post-consult (see secretary_system_prompt).
    assert (
        prompt.index("INSTRUÇÕES DE PERSONA")
        < prompt.index("SOBRE O PROFISSIONAL")
        < prompt.index("CONHECIMENTO PÓS-CONSULTA")
    )


def test_prompt_still_renders_business_hours_and_types_normally():
    """Sanity: the professional-context addition must not disturb the
    existing hours/types rendering."""
    config = _config(
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        professional_id=uuid4(),
        context_doctor_message="Nota.",
    )
    prompt = secretary_system_prompt(config)
    assert "Segunda" in prompt
    assert "08h" in prompt


# --------------------------------------------------------------------------
# Appointment context ("Outro" -> LLM handoff, PROMPT for existing appointments)
# --------------------------------------------------------------------------

_APPOINTMENT_CONTEXT_PAYLOAD = "Próxima consulta: 03/08 às 11:00 — Consulta Geral — Dra. Ana"


def test_format_appointment_context_empty_when_unset():
    assert _format_appointment_context(_config()) == ""


def test_format_appointment_context_renders_payload_and_tool_names():
    config = _config(appointment_context=_APPOINTMENT_CONTEXT_PAYLOAD)
    section = _format_appointment_context(config)
    assert "CONSULTAS MARCADAS" in section
    assert _APPOINTMENT_CONTEXT_PAYLOAD in section
    assert "manage_existing_appointment" in section
    assert "show_main_menu" in section


def test_no_appointment_context_section_when_unset():
    prompt = secretary_system_prompt(_config())
    assert "CONSULTAS MARCADAS" not in prompt


def test_appointment_context_section_present_when_set():
    config = _config(appointment_context=_APPOINTMENT_CONTEXT_PAYLOAD)
    prompt = secretary_system_prompt(config)
    assert "CONSULTAS MARCADAS" in prompt
    assert _APPOINTMENT_CONTEXT_PAYLOAD in prompt
    assert "manage_existing_appointment" in prompt


def test_persona_professional_post_consult_and_appointment_context_coexist_in_order():
    config = _config(
        persona_notes="Seja bem-humorada.",
        professional_id=uuid4(),
        context_doctor_message="Fala pausadamente com pacientes idosos.",
        post_consult_knowledge="Retorno em 7 dias.",
        appointment_context=_APPOINTMENT_CONTEXT_PAYLOAD,
    )
    prompt = secretary_system_prompt(config)
    # persona -> professional -> post-consult -> appointment context (see
    # secretary_system_prompt).
    assert (
        prompt.index("INSTRUÇÕES DE PERSONA")
        < prompt.index("SOBRE O PROFISSIONAL")
        < prompt.index("CONHECIMENTO PÓS-CONSULTA")
        < prompt.index("CONSULTAS MARCADAS")
    )
