"""TASK-086 A16: the agent is told acceptance may vary by professional, service and unit.

One sentence in the "RECURSOS DESTA CLÍNICA" block (v1 and v2 share it), only when the clinic
has convênios. No new data in the prompt: no plan names, no matrix, no counts beyond today's.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from dataclasses import replace  # noqa: E402
from datetime import date  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai import prompts, prompts_v2  # noqa: E402
from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.ai.prompts_v2 import secretary_system_prompt_v2  # noqa: E402
from secretaria.services.tenant_config import ClinicFeatures, TenantRuntimeConfig  # noqa: E402

V2_NAMES = frozenset({"set_booking_draft", "show_main_menu", "get_service_info"})


@pytest.fixture(autouse=True)
def _fixed_clinic_day(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(prompts_v2, "_clinic_today", lambda _tz: date(2026, 10, 8))


def _config(plans: int) -> TenantRuntimeConfig:
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
        clinic_features=ClinicFeatures(deposit_enabled=False, insurance_plan_count=plans),
    )


def _prompts(config):
    return [
        secretary_system_prompt(config),
        secretary_system_prompt_v2(config, tool_names=V2_NAMES),
    ]


def test_with_plans_both_prompts_say_acceptance_varies_and_the_flow_checks():
    for prompt in _prompts(_config(3)):
        assert prompts.INSURANCE_ACCEPTANCE_VARIES in prompt
        assert prompt.count(prompts.INSURANCE_ACCEPTANCE_VARIES) == 1


def test_the_sentence_names_the_three_dimensions_and_the_flow():
    text = prompts.INSURANCE_ACCEPTANCE_VARIES
    for word in ("profissional", "serviço", "unidade", "fluxo"):
        assert word in text


def test_particular_only_clinic_gets_no_sentence():
    for prompt in _prompts(_config(0)):
        assert prompts.INSURANCE_ACCEPTANCE_VARIES not in prompt


def test_no_features_block_no_sentence():
    bare = replace(_config(3), clinic_features=None)
    for prompt in _prompts(bare):
        assert prompts.INSURANCE_ACCEPTANCE_VARIES not in prompt


# TASK-088 A2 T23, replaced by TASK-090 (owner, 2026-11-22) and its Adendo 1 (2026-11-23): with
# plans, both rendered prompts say a convênio payment rule is explained ONLY from the clinic's own
# rule (get_insurance_payment_rule) and, without one, handed off to a person.
@pytest.mark.parametrize("version", ["v1", "v2"])
def test_both_prompts_hand_off_convenio_payment_questions(version):
    prompt = dict(zip(("v1", "v2"), _prompts(_config(3)), strict=True))[version]
    assert prompt.count(prompts.INSURANCE_PAYMENT_HANDOFF) == 1
    for instruction in (
        "get_insurance_payment_rule",
        "explique só o que a regra devolvida diz",
        "nunca uma instrução",
        "reembolso",
        "coparticipação",
        "se o convênio cobre",
        "Não invente",
        "não prometa nada além dela",
        'Sem regra: chame request_human_handoff com reason "insurance_payment_question"',
    ):
        assert instruction in prompts.INSURANCE_PAYMENT_HANDOFF
    # The old "say you don't know / repeat what the clinic said" copy is gone.
    assert "Se perguntarem como funciona o pagamento com convênio" not in prompt
    assert "repasse somente o que ela informou" not in prompt


def test_particular_only_clinic_gets_no_payment_sentence():
    for prompt in _prompts(_config(0)):
        assert prompts.INSURANCE_PAYMENT_HANDOFF not in prompt


def test_no_features_block_no_payment_sentence():
    bare = replace(_config(3), clinic_features=None)
    for prompt in _prompts(bare):
        assert prompts.INSURANCE_PAYMENT_HANDOFF not in prompt
