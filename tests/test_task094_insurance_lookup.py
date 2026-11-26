"""TASK-094 D6 - a convênio "Outro" with a rule (and "sul america") was not found.

Battery 2026-11-26 (Chrysostomo):
* IA-4: "Como funciona o Plano Teste Bateria?" -> "Não tenho informações sobre o "Plano Teste
  Bateria" e não consigo confirmar como ele funciona por aqui." + a generic human offer. The
  trigger of get_insurance_payment_rule was a list of words (reembolso, coparticipação, cobre,
  como/quanto se paga) - "como funciona o <nome>" did not fire it.
* IA-8: "e pela sul america, tem reembolso?" -> "Não tenho cadastrada a regra" - it has one: the
  lookup compared normalize() keys, which keep the space ("sul america" != "sulamerica").

Owner's decision (TASK-094): any question about how a convênio NAMED by the patient works calls
the tool; the comparison ignores accents, case, spaces and punctuation (a compact key), on the
name and on the aliases; common aliases are added for the AI lookup; a name that matches more
than one convênio makes the AI ask which one.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio

from secretaria.ai import prompts, prompts_v2, tools as ai_tools
from secretaria.core import database as core_database
from secretaria.models import Tenant
from secretaria.models.insurance import InsuranceCatalog, TenantInsurancePlan
from secretaria.services import insurance_payment_note as ipn
from secretaria.services.insurance_catalog import (
    CATALOG_LOOKUP_ALIASES,
    catalog_id_for_slug,
    catalog_lookup_keys,
    catalog_match_keys,
    ensure_catalog_seeded,
)
from tests.payments_support import make_sessionmaker

SULAMERICA = catalog_id_for_slug("sulamerica")
BRADESCO = catalog_id_for_slug("bradesco-saude")
OUTRO = "Plano Teste Bateria"
RULE = "Coparticipação de 20%, paga no dia do atendimento."


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    monkeypatch.setattr(core_database, "async_session_factory", maker)
    yield maker
    await engine.dispose()


async def _clinic(db, *rows) -> Tenant:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="C", phone_number_id=None, insurance_mode="shared")
        session.add(tenant)
        await session.flush()
        for row in rows:
            row.tenant_id = tenant.id
            session.add(row)
        await session.commit()
        return tenant


async def _ask(tenant_id, insurance: str) -> tuple[str, list[str]]:
    notices: list[str] = []
    tokens = (
        ai_tools._tenant_id_ctx.set(tenant_id),
        ai_tools._conversation_id_ctx.set(None),
        ai_tools._patient_notices_ctx.set(notices),
    )
    try:
        answer = await ai_tools.get_insurance_payment_rule.ainvoke({"insurance": insurance})
    finally:
        ai_tools._patient_notices_ctx.reset(tokens[2])
        ai_tools._conversation_id_ctx.reset(tokens[1])
        ai_tools._tenant_id_ctx.reset(tokens[0])
    return answer, notices


# ----------------------------------------------------------------------- the compact key


@pytest.mark.parametrize(
    "spelling", ["SulAmérica", "sul america", "Sul-America", "SUL AMÉRICA", " sul.américa? "]
)
def test_the_compact_key_ignores_accents_case_spaces_and_punctuation(spelling):
    assert ipn.compact_key(spelling) == "sulamerica"


# ------------------------------------------------------------------------- the symptoms


@pytest.mark.parametrize("asked", ["sul america", "SulAmérica", "Sul América", "sulamerica"])
async def test_symptom_sul_america_without_accent_finds_the_rule(db, asked):
    tenant = await _clinic(db, TenantInsurancePlan(catalog_id=SULAMERICA, custom_payment_note=RULE))
    answer, notices = await _ask(tenant.id, asked)
    assert len(notices) == 1 and RULE in notices[0]
    assert "SulAmérica" in notices[0]
    assert ipn.FENCE_OPEN in answer


@pytest.mark.parametrize(
    "asked",
    ["Plano Teste Bateria", "plano teste bateria", "PlanoTesteBateria", "plano-teste bateria"],
)
async def test_symptom_the_outro_with_a_rule_is_found_by_its_name(db, asked):
    tenant = await _clinic(db, TenantInsurancePlan(custom_name=OUTRO, custom_payment_note=RULE))
    _answer, notices = await _ask(tenant.id, asked)
    assert notices == [f"Regra de pagamento cadastrada pela clínica para {OUTRO}:\n\n{RULE}"]


@pytest.mark.parametrize(
    "slug, asked",
    [
        ("bradesco-saude", "bradesco"),
        ("notredame-intermedica", "notredame"),
        ("notredame-intermedica", "intermedica"),
        ("porto-seguro-saude", "porto seguro"),
        ("golden-cross", "golden"),
        ("prevent-senior", "prevent"),
        ("sulamerica", "sul america saude"),
        ("assim-saude", "assim"),
    ],
)
async def test_common_aliases_find_the_catalog_convenio(db, slug, asked):
    tenant = await _clinic(
        db, TenantInsurancePlan(catalog_id=catalog_id_for_slug(slug), custom_payment_note=RULE)
    )
    _answer, notices = await _ask(tenant.id, asked)
    assert len(notices) == 1 and RULE in notices[0]


async def test_an_unknown_convenio_is_still_the_fixed_offer(db):
    tenant = await _clinic(db, TenantInsurancePlan(catalog_id=SULAMERICA, custom_payment_note=RULE))
    with pytest.raises(ai_tools.HumanHandoffRequested):
        await _ask(tenant.id, "Hapvida")


async def test_a_name_matching_two_convenios_makes_the_ai_ask_which(db):
    tenant = await _clinic(
        db,
        TenantInsurancePlan(catalog_id=BRADESCO, custom_payment_note="Regra do Bradesco Saúde"),
        TenantInsurancePlan(custom_name="Bradesco", custom_payment_note="Regra do outro"),
    )
    answer, notices = await _ask(tenant.id, "bradesco")
    assert notices == []
    assert "Bradesco Saúde" in answer and "Bradesco" in answer
    assert "qual" in answer.lower()
    assert "Regra do" not in answer  # no rule text before the patient says which


# ------------------------------------------------- the lookup aliases stay out of the flow


async def test_lookup_aliases_never_change_the_flows_matching(db):
    async with db() as session:
        entry = await session.get(InsuranceCatalog, BRADESCO)
    assert "bradesco" not in catalog_match_keys(entry)  # the flow and the legacy reconcile
    assert "bradesco" in {ipn.compact_key(key) for key in catalog_lookup_keys(entry)}
    seeded = {
        "alice",
        "amil",
        "assim-saude",
        "bradesco-saude",
        "care-plus",
        "cassi",
        "golden-cross",
        "hapvida",
        "notredame-intermedica",
        "omint",
        "porto-seguro-saude",
        "prevent-senior",
        "sulamerica",
        "unimed",
    }
    assert set(CATALOG_LOOKUP_ALIASES) <= seeded


# ------------------------------------------------------------------------- the prompts


def test_how_does_x_work_triggers_the_tool_in_both_prompts():
    for rule in (prompts.INSURANCE_PAYMENT_HANDOFF_RULE, prompts.INSURANCE_PAYMENT_HANDOFF):
        assert "como funciona" in rule
        assert "get_insurance_payment_rule" in rule
    assert "como funciona" in prompts_v2.INSURANCE_PAYMENT_RULE_V2
