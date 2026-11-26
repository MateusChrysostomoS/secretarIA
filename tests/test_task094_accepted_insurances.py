"""TASK-094 D5 - the AI did not know WHICH convênios the clinic accepts.

Battery 2026-11-26 (Chrysostomo, IA-7): "Quais convênios vocês aceitam?" -> "A clínica aceita 6
convênios, mas não tenho a lista completa aqui para te mostrar." The prompt only ever carried the
COUNT (TASK-086 A16 took the plan names out of the prompt on purpose - kept that way).

Owner's decision (TASK-094): a new read-only tool `list_accepted_insurances` returns only the
NAMES of the accepted, switched-on convênios, in the order the patient sees them in Agendar
(an "Outro" switched on included, by the name the clinic gave it). Mode "cada médico com sua
lista": with a chosen doctor, his list; without, the union and a note that it depends on the
professional. The prompts (v1 and v2) say: "quais convênios vocês aceitam?" / "aceitam X?" ->
call it and answer with the list; X not in it -> it is not among the accepted ones (and say
which are). Never a human for that.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio

from secretaria.ai import graph, prompts, prompts_v2, tools as ai_tools
from secretaria.ai.prompts import secretary_system_prompt
from secretaria.ai.prompts_v2 import secretary_system_prompt_v2
from secretaria.ai.tool_output import V2_TOOL_OUTPUTS
from secretaria.core import database as core_database
from secretaria.models import Conversation, Patient, Professional, Tenant
from secretaria.models.insurance import ProfessionalInsurancePlan, TenantInsurancePlan
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.models.unit import Unit
from secretaria.services.insurance_catalog import (
    catalog_id_for_slug,
    ensure_catalog_seeded,
    load_tenant_insurance,
)
from secretaria.services.tenant_config import ClinicFeatures, TenantRuntimeConfig
from tests.payments_support import make_sessionmaker

UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")
SULAMERICA = catalog_id_for_slug("sulamerica")
OUTRO = "Plano Teste Bateria"


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    monkeypatch.setattr(core_database, "async_session_factory", maker)
    yield maker
    await engine.dispose()


async def _clinic(db, mode: str = "shared") -> Tenant:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="C", phone_number_id=None, insurance_mode=mode)
        session.add(tenant)
        await session.commit()
        return tenant


async def _list(tenant_id, *, conversation_id=None) -> str:
    tokens = (
        ai_tools._tenant_id_ctx.set(tenant_id),
        ai_tools._conversation_id_ctx.set(conversation_id),
    )
    try:
        return await ai_tools.list_accepted_insurances.ainvoke({})
    finally:
        ai_tools._conversation_id_ctx.reset(tokens[1])
        ai_tools._tenant_id_ctx.reset(tokens[0])


def _names_in(answer: str) -> list[str]:
    """The names the tool lists, in order (one per line, "- " bullets)."""
    return [line[2:].strip() for line in answer.splitlines() if line.startswith("- ")]


# ------------------------------------------------------------------ the symptom (shared mode)


async def test_symptom_the_six_accepted_convenios_are_listed_by_name(db):
    tenant = await _clinic(db)
    async with db() as session:
        session.add_all(
            [
                TenantInsurancePlan(tenant_id=tenant.id, catalog_id=UNIMED),
                TenantInsurancePlan(tenant_id=tenant.id, catalog_id=AMIL),
                TenantInsurancePlan(tenant_id=tenant.id, catalog_id=SULAMERICA),
                TenantInsurancePlan(tenant_id=tenant.id, catalog_id=catalog_id_for_slug("cassi")),
                TenantInsurancePlan(tenant_id=tenant.id, catalog_id=catalog_id_for_slug("omint")),
                TenantInsurancePlan(tenant_id=tenant.id, custom_name=OUTRO),
                # An "Outro" switched OFF is not accepted.
                TenantInsurancePlan(
                    tenant_id=tenant.id, custom_name="Plano Desligado", is_active=False
                ),
            ]
        )
        await session.commit()
    answer = await _list(tenant.id)
    names = _names_in(answer)
    assert len(names) == 6
    assert OUTRO in names and "Plano Desligado" not in names
    # The order the patient sees in Agendar (the flow's own list).
    async with db() as session:
        flow = [plan["name"] for plan in (await load_tenant_insurance(session, tenant.id)).plans]
    assert names == flow
    assert "não tenho a lista" not in answer.lower()
    assert "depende do profissional" not in answer


async def test_without_convenios_the_answer_is_particular_only(db):
    tenant = await _clinic(db)
    answer = await _list(tenant.id)
    assert _names_in(answer) == []
    assert "particular" in answer.lower()


async def test_no_clinic_is_an_error_not_a_list(db):
    answer = await _list(None)
    assert _names_in(answer) == []
    assert "não consegui" in answer.lower()


# --------------------------------------------------------- "cada médico com sua lista" mode


async def _independent(db):
    tenant = await _clinic(db, mode="independent")
    async with db() as session:
        ana = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        bia = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Bia", is_active=True)
        session.add_all([ana, bia])
        await session.flush()
        session.add_all(
            [
                ProfessionalInsurancePlan(
                    professional_id=ana.id, tenant_id=tenant.id, catalog_id=UNIMED
                ),
                ProfessionalInsurancePlan(
                    professional_id=bia.id, tenant_id=tenant.id, catalog_id=UNIMED
                ),
                ProfessionalInsurancePlan(
                    professional_id=bia.id, tenant_id=tenant.id, catalog_id=AMIL
                ),
                ProfessionalInsurancePlan(
                    professional_id=bia.id, tenant_id=tenant.id, custom_name=OUTRO
                ),
            ]
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999990000")
        session.add(patient)
        await session.flush()
        with_ana = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            flow_selected_professional_id=ana.id,
        )
        session.add(with_ana)
        await session.commit()
    return tenant, ana, bia, with_ana


async def test_with_a_chosen_doctor_only_his_list(db):
    tenant, _ana, _bia, with_ana = await _independent(db)
    answer = await _list(tenant.id, conversation_id=with_ana.id)
    assert _names_in(answer) == ["Unimed"]
    assert "depende do profissional" not in answer


async def test_without_a_doctor_the_union_and_it_depends_on_the_professional(db):
    tenant, _ana, _bia, _with_ana = await _independent(db)
    answer = await _list(tenant.id)
    assert set(_names_in(answer)) == {"Unimed", "Amil", OUTRO}
    assert len(_names_in(answer)) == 3  # Unimed once, not once per doctor
    assert "depende do profissional" in answer


@pytest.mark.real_unit_gate
async def test_a_doctor_hidden_from_patients_lends_no_convenio(db):
    tenant, _ana, bia, _with_ana = await _independent(db)
    async with db() as session:
        closed = Unit(id=uuid4(), tenant_id=tenant.id, name="Fechada", is_active=False)
        session.add(closed)
        await session.flush()
        session.add(ProfessionalUnit(professional_id=bia.id, unit_id=closed.id, is_active=True))
        await session.commit()
    answer = await _list(tenant.id)
    assert _names_in(answer) == ["Unimed"]


# --------------------------------------------------------------------- registry and prompts


def test_the_tool_is_registered_read_only_on_every_toolset():
    from secretaria.workers.shared.llm_context import _flow_handback_tools

    assert V2_TOOL_OUTPUTS["list_accepted_insurances"].text is True
    tenant = Tenant(id=uuid4(), clinic_name="C", initial_flows={})
    names = {t.name for t in _flow_handback_tools(tenant, "sole", [])}
    assert "list_accepted_insurances" in names
    editing = {
        t.name
        for t in graph.effective_tools("sole", [ai_tools.list_accepted_insurances], editing=True)
    }
    assert "list_accepted_insurances" in editing


def _config(plans: int) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="C",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
        clinic_features=ClinicFeatures(deposit_enabled=False, insurance_plan_count=plans),
    )


def test_both_prompts_send_the_question_to_the_tool_never_to_a_human(monkeypatch):
    monkeypatch.setattr(prompts_v2, "_clinic_today", lambda _tz: date(2026, 11, 26))
    names = frozenset({"set_booking_draft", "show_main_menu", "list_accepted_insurances"})
    v1 = secretary_system_prompt(_config(6))
    v2 = secretary_system_prompt_v2(_config(6), tool_names=names)
    assert prompts.ACCEPTED_INSURANCES_RULE in v1
    assert prompts.ACCEPTED_INSURANCES_RULE in v2
    rule = prompts.ACCEPTED_INSURANCES_RULE
    for words in ("list_accepted_insurances", "quais convênios", "não consta", "atendente"):
        assert words in rule
    # The tool's own short line in the v2 tool list (tests/test_prompts_v2.py checks the size
    # ceiling WITH it).
    assert any(line.startswith("- list_accepted_insurances") for line in v2.splitlines())
    # The old "o fluxo mostra a lista" (the model's excuse) is gone.
    for prompt in (v1, v2):
        assert "o fluxo mostra a lista" not in prompt
