"""TASK-094 D4 - the AI gave the OLD clinic address with a junk CEP.

Battery 2026-11-26 (Chrysostomo): "Qual o endereço da clínica?" -> "Fica na Rua Constante Ramos,
122, Copacabana, Rio de Janeiro - RJ, 123121." The Sede's CEP is 22051-012; "123121" came from
the legacy clinic-level `tenants.address`, which the prompt's "SOBRE A CLÍNICA" block read.

Owner's decision (TASK-094): the address the AI knows comes from the USABLE units (active and
with a complete address), each with its name. "Depende do momento": a chosen unit - or a chosen
doctor who attends only ONE usable unit - means only that address; otherwise every usable unit.
The legacy address never reaches the patient: not in the prompt, not in the booking details,
not in the appointment-edit recap. The config PUT stops writing it (accepted and ignored).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from uuid import uuid4

import pytest
import pytest_asyncio
from langchain_core.messages import HumanMessage

from secretaria.ai import graph, prompts_v2
from secretaria.ai.prompts import secretary_system_prompt
from secretaria.ai.prompts_v2 import secretary_system_prompt_v2
from secretaria.models import Conversation, FlowState, Professional, Tenant
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.services import appointment_edit as ae, appointment_edit_flow as aef
from secretaria.services.hub_configuration import tenant_scalar_updates
from secretaria.services.llm_context import selected_unit_id
from secretaria.services.tenant_config import (
    RuntimeUnit,
    TenantRuntimeConfig,
    load_tenant_config,
)
from secretaria.workers.shared.greeting import _flow_tenant_snapshot
from tests import task094_support as support
from tests.payments_support import make_sessionmaker

# The REAL usable-unit rule (active + complete address), not the suite's "every active unit"
# default (tests/conftest.py::_units_usable_by_default).
pytestmark = pytest.mark.real_unit_gate

LEGACY_JUNK = "123121"
CENTRO_LINE = "Av. Atlântica, 500, Copacabana, Rio de Janeiro - RJ, 22021-001"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


def _config(**overrides) -> TenantRuntimeConfig:
    base = TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="Chrysostomo For Eyes",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=None,
    )
    return replace(base, **overrides)


SEDE = RuntimeUnit(id=uuid4(), name="Sede", address=support.SEDE_LINE, professional_ids=frozenset())
CENTRO = RuntimeUnit(id=uuid4(), name="Centro", address=CENTRO_LINE, professional_ids=frozenset())


def _prompts(config: TenantRuntimeConfig, monkeypatch) -> tuple[str, str]:
    monkeypatch.setattr(prompts_v2, "_clinic_today", lambda _tz: date(2026, 11, 26))
    v2 = secretary_system_prompt_v2(
        config, tool_names=frozenset({"set_booking_draft", "show_main_menu"})
    )
    return secretary_system_prompt(config), v2


async def _clinic(db, *, legacy=support.LEGACY_ADDRESS) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Chrysostomo For Eyes",
            phone_number_id=None,
            timezone="America/Sao_Paulo",
            address=legacy,
        )
        session.add(tenant)
        await session.commit()
        return tenant


async def _loaded(db, tenant) -> TenantRuntimeConfig:
    async with db() as session:
        return await load_tenant_config(session, await session.get(Tenant, tenant.id))


# ------------------------------------------------------------------ the symptom, end to end


async def test_symptom_the_prompt_gives_the_sede_address_never_the_legacy_one(db, monkeypatch):
    tenant = await _clinic(db)
    async with db() as session:
        session.add(support.unit(tenant.id, "Sede"))
        await session.commit()
    config = await _loaded(db, tenant)
    for prompt in _prompts(config, monkeypatch):
        assert support.SEDE_LINE in prompt
        assert "Sede" in prompt
        assert LEGACY_JUNK not in prompt


async def test_only_usable_units_are_listed_each_with_its_name(db, monkeypatch):
    tenant = await _clinic(db)
    async with db() as session:
        session.add_all(
            [
                support.unit(tenant.id, "Sede"),
                support.unit(
                    tenant.id, "Centro", street="Av. Atlântica", number="500", cep="22021001"
                ),
                support.unit(tenant.id, "Velha", street="Rua Antiga", number=None),  # incomplete
                support.unit(tenant.id, "Fechada", street="Rua Fechada", active=False),
            ]
        )
        await session.commit()
    config = await _loaded(db, tenant)
    assert {u.name for u in config.units} == {"Sede", "Centro"}
    for prompt in _prompts(config, monkeypatch):
        assert support.SEDE_LINE in prompt and CENTRO_LINE in prompt
        assert "Rua Antiga" not in prompt and "Rua Fechada" not in prompt
        assert LEGACY_JUNK not in prompt


async def test_no_usable_unit_means_no_address_line_at_all(db, monkeypatch):
    tenant = await _clinic(db)
    config = await _loaded(db, tenant)
    assert config.units == ()
    for prompt in _prompts(config, monkeypatch):
        assert "Rua Constante Ramos" not in prompt and LEGACY_JUNK not in prompt


async def test_the_units_carry_who_attends_there(db):
    tenant = await _clinic(db)
    async with db() as session:
        sede = support.unit(tenant.id, "Sede")
        centro = support.unit(tenant.id, "Centro", number="500")
        ana = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        bia = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Bia", is_active=True)
        session.add_all([sede, centro, ana, bia])
        await session.flush()
        session.add_all(
            [
                ProfessionalUnit(professional_id=ana.id, unit_id=sede.id, is_active=True),
                ProfessionalUnit(professional_id=bia.id, unit_id=sede.id, is_active=True),
                ProfessionalUnit(professional_id=bia.id, unit_id=centro.id, is_active=True),
                # An inactive link does not count.
                ProfessionalUnit(professional_id=ana.id, unit_id=centro.id, is_active=False),
            ]
        )
        await session.commit()
    config = await _loaded(db, tenant)
    by_name = {u.name: u for u in config.units}
    assert by_name["Sede"].professional_ids == frozenset({ana.id, bia.id})
    assert by_name["Centro"].professional_ids == frozenset({bia.id})


# ---------------------------------------------------------------- "depende do momento"


def test_with_a_chosen_unit_only_that_address(monkeypatch):
    config = _config(units=(SEDE, CENTRO), selected_unit_id=CENTRO.id)
    for prompt in _prompts(config, monkeypatch):
        assert CENTRO_LINE in prompt
        assert support.SEDE_LINE not in prompt


def test_a_chosen_doctor_with_one_unit_gives_only_that_address(monkeypatch):
    ana, bia = uuid4(), uuid4()
    sede = replace(SEDE, professional_ids=frozenset({ana, bia}))
    centro = replace(CENTRO, professional_ids=frozenset({bia}))
    only_sede = _config(units=(sede, centro), professional_id=ana)
    for prompt in _prompts(only_sede, monkeypatch):
        assert support.SEDE_LINE in prompt and CENTRO_LINE not in prompt
    both = _config(units=(sede, centro), professional_id=bia)
    for prompt in _prompts(both, monkeypatch):
        assert support.SEDE_LINE in prompt and CENTRO_LINE in prompt


def test_nothing_chosen_lists_every_unit_with_its_name(monkeypatch):
    config = _config(units=(SEDE, CENTRO))
    for prompt in _prompts(config, monkeypatch):
        assert "Sede" in prompt and "Centro" in prompt
        assert support.SEDE_LINE in prompt and CENTRO_LINE in prompt


def test_an_unknown_chosen_unit_falls_back_to_every_unit(monkeypatch):
    config = _config(units=(SEDE, CENTRO), selected_unit_id=uuid4())
    for prompt in _prompts(config, monkeypatch):
        assert support.SEDE_LINE in prompt and CENTRO_LINE in prompt


def test_the_turn_unit_comes_from_the_booking_or_the_edit_draft():
    unit_id = uuid4()
    booking = Conversation(id=uuid4(), flow_selected_unit_id=unit_id)
    assert selected_unit_id(booking) == unit_id
    edit = Conversation(
        id=uuid4(),
        flow_state=FlowState.EDIT_BOOKING,
        flow_edit_draft={"current": {"unit_id": str(unit_id)}},
    )
    assert selected_unit_id(edit) == unit_id
    assert selected_unit_id(Conversation(id=uuid4())) is None
    assert selected_unit_id(None) is None


async def test_run_agent_puts_the_turn_unit_on_the_prompt_config(monkeypatch):
    seen: dict = {}

    async def _history(_cid):
        return [HumanMessage(content="Qual o endereço da clínica?")]

    async def _model(_messages, _cid):
        seen["config"] = graph._tenant_config_ctx.get()
        return "Fica na Sede."

    monkeypatch.setattr(graph, "_load_history", _history)
    monkeypatch.setattr(graph, "_invoke_agent_with_retry", _model)
    reply = await graph.run_agent(
        "Qual o endereço da clínica?",
        context={"conversation_id": str(uuid4())},
        tenant_config=_config(units=(SEDE, CENTRO)),
        selected_unit_id=CENTRO.id,
    )
    assert reply == "Fica na Sede."
    assert seen["config"].selected_unit_id == CENTRO.id


async def test_the_worker_passes_the_conversations_unit_to_the_agent(db, monkeypatch):
    agent = support.wire(monkeypatch, db)
    tenant, conversation, (_sede, centro) = await support.seed(db, units=2)
    async with db() as session:
        row = await session.get(Conversation, conversation.id)
        row.flow_selected_unit_id = centro.id
        await session.commit()
    agent["reply"] = "Fica no Centro."
    await support.turn(tenant, "Qual o endereço da clínica?")
    assert agent["calls"] == 1
    assert agent["kwargs"][0]["selected_unit_id"] == centro.id


# ------------------------------------------------------- booking details and the edit recap


def _tenant_row(**extra) -> Tenant:
    return Tenant(
        id=uuid4(),
        clinic_name="C",
        appointment_types=[],
        business_hours={},
        appointment_duration_min=30,
        insurances=[],
        initial_flows={},
        address=support.LEGACY_ADDRESS,
        **extra,
    )


def test_the_flow_snapshot_never_carries_the_legacy_address():
    tenant = _tenant_row()
    sede = support.unit(tenant.id, "Sede")
    centro = support.unit(tenant.id, "Centro", number="500")
    incomplete = support.unit(tenant.id, "Velha", number=None)
    none = _flow_tenant_snapshot(tenant, [], usable_units=[])
    assert none.clinic_address is None and none.sole_unit is None
    assert getattr(none, "address", None) is None
    one = _flow_tenant_snapshot(tenant, [], usable_units=[sede, incomplete])
    assert one.clinic_address == f"Sede — {support.SEDE_LINE}"
    assert (one.sole_unit.name, one.sole_unit.address) == ("Sede", support.SEDE_LINE)
    two = _flow_tenant_snapshot(tenant, [], usable_units=[sede, centro])
    assert two.clinic_address is None and two.sole_unit is None
    for snapshot in (none, one, two):
        assert LEGACY_JUNK not in str(vars(snapshot))


def _draft_without_unit() -> ae.EditDraft:
    values = {
        "service": "Consulta",
        "professional_id": None,
        "start_at": "2026-12-01T10:00",
        "end_at": "2026-12-01T10:30",
        "insurance": None,
        "attendee_name": None,
    }
    return ae.EditDraft(appointment_id=str(uuid4()), current=dict(values), original=dict(values))


def test_the_edit_recap_uses_the_only_usable_unit_or_no_address():
    tenant = _tenant_row()
    sede = support.unit(tenant.id, "Sede")
    centro = support.unit(tenant.id, "Centro", number="500")
    draft = _draft_without_unit()
    one = _flow_tenant_snapshot(tenant, [], usable_units=[sede])
    assert aef._recap_place(draft, None, one) == ("Sede", support.SEDE_LINE)
    two = _flow_tenant_snapshot(tenant, [], usable_units=[sede, centro])
    assert aef._recap_place(draft, None, two) == (None, None)
    none = _flow_tenant_snapshot(tenant, [], usable_units=[])
    assert aef._recap_place(draft, None, none) == (None, None)


# ------------------------------------------------------------ the config PUT stops writing it


def test_the_scalar_updates_drop_the_legacy_address():
    updates = tenant_scalar_updates({"address": {"line": "Rua X"}, "clinic_facts": {}})
    assert "address" not in updates
    assert "clinic_facts" in updates


@pytest.mark.parametrize("path", ["/tenants/me/config"])
async def test_the_config_put_accepts_and_ignores_the_address(db, path):
    from fastapi import Depends
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import AsyncSession

    from secretaria.api.hub.deps import get_current_tenant
    from secretaria.core.database import get_session
    from secretaria.main import app

    tenant = await _clinic(db, legacy=None)

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
            response = await client.put(
                path, json={"address": {"line": "Rua Nova, 1", "city": "Recife"}}
            )
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_current_tenant, None)
    assert response.status_code == 200
    async with db() as session:
        assert (await session.get(Tenant, tenant.id)).address is None
