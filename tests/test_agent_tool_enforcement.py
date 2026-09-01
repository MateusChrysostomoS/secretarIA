"""The agent's capabilities are enforced by the tool set, not by the prompt.

A multi-professional clinic's every booking belongs to ONE professional's
agenda. The prompt used to be the only thing saying so while
`create_event`/`cancel_event`/`check_availability`/`list_free_slots` — all
tenant-level — stayed on the table, so a single bad tool choice could write to
the clinic-level calendar and persist an ownerless appointment. Entitlements
could not fix that: they only ADD tools, never remove the unsafe alternative.

Here the capability set is derived from the tenant's REAL shape
(services/booking_scope.py's topology) and asserted by exact tool names, with
a second lock inside each tool for anything that arrives anyway.

`create_react_agent` is replaced by a recording fake — nothing here reaches
OpenAI, Google or a DB.
"""

import asyncio
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from secretaria.ai import (
    graph,  # noqa: E402
    tools as ai_tools,  # noqa: E402
)
from secretaria.ai.tools import manage_existing_appointment  # noqa: E402
from secretaria.plugins import (
    multi_professional as mp,  # noqa: E402
    multi_unit as mu,  # noqa: E402
    registry as reg,  # noqa: E402
)
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402

_TENANT_LEVEL_CALENDAR_TOOLS = {
    "check_availability",
    "list_free_slots",
    "create_event",
    "cancel_event",
}
_SCOPE_FREE_TOOLS = {"iniciar_pre_consulta", "list_patient_appointments", "show_main_menu"}

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "analytics_bi_advanced": False,
    "human_backup_24_7": False,
}


def _summary(**addons) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={**_ALL_ADDONS_OFF, **addons},
        limits={},
    )


def _config(tenant_id=None, professional_id=None) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id or uuid4(),
        clinic_name="Clinica",
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="cal",
        google_refresh_token=None,
        professional_id=professional_id,
    )


class _RecordingAgent:
    """Stand-in for the compiled LangGraph: records its tools, never calls an LLM."""

    def __init__(self, tools):
        self.tools = tools

    async def ainvoke(self, state):
        return {"messages": [AIMessage(content="ok")]}


@pytest.fixture(autouse=True)
def _fake_compile(monkeypatch: pytest.MonkeyPatch):
    graph._AGENTS.clear()

    def _fake_create_react_agent(model, tools, prompt):
        return _RecordingAgent(list(tools))

    async def _empty_history(_conversation_id):
        return []

    monkeypatch.setattr(graph, "create_react_agent", _fake_create_react_agent)
    monkeypatch.setattr(graph, "_load_history", _empty_history)
    yield
    graph._AGENTS.clear()


def _names(tools) -> set[str]:
    return {getattr(t, "name", str(t)) for t in tools}


# --------------------------------------------------------------------------
# The capability matrix: topology x entitlement
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "topology", [BOOKING_TOPOLOGY_UNKNOWN, BOOKING_TOPOLOGY_NONE, BOOKING_TOPOLOGY_SOLE]
)
def test_tenant_level_topologies_keep_the_historical_base_set(topology):
    names = _names(graph.base_tools_for(topology))
    assert names == _TENANT_LEVEL_CALENDAR_TOOLS | _SCOPE_FREE_TOOLS


def test_multi_professional_never_gets_a_tenant_level_calendar_tool():
    names = _names(graph.base_tools_for(BOOKING_TOPOLOGY_MULTI))
    assert names == _SCOPE_FREE_TOOLS
    assert names.isdisjoint(_TENANT_LEVEL_CALENDAR_TOOLS)


def test_multi_professional_with_the_addon_gets_owner_scoped_tools_instead():
    """The safe alternative replaces the unsafe one — it does not sit beside it."""
    entitled = _summary(multi_professional=True)
    tools = [*graph.base_tools_for(BOOKING_TOPOLOGY_MULTI), *reg.agent_tools_for(entitled)]
    names = _names(tools)
    assert names.isdisjoint(_TENANT_LEVEL_CALENDAR_TOOLS)
    assert {
        mp.list_professionals.name,
        mp.list_free_slots_for_professional.name,
        mp.create_event_for_professional.name,
        mp.select_professional_and_continue.name,
    } <= names


