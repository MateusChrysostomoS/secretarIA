"""The AI toolset v2: what the AI loses and what it gains, by exact tool names (TASK-030 P4).

On a clinic with `initial_flows["ai_draft_v2"] = true` the AI no longer has a tool that reads a
busy interval (`ai/tools.py::AI_TOOLSET_V2_WITHHELD`, with the two plugin writers that have no
v2 variant) nor the LEGACY `create_event`/`cancel_event` (`AI_TOOLSET_V2_STAGING`). It reads
free windows through `get_availability`, and the names `create_event`/`cancel_event` are the
blind tools of ai/staging_tools.py, which only stage the patient's confirmation card. With
the switch off, the toolset is exactly what it was.

This is lock one: the tool set (`graph.effective_tools`, asserted by exact tool NAMES, the way
tests/test_agent_tool_enforcement.py does it for topologies, and by IDENTITY where a name has
two implementations). Lock two - each withheld or legacy tool refusing by itself - is
tests/test_ai_toolset_v2_locks.py. `create_react_agent` is replaced by a recording fake:
nothing here reaches OpenAI, Google or a DB.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    tools as ai_tools,
)
from secretaria.ai.availability_tool import get_availability  # noqa: E402
from secretaria.ai.staging_tools import cancel_event_v2, create_event_v2  # noqa: E402
from secretaria.plugins import (  # noqa: E402
    multi_professional as mp,
    registry as reg,
)
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.calendar import CalendarService, CalendarUnavailableError  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402
from secretaria.workers.shared.llm_context import (  # noqa: E402
    _ai_toolset_v2,
    _flow_handback_tools,
)

TOPOLOGIES = [
    BOOKING_TOPOLOGY_UNKNOWN,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_MULTI,
]
TENANT_ON = SimpleNamespace(initial_flows={"ai_draft_v2": True})
TENANT_OFF = SimpleNamespace(initial_flows={})

_SCOPE_FREE = {
    "iniciar_pre_consulta",
    "list_patient_appointments",
    "show_main_menu",
    "get_service_info",
}
_TENANT_LEVEL_AGENDA_TOOLS = {
    "check_availability",
    "list_free_slots",
    "create_event",
    "cancel_event",
}
_PLUGIN_AGENDA_TOOLS = {
    "list_free_slots_for_professional",
    "create_event_for_professional",
    "create_event_at_unit",
}
_BUSY_AND_WRITE = _TENANT_LEVEL_AGENDA_TOOLS | _PLUGIN_AGENDA_TOOLS
_WITHHELD = {
    "check_availability",
    "list_free_slots",
    "list_free_slots_for_professional",
    "create_event_for_professional",
    "create_event_at_unit",
}
_STAGING = {"create_event", "cancel_event"}
_V2_READS_AND_STAGING = {"get_availability", *_STAGING}
_PLUGIN_TOOLS_KEPT = {"list_professionals", "select_professional_and_continue", "list_units"}
_HANDBACKS = {
    "manage_existing_appointment", "set_booking_draft",
    "request_human_handoff", "offer_human_handoff",
}

_ADDONS_OFF = {
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
        addons={**_ADDONS_OFF, **addons},
        limits={},
    )


ALL_ADDONS = _summary(multi_professional=True, multi_unit=True)


def _config(tenant_id=None) -> TenantRuntimeConfig:
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
    )


def _names(tools) -> set[str]:
    return {getattr(t, "name", str(t)) for t in tools}


def _staging_tools(tools) -> list:
    return [t for t in tools if getattr(t, "name", None) in _STAGING]


def _turn_tools(tenant, topology, *, addons=ALL_ADDONS):
    """What the worker hands the agent for one turn: `extra_tools` + the v2 flag."""
    extra = _flow_handback_tools(tenant, topology, reg.agent_tools_for(addons))
    return graph.effective_tools(topology, extra, toolset_v2=_ai_toolset_v2(tenant))


class _RecordingAgent:
    def __init__(self, tools):
        self.tools = tools

    async def ainvoke(self, state):
        return {"messages": [AIMessage(content="ok")]}


@pytest.fixture(autouse=True)
def _fake_compile(monkeypatch: pytest.MonkeyPatch):
    graph._AGENTS.clear()
    monkeypatch.setattr(
        graph, "create_react_agent", lambda model, tools, prompt: _RecordingAgent(list(tools))
    )

    async def _empty_history(_conversation_id):
        return []

    monkeypatch.setattr(graph, "_load_history", _empty_history)
    yield
    graph._AGENTS.clear()


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tenant, expected",
    [
        (None, False),
        (SimpleNamespace(initial_flows=None), False),
        (TENANT_OFF, False),
        (SimpleNamespace(initial_flows={"ai_draft_v2": False}), False),
        (SimpleNamespace(initial_flows={"ai_draft_v2": "true"}), False),
        (TENANT_ON, True),
    ],
)
def test_the_toolset_switch_reads_only_an_explicit_true(tenant, expected):
    assert _ai_toolset_v2(tenant) is expected


# --------------------------------------------------------------------------
# Lock one: the tool set, by exact names (and identity where a name has two tools)
# --------------------------------------------------------------------------


def _todays_tool_names(topology):
    """The tool names a turn had before TASK-030 P4, written out: the regression oracle."""
    names = _SCOPE_FREE | _PLUGIN_TOOLS_KEPT | _PLUGIN_AGENDA_TOOLS | _HANDBACKS
    if topology != BOOKING_TOPOLOGY_MULTI:
        names |= _TENANT_LEVEL_AGENDA_TOOLS | {"start_guided_booking"}
    return names


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_switch_off_is_exactly_todays_composition(topology):
    extras = reg.agent_tools_for(ALL_ADDONS)
    assert graph.effective_tools(topology, extras) == [*graph.base_tools_for(topology), *extras]
    tools = _turn_tools(TENANT_OFF, topology)
    assert _names(tools) == _todays_tool_names(topology)
    # Where today's set has create_event/cancel_event, they are the legacy ones.
    assert all(t in (ai_tools.create_event, ai_tools.cancel_event) for t in _staging_tools(tools))


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_switch_on_swaps_the_busy_readers_for_get_availability_and_the_blind_writers(topology):
    tools = _turn_tools(TENANT_ON, topology)
    expected = _SCOPE_FREE | _PLUGIN_TOOLS_KEPT | _HANDBACKS | _V2_READS_AND_STAGING
    if topology != BOOKING_TOPOLOGY_MULTI:
        expected |= {"start_guided_booking"}
    assert _names(tools) == expected
    assert _names(tools).isdisjoint(_WITHHELD)
    # Same names as the legacy writers, but the blind implementations - on every topology,
    # a multi-professional clinic included (the professional is a field of the draft).
    assert _staging_tools(tools) == [create_event_v2, cancel_event_v2]


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_switch_on_strips_a_withheld_or_legacy_tool_even_when_a_caller_passes_it(topology):
    extras = [
        *reg.agent_tools_for(ALL_ADDONS),
        ai_tools.create_event,
        ai_tools.cancel_event,
        ai_tools.check_availability,
    ]
    tools = graph.effective_tools(topology, extras, toolset_v2=True)
    assert _names(tools).isdisjoint(_BUSY_AND_WRITE)


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_a_staging_name_passes_only_as_its_blind_variant(topology):
    extras = [ai_tools.create_event, create_event_v2, ai_tools.cancel_event, cancel_event_v2]
    tools = graph.effective_tools(topology, extras, toolset_v2=True)
    assert _staging_tools(tools) == [create_event_v2, cancel_event_v2]


def test_without_the_addons_the_v2_set_still_has_the_way_back_to_the_flow():
    names = _names(_turn_tools(TENANT_ON, BOOKING_TOPOLOGY_SOLE, addons=_summary()))
    assert names == _SCOPE_FREE | _HANDBACKS | _V2_READS_AND_STAGING | {"start_guided_booking"}


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_the_v2_tools_are_offered_only_through_the_switch(topology):
    v2_tools = (get_availability, create_event_v2, cancel_event_v2)
    on = _flow_handback_tools(TENANT_ON, topology, [])
    assert all(t in on for t in v2_tools)
    for tenant in (TENANT_OFF, None):
        off = _flow_handback_tools(tenant, topology, [])
        assert not any(t in off for t in v2_tools)


# --------------------------------------------------------------------------
# build_agent / run_agent carry the switch
# --------------------------------------------------------------------------


def test_the_switch_yields_a_distinct_cached_agent_per_set():
    extras = reg.agent_tools_for(ALL_ADDONS)
    v1 = graph.build_agent(extras, BOOKING_TOPOLOGY_SOLE)
    v2 = graph.build_agent(extras, BOOKING_TOPOLOGY_SOLE, toolset_v2=True)
    assert v1 is not v2
    assert graph.build_agent(extras, BOOKING_TOPOLOGY_SOLE, toolset_v2=True) is v2
    assert graph.build_agent(extras, BOOKING_TOPOLOGY_SOLE) is v1
    assert _names(v2.tools).isdisjoint(_BUSY_AND_WRITE)
    assert "create_event" in _names(v1.tools)


async def test_run_agent_threads_the_switch_and_always_resets_it(monkeypatch):
    seen: list[tuple[bool, set[str]]] = []

    async def _capture(messages):
        agent = graph.build_agent(
            graph._extra_tools_ctx.get(),
            ai_tools._booking_topology_ctx.get(),
            toolset_v2=ai_tools._ai_toolset_v2_ctx.get(),
        )
        seen.append((ai_tools._ai_toolset_v2_ctx.get(), _names(agent.tools)))
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _capture)
    assert ai_tools._ai_toolset_v2_ctx.get() is False
    for flag in (True, False):
        await graph.run_agent(
            "oi",
            context={"conversation_id": str(uuid4())},
            tenant_config=_config(),
            extra_tools=reg.agent_tools_for(ALL_ADDONS),
            booking_topology=BOOKING_TOPOLOGY_SOLE,
            toolset_v2=flag,
        )
        assert ai_tools._ai_toolset_v2_ctx.get() is False
    (on_flag, on_names), (off_flag, off_names) = seen
    assert (on_flag, off_flag) == (True, False)
    assert on_names.isdisjoint(_BUSY_AND_WRITE)
    assert {"create_event", "check_availability"} <= off_names


async def test_run_agent_logs_the_effective_capabilities(monkeypatch):
    log = _Log()
    monkeypatch.setattr(graph, "logger", log)

    async def _noop(messages):
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _noop)
    await graph.run_agent(
        "oi",
        context={"conversation_id": str(uuid4())},
        tenant_config=_config(),
        extra_tools=[ai_tools.create_event, create_event_v2, get_availability],
        booking_topology=BOOKING_TOPOLOGY_SOLE,
        toolset_v2=True,
    )
    (fields,) = [f for e, f in log.events if e == "agent_capabilities_resolved"]
    assert fields["toolset_v2"] is True
    # The legacy create_event is gone; the blind one (same name) is what ran.
    assert fields["capabilities"] == sorted(_SCOPE_FREE | {"get_availability", "create_event"})


async def test_concurrent_turns_never_share_the_switch(monkeypatch):
    import asyncio

    started, release = asyncio.Event(), asyncio.Event()
    observed: dict[bool, set[str]] = {}

    async def _interleaved(messages):
        flag = ai_tools._ai_toolset_v2_ctx.get()
        names = _names(graph.build_agent((), BOOKING_TOPOLOGY_SOLE, toolset_v2=flag).tools)
        if flag:
            started.set()
            await release.wait()
        else:
            await started.wait()
            release.set()
        observed[flag] = names
        assert ai_tools._ai_toolset_v2_ctx.get() is flag
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _interleaved)
    await asyncio.gather(
        *(
            graph.run_agent(
                "oi",
                context={"conversation_id": str(uuid4())},
                tenant_config=_config(),
                booking_topology=BOOKING_TOPOLOGY_SOLE,
                toolset_v2=flag,
            )
            for flag in (True, False)
        )
    )
    assert "create_event" not in observed[True]
    assert "create_event" in observed[False]


async def test_a_calendar_outage_inside_get_availability_reaches_the_handover_sentinel(
    monkeypatch,
):
    """Today's behaviour for every calendar read, kept: outage -> the sentinel -> a person."""

    async def _active(_tenant_id):
        return []

    async def _down(self, *args, **kwargs):
        raise CalendarUnavailableError("down")

    monkeypatch.setattr(mp, "_active_professionals", _active)
    monkeypatch.setattr(CalendarService, "list_available_days", _down)

    async def _ask_the_tool(messages):
        return str(await get_availability.ainvoke({}))

    monkeypatch.setattr(graph, "invoke_agent", _ask_the_tool)
    reply = await graph.run_agent(
        "tem horário?",
        context={"conversation_id": str(uuid4())},
        tenant_config=_config(),
        booking_topology=BOOKING_TOPOLOGY_NONE,
        toolset_v2=True,
    )
    assert reply == graph.CALENDAR_UNAVAILABLE_SENTINEL
