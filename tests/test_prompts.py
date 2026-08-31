"""Tests for ai/prompts.py: the hardcoded safety/tone block
(`_format_safety_rules`, unconditional — replaces the old clinic-editable
`persona_notes` override), the per-professional "SOBRE O PROFISSIONAL"
injection (contract v1 §10 item D, only rendered when `load_tenant_config`
populated the professional-context fields on `TenantRuntimeConfig`),
post-consult knowledge, and appointment context.
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
    _format_safety_rules,
    secretary_system_prompt,
)
from secretaria.core.whatsapp_limits import (  # noqa: E402
    EMOJI_SCHEDULE,
    MAX_LIST_ROW_TITLE_CHARS,
    decorated_text_budget,
)
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402

_SAFETY_HEADING = "REGRAS INEGOCIÁVEIS DE SEGURANÇA E CONDUTA"


def _config(**overrides) -> TenantRuntimeConfig:
    fields = dict(
        tenant_id=uuid4(),
        clinic_name="Clínica Teste",
        greeting_message=None,
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


# --------------------------------------------------------------------------
# Hardcoded safety/tone block (unconditional — no config field controls it)
# --------------------------------------------------------------------------


def test_format_safety_rules_contains_all_required_elements():
    """Direct unit test of the pure helper: every non-negotiable rule from
    the corrections-round spec must show up as a recognizable substring."""
    block = _format_safety_rules()
    assert _SAFETY_HEADING in block
    assert "diagnóstico" in block
    assert "pronto-socorro" in block
    assert "192" in block
    assert "cordial" in block


def test_safety_rules_present_with_minimal_config():
    """A bare-minimum tenant (no professional/persona/post-consult data at
    all) still gets the full safety block — it is unconditional."""
    prompt = secretary_system_prompt(_config())
    assert _SAFETY_HEADING in prompt
    assert "diagnóstico" in prompt
    assert "pronto-socorro" in prompt
    assert "192" in prompt
    assert "cordial" in prompt


def test_safety_rules_present_with_fully_populated_config():
    """A fully-dressed tenant (professional context + post-consult knowledge
    + appointment context all set) still renders the safety block verbatim —
    presence never depends on any other field."""
    config = _config(
        professional_id=uuid4(),
        context_doctor_message="Fala pausadamente com pacientes idosos.",
        specialty="Cardiologia",
        about="Atende há 15 anos na região.",
        post_consult_knowledge="Retorno em 7 dias.",
        appointment_context="Próxima consulta: 03/08 às 11:00.",
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
    )
    prompt = secretary_system_prompt(config)
    assert _SAFETY_HEADING in prompt
    assert "diagnóstico" in prompt
    assert "pronto-socorro" in prompt
    assert "192" in prompt
    assert "cordial" in prompt


def test_safety_rules_do_not_depend_on_a_persona_notes_field():
    """`TenantRuntimeConfig` no longer carries `persona_notes` at all — the
    dataclass must accept none, and the removed "INSTRUÇÕES DE PERSONA"
    heading must never appear."""
    assert "persona_notes" not in TenantRuntimeConfig.__dataclass_fields__
    prompt = secretary_system_prompt(_config())
    assert "INSTRUÇÕES DE PERSONA" not in prompt


def test_safety_rules_precede_professional_section():
    config = _config(
        professional_id=uuid4(),
        context_doctor_message="Fala pausadamente com pacientes idosos.",
    )
    prompt = secretary_system_prompt(config)
    assert _SAFETY_HEADING in prompt
    assert "SOBRE O PROFISSIONAL" in prompt
    assert prompt.index(_SAFETY_HEADING) < prompt.index("SOBRE O PROFISSIONAL")


# --------------------------------------------------------------------------
# Per-professional "SOBRE O PROFISSIONAL" section
# --------------------------------------------------------------------------


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


def test_safety_professional_and_post_consult_sections_coexist_in_order():
    config = _config(
        professional_id=uuid4(),
        context_doctor_message="Fala pausadamente com pacientes idosos.",
        post_consult_knowledge="Retorno em 7 dias.",
    )
    prompt = secretary_system_prompt(config)
    assert _SAFETY_HEADING in prompt
    assert "SOBRE O PROFISSIONAL" in prompt
    assert "CONHECIMENTO PÓS-CONSULTA" in prompt
    # safety -> professional -> post-consult (see secretary_system_prompt).
    assert (
        prompt.index(_SAFETY_HEADING)
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


def test_safety_professional_post_consult_and_appointment_context_coexist_in_order():
    config = _config(
        professional_id=uuid4(),
        context_doctor_message="Fala pausadamente com pacientes idosos.",
        post_consult_knowledge="Retorno em 7 dias.",
        appointment_context=_APPOINTMENT_CONTEXT_PAYLOAD,
    )
    prompt = secretary_system_prompt(config)
    # safety -> professional -> post-consult -> appointment context (see
    # secretary_system_prompt).
    assert (
        prompt.index(_SAFETY_HEADING)
        < prompt.index("SOBRE O PROFISSIONAL")
        < prompt.index("CONHECIMENTO PÓS-CONSULTA")
        < prompt.index("CONSULTAS MARCADAS")
    )


# --------------------------------------------------------------------------
# [SLOTS] row-title limit stated to the model
# --------------------------------------------------------------------------


def test_slots_instruction_states_the_row_title_limit_as_a_number():
    # The block used to say only "rotulo curto", which the model cannot act on.
    # The number has to reach the prompt, and it has to come from the shared
    # constant - a hand-typed "24" here would be the fourth copy of it.
    prompt = secretary_system_prompt(_config())
    slots_block = prompt.split("B) LISTA DE HOR")[1].split("=========")[0]
    # Not the raw row cap: _parse_slot_rows prepends the calendar emoji, so what
    # the model may WRITE is the cap minus what that prefix costs. Telling it 24
    # would be quoting the parser's cut point, not the model's own budget.
    budget = decorated_text_budget(EMOJI_SCHEDULE, MAX_LIST_ROW_TITLE_CHARS)
    assert str(budget) in slots_block
    assert "caracteres" in slots_block


def test_slots_instruction_says_what_happens_past_the_limit():
    # Stating a number without a consequence reads as a style note. The model
    # is told the label is cut, so a long one is a real failure, not a nit.
    prompt = secretary_system_prompt(_config())
    slots_block = prompt.split("B) LISTA DE HOR")[1].split("=========")[0]
    assert "corta" in slots_block


def test_slots_instruction_does_not_hardcode_the_limit():
    # If someone re-inlines the number, bumping MAX_LIST_ROW_TITLE_CHARS would
    # leave the prompt lying to the model. Rendering with a patched constant is
    # the only way to prove the prompt actually reads it.
    import secretaria.ai.prompts as prompts_module

    original = prompts_module.MAX_LIST_ROW_TITLE_CHARS
    try:
        prompts_module.MAX_LIST_ROW_TITLE_CHARS = 99
        patched = secretary_system_prompt(_config())
        block = patched.split("B) LISTA DE HOR")[1].split("=========")[0]
        # 96, not 99: the budget is DERIVED from the patched cap at render time.
        # Reading 99 here would mean someone froze the emoji's cost into a
        # literal; reading 21 would mean the whole thing was snapshotted at
        # import and no longer tracks the constant at all.
        assert str(decorated_text_budget(EMOJI_SCHEDULE, 99)) in block
        assert "99" not in block
    finally:
        prompts_module.MAX_LIST_ROW_TITLE_CHARS = original


def test_confirm_block_needs_no_limit_because_its_labels_are_fixed():
    # ButtonBubble hardcodes confirm_label/cancel_label ("Confirmar"/"Cancelar"),
    # both well under MAX_BUTTON_LABEL_CHARS, and the model only writes the CARD
    # BODY. Pinned so nobody "fixes" the [CONFIRM] block by adding a cap the
    # model has no way to violate.
    from secretaria.ai.formatter import ButtonBubble
    from secretaria.core.whatsapp_limits import MAX_BUTTON_LABEL_CHARS

    bubble = ButtonBubble(body="qualquer coisa")
    assert len(bubble.confirm_label) <= MAX_BUTTON_LABEL_CHARS
    assert len(bubble.cancel_label) <= MAX_BUTTON_LABEL_CHARS