def test_multi_professional_without_the_addon_degrades_to_the_button_flow():
    """No entitlement means no booking tool at all — never a broader one."""
    tools = [*graph.base_tools_for(BOOKING_TOPOLOGY_MULTI), *reg.agent_tools_for(_summary())]
    names = _names(tools)
    assert names.isdisjoint(_TENANT_LEVEL_CALENDAR_TOOLS)
    assert not any(name.startswith("create_event") for name in names)
    # The way back to the deterministic flow always survives.
    assert "show_main_menu" in names


def test_multi_professional_manages_existing_appointments_through_the_flow():
    """The worker's own composition for a flow-enabled tenant (workers/tasks.py)."""
    entitled = _summary(multi_professional=True)
    tools = [
        *graph.base_tools_for(BOOKING_TOPOLOGY_MULTI),
        *reg.agent_tools_for(entitled),
        manage_existing_appointment,
    ]
    names = _names(tools)
    # Reschedule/cancel hand back to the deterministic manage flow, which
    # resolves the appointment's OWN professional - never `cancel_event`.
    assert "manage_existing_appointment" in names
    assert "cancel_event" not in names


def test_single_professional_keeps_booking_through_the_base_tool():
    tools = [*graph.base_tools_for(BOOKING_TOPOLOGY_SOLE), *reg.agent_tools_for(_summary())]
    assert "create_event" in _names(tools)


# --------------------------------------------------------------------------
# The cache key describes the EFFECTIVE set
# --------------------------------------------------------------------------


def test_topology_alone_yields_distinct_cached_agents():
    sole = graph.build_agent((), BOOKING_TOPOLOGY_SOLE)
    multi = graph.build_agent((), BOOKING_TOPOLOGY_MULTI)
    assert sole is not multi
    assert len(graph._AGENTS) == 2
    assert "create_event" not in _names(multi.tools)


def test_same_topology_and_tools_reuse_one_agent():
    first = graph.build_agent((mp.list_professionals,), BOOKING_TOPOLOGY_MULTI)
    second = graph.build_agent((mp.list_professionals,), BOOKING_TOPOLOGY_MULTI)
    assert first is second
    assert len(graph._AGENTS) == 1


# --------------------------------------------------------------------------
# run_agent wires the topology through
# --------------------------------------------------------------------------


async def test_run_agent_builds_the_multi_professional_set(monkeypatch):
    seen: list[set[str]] = []

    async def _capture(messages):
        agent = graph.build_agent(
            graph._extra_tools_ctx.get(), ai_tools._booking_topology_ctx.get()
        )
        seen.append(_names(agent.tools))
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _capture)
    await graph.run_agent(
        "oi",
        context={"conversation_id": str(uuid4())},
        tenant_config=_config(),
        extra_tools=[mp.create_event_for_professional],
        booking_topology=BOOKING_TOPOLOGY_MULTI,
    )
    assert seen[0].isdisjoint(_TENANT_LEVEL_CALENDAR_TOOLS)
    assert mp.create_event_for_professional.name in seen[0]


async def test_run_agent_resets_the_topology_context_var():
    before = ai_tools._booking_topology_ctx.get()
    await graph.run_agent(
        "oi",
        context={"conversation_id": str(uuid4())},
        tenant_config=_config(),
        booking_topology=BOOKING_TOPOLOGY_MULTI,
    )
    assert ai_tools._booking_topology_ctx.get() == before == BOOKING_TOPOLOGY_UNKNOWN


