"""Tests for the capability-keyed agent cache in ai/graph.py.

Replaces the old process-wide `_AGENT` singleton with `_AGENTS`, a dict keyed
by the frozenset of tool names the agent was built with, so tenants entitled
to different plugin tool sets don't share (or collide on) a compiled graph.

`create_react_agent` is monkeypatched with a tiny fake so these tests never
touch OpenAI: only the caching/keying behaviour is under test.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest  # noqa: E402
from langchain_core.messages import AIMessage  # noqa: E402
from langchain_core.tools import tool  # noqa: E402

from secretaria.ai import graph  # noqa: E402


@tool
def _fake_tool_a(x: str) -> str:
    """A fake plugin tool A."""
    return x


@tool
def _fake_tool_b(x: str) -> str:
    """A fake plugin tool B."""
    return x


class _FakeCompiledAgent:
    """Stand-in for the LangGraph compiled graph: records its tools, no LLM call."""

    def __init__(self, tools):
        self.tools = tools

    async def ainvoke(self, state):
        return {"messages": [AIMessage(content="ok")]}


@pytest.fixture(autouse=True)
def _clean_agent_cache_and_fakes(monkeypatch: pytest.MonkeyPatch):
    """Isolate _AGENTS across tests and stub out the real LangGraph compile."""
    graph._AGENTS.clear()

    build_calls: list[list] = []

    def _fake_create_react_agent(model, tools, prompt):
        build_calls.append(list(tools))
        return _FakeCompiledAgent(tools)

    monkeypatch.setattr(graph, "create_react_agent", _fake_create_react_agent)
    monkeypatch.setattr(graph, "_load_history", _empty_history)
    yield build_calls
    graph._AGENTS.clear()


async def _empty_history(_conversation_id):
    return []


# --------------------------------------------------------------------------
# build_agent
# --------------------------------------------------------------------------


def test_build_agent_base_tools_unchanged():
    agent = graph.build_agent()
    names = {t.name for t in agent.tools}
    assert names == {
        "check_availability",
        "list_free_slots",
        "create_event",
        "cancel_event",
        "iniciar_pre_consulta",
        "list_patient_appointments",
        "show_main_menu",
    }


def test_build_agent_different_extra_tools_yield_distinct_cached_agents():
    agent_a = graph.build_agent((_fake_tool_a,))
    agent_b = graph.build_agent((_fake_tool_b,))
    assert agent_a is not agent_b
    assert len(graph._AGENTS) == 2


def test_build_agent_same_extra_tools_reuse_cached_agent(_clean_agent_cache_and_fakes):
    build_calls = _clean_agent_cache_and_fakes
    first = graph.build_agent((_fake_tool_a,))
    second = graph.build_agent((_fake_tool_a,))
    assert first is second
    assert len(build_calls) == 1  # only compiled once


def test_build_agent_no_extra_tools_reuses_base_cache_entry():
    first = graph.build_agent()
    second = graph.build_agent(())
    assert first is second
    assert len(graph._AGENTS) == 1


# --------------------------------------------------------------------------
# run_agent threads extra_tools through to the cached agent
# --------------------------------------------------------------------------


async def test_run_agent_caches_distinct_agents_per_extra_tools_set(
    _clean_agent_cache_and_fakes,
):
    build_calls = _clean_agent_cache_and_fakes
    ctx = {"conversation_id": "11111111-2222-3333-4444-555555555555"}

    await graph.run_agent("oi", context=ctx, extra_tools=(_fake_tool_a,))
    await graph.run_agent("oi", context=ctx, extra_tools=(_fake_tool_b,))
    assert len(graph._AGENTS) == 2
    assert len(build_calls) == 2

    # Repeating the first tool set must reuse the cached agent, not rebuild.
    await graph.run_agent("oi", context=ctx, extra_tools=(_fake_tool_a,))
    assert len(graph._AGENTS) == 2
    assert len(build_calls) == 2
