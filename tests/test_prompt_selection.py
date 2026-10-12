"""Which system prompt a turn gets (TASK-030 P5): ai/graph.py::_turn_system_prompt.

Switch off -> exactly ai/prompts.py::secretary_system_prompt, byte for byte. Switch on -> the
v2 prompt (ai/prompts_v2.py), naming only the tools of THAT turn's effective set
(graph.effective_tools, the assembly build_agent uses) - checked for every topology with
the real plugin tools and the real hand-backs. The choice is made on every model call,
never frozen into a cached agent. No model, no Google: the agent is a recording fake.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import re  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import date  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from langchain_core.messages import HumanMessage  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    prompts_v2,
    tools as ai_tools,
)
from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.ai.prompts_v2 import REQUIRED_TOOLS  # noqa: E402
from secretaria.plugins import registry as reg  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402
from secretaria.workers.shared.llm_context import _flow_handback_tools  # noqa: E402

TOPOLOGIES = [
    BOOKING_TOPOLOGY_UNKNOWN,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_MULTI,
]
TENANT_ON = SimpleNamespace(initial_flows={"ai_draft_v2": True})
TENANT_OFF = SimpleNamespace(initial_flows={})
V2_HEADING = "COMO VOCÊ TRABALHA COM O FLUXO"

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
NO_ADDONS = _summary()


def _config() -> TenantRuntimeConfig:
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
    )


def _universe() -> set[str]:
    """Every tool name a turn can be built with, v1 or v2 - read off the real objects."""
    names = {t.name for t in graph._BASE_TOOLS}
    names |= {t.name for t in reg.agent_tools_for(ALL_ADDONS)}
    for tenant in (TENANT_ON, TENANT_OFF):
        names |= {t.name for t in _flow_handback_tools(tenant, BOOKING_TOPOLOGY_SOLE, [])}
    return names


def _mentioned(prompt: str) -> set[str]:
    # \b around a snake_case name: "create_event" does not match inside
    # "create_event_for_professional" ("_" is a word character).
    return {name for name in _universe() if re.search(rf"\b{name}\b", prompt)}


@pytest.fixture(autouse=True)
def _pinned_day(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(prompts_v2, "_clinic_today", lambda _tz: date(2026, 10, 8))


@contextmanager
def _turn(*, v2: bool, topology: str, extras, config: TenantRuntimeConfig):
    """The context vars run_agent sets for one turn - what _prompt_with_today reads."""
    tokens = [
        (ai_tools._ai_toolset_v2_ctx, ai_tools._ai_toolset_v2_ctx.set(v2)),
        (ai_tools._booking_topology_ctx, ai_tools._booking_topology_ctx.set(topology)),
        (graph._extra_tools_ctx, graph._extra_tools_ctx.set(list(extras))),
        (ai_tools._tenant_config_ctx, ai_tools._tenant_config_ctx.set(config)),
    ]
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


def _rendered() -> str:
    system, *_history = graph._prompt_with_today({"messages": [HumanMessage(content="oi")]})
    return system.content


@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_a_switch_off_turn_gets_exactly_the_v1_prompt(topology):
    config = _config()
    extras = _flow_handback_tools(TENANT_OFF, topology, reg.agent_tools_for(ALL_ADDONS))
    with _turn(v2=False, topology=topology, extras=extras, config=config):
        assert _rendered() == secretary_system_prompt(config)
        assert graph._turn_system_prompt(config) == secretary_system_prompt(config)


@pytest.mark.parametrize("addons", [ALL_ADDONS, NO_ADDONS], ids=["all_addons", "no_addons"])
@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_a_v2_turn_prompt_names_only_its_own_tools(topology, addons):
    config = _config()
    extras = _flow_handback_tools(TENANT_ON, topology, reg.agent_tools_for(addons))
    turn_names = {t.name for t in graph.effective_tools(topology, extras, toolset_v2=True)}
    with _turn(v2=True, topology=topology, extras=extras, config=config):
        prompt = _rendered()
        assert graph._turn_tool_names() == turn_names
    assert V2_HEADING in prompt
    # Every real v2 turn carries the two tools the rules name unconditionally.
    assert set(REQUIRED_TOOLS) <= turn_names
    assert _mentioned(prompt) <= turn_names
    for gone in (*ai_tools.AI_TOOLSET_V2_WITHHELD, *ai_tools.AI_TOOLSET_V2_RETIRED):
        assert not re.search(rf"\b{gone}\b", prompt)
    assert "get_availability" in prompt


def test_a_v2_turn_without_get_availability_never_hears_of_it():
    config = _config()
    extras = [
        t
        for t in _flow_handback_tools(TENANT_ON, BOOKING_TOPOLOGY_SOLE, [])
        if t.name != "get_availability"
    ]
    with _turn(v2=True, topology=BOOKING_TOPOLOGY_SOLE, extras=extras, config=config):
        prompt = _rendered()
    assert "get_availability" not in prompt
    assert "Você NÃO consulta a agenda neste turno" in prompt


def test_the_prompt_is_chosen_per_call_not_per_cached_agent(monkeypatch):
    captured = {}

    def _recording_agent(model, tools, prompt):
        captured["prompt"] = prompt
        return SimpleNamespace(tools=list(tools))

    graph._AGENTS.clear()
    monkeypatch.setattr(graph, "create_react_agent", _recording_agent)
    try:
        graph.build_agent((), BOOKING_TOPOLOGY_SOLE)
    finally:
        graph._AGENTS.clear()
    render = captured["prompt"]
    config = _config()
    state = {"messages": [HumanMessage(content="oi")]}
    with _turn(v2=False, topology=BOOKING_TOPOLOGY_SOLE, extras=(), config=config):
        assert render(state)[0].content == secretary_system_prompt(config)
    with _turn(
        v2=True,
        topology=BOOKING_TOPOLOGY_SOLE,
        extras=_flow_handback_tools(TENANT_ON, BOOKING_TOPOLOGY_SOLE, []),
        config=config,
    ):
        assert V2_HEADING in render(state)[0].content


async def test_run_agent_hands_the_model_the_prompt_of_its_turn(monkeypatch):
    seen: list[str] = []

    async def _capture(messages):
        seen.append(graph._prompt_with_today({"messages": messages})[0].content)
        return "ok"

    async def _empty_history(_conversation_id):
        return []

    monkeypatch.setattr(graph, "invoke_agent", _capture)
    monkeypatch.setattr(graph, "_load_history", _empty_history)
    config = _config()
    for flag in (True, False):
        tenant = TENANT_ON if flag else TENANT_OFF
        await graph.run_agent(
            "oi",
            context={"conversation_id": str(uuid4())},
            tenant_config=config,
            extra_tools=_flow_handback_tools(tenant, BOOKING_TOPOLOGY_SOLE, []),
            booking_topology=BOOKING_TOPOLOGY_SOLE,
            toolset_v2=flag,
        )
    v2_prompt, v1_prompt = seen
    assert V2_HEADING in v2_prompt
    assert v1_prompt == secretary_system_prompt(config)
    assert ai_tools._ai_toolset_v2_ctx.get() is False


def test_the_trace_log_records_the_prompt_the_turn_used(monkeypatch):
    events: list[tuple[str, dict]] = []

    class _Log:
        def __getattr__(self, level):
            def _log(event, **fields):
                events.append((event, fields))

            return _log

    monkeypatch.setattr(graph, "logger", _Log())
    config = _config()
    extras = _flow_handback_tools(TENANT_ON, BOOKING_TOPOLOGY_SOLE, [])
    with _turn(v2=True, topology=BOOKING_TOPOLOGY_SOLE, extras=extras, config=config):
        graph._log_trace_content([], config, uuid4())
        expected = graph._turn_system_prompt(config)
    (fields,) = [f for e, f in events if e == "llm_trace_prompt"]
    assert fields["system_prompt"] == expected
    assert V2_HEADING in expected
    assert fields["prompt_chars"] == len(expected)