async def test_concurrent_tenants_never_share_topology_or_tool_set(monkeypatch):
    """Two tenants in flight at once keep their own capabilities throughout."""
    multi_started = asyncio.Event()
    sole_checked = asyncio.Event()
    observed: dict[str, set[str]] = {}

    async def _interleaved(messages):
        topology = ai_tools._booking_topology_ctx.get()
        agent = graph.build_agent(graph._extra_tools_ctx.get(), topology)
        if topology == BOOKING_TOPOLOGY_SOLE:
            # Hold the turn open across the OTHER tenant's whole invocation.
            await multi_started.wait()
            observed["sole"] = _names(agent.tools)
            assert ai_tools._booking_topology_ctx.get() == BOOKING_TOPOLOGY_SOLE
            sole_checked.set()
        else:
            multi_started.set()
            observed["multi"] = _names(agent.tools)
            await sole_checked.wait()
            assert ai_tools._booking_topology_ctx.get() == BOOKING_TOPOLOGY_MULTI
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _interleaved)
    await asyncio.gather(
        graph.run_agent(
            "oi",
            context={"conversation_id": str(uuid4())},
            tenant_config=_config(),
            booking_topology=BOOKING_TOPOLOGY_SOLE,
        ),
        graph.run_agent(
            "oi",
            context={"conversation_id": str(uuid4())},
            tenant_config=_config(),
            extra_tools=[mp.create_event_for_professional],
            booking_topology=BOOKING_TOPOLOGY_MULTI,
        ),
    )
    assert "create_event" in observed["sole"]
    assert observed["multi"].isdisjoint(_TENANT_LEVEL_CALENDAR_TOOLS)
    assert len(graph._AGENTS) == 2


# --------------------------------------------------------------------------
# Second lock: the tools themselves refuse, without calling Google
# --------------------------------------------------------------------------


class _ExplodingCalendar:
    """Any use is a bug: a blocked tool must never reach the calendar."""

    def __getattr__(self, name):
        raise AssertionError(f"calendar must not be touched (called {name})")


@pytest.fixture
def _multi_turn():
    tokens = [
        (
            ai_tools._booking_topology_ctx,
            ai_tools._booking_topology_ctx.set(BOOKING_TOPOLOGY_MULTI),
        ),
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(uuid4())),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(_ExplodingCalendar())),
    ]
    yield
    for var, token in reversed(tokens):
        var.reset(token)


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        (
            "create_event",
            {
                "start": "2026-08-03T08:00:00",
                "end": "2026-08-03T08:30:00",
                "summary": "Consulta",
            },
        ),
        ("cancel_event", {"event_id": "evt-1"}),
        ("check_availability", {"start": "2026-08-03T08:00:00", "end": "2026-08-03T08:30:00"}),
        ("list_free_slots", {"day": "2026-08-03"}),
    ],
)
async def test_tenant_level_tool_invoked_on_a_multi_turn_fails_closed(_multi_turn, tool, args):
    result = await getattr(ai_tools, tool).ainvoke(args)
    assert "error" in result
    # The refusal names the safe route without leaking anything about the clinic.
    assert "profissional" in result["error"].lower()


async def test_unit_booking_tool_also_fails_closed_on_a_multi_turn(_multi_turn):
    result = await mu.create_event_at_unit.ainvoke(
        {
            "unit_name": "Unidade Centro",
            "start": "2026-08-03T08:00:00",
            "end": "2026-08-03T08:30:00",
            "summary": "Consulta",
        }
    )
    assert "error" in result


async def test_tenant_level_tools_still_work_on_a_single_professional_turn():
    """The guard is topology-scoped: it must not disarm a working clinic."""

    class _Calendar:
        async def check_availability(self, start, end):
            return []

    token = ai_tools._booking_topology_ctx.set(BOOKING_TOPOLOGY_SOLE)
    cal_token = ai_tools._calendar_ctx.set(_Calendar())
    try:
        result = await ai_tools.check_availability.ainvoke(
            {"start": "2026-08-03T08:00:00", "end": "2026-08-03T08:30:00"}
        )
    finally:
        ai_tools._calendar_ctx.reset(cal_token)
        ai_tools._booking_topology_ctx.reset(token)
    assert result == {"busy": []}


def test_block_reasons_are_stable_enums():
    assert ai_tools.TOOL_BLOCK_WRONG_TOPOLOGY == "wrong_topology"
    assert ai_tools.TOOL_BLOCK_UNKNOWN_SERVICE == "unknown_service"
    assert ai_tools.TOOL_BLOCK_AMBIGUOUS_SERVICE == "ambiguous_service"
