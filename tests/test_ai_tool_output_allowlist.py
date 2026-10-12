"""The v2 output allowlist (TASK-030 P4): every v2 tool answers only the keys it declared.

`ai/tool_output.py::V2_TOOL_OUTPUTS` is the declaration; `wrap_tools_with_output_allowlist`
enforces it on every tool of a v2 agent (ai/graph.py::build_agent), INSIDE the
pseudonymization guard. Three layers here: the filter itself (pure), the static check that
every tool a v2 turn can be built with has a declaration (every topology, with and without
the addons), and the agent wiring. The end-to-end proof against decoy data is
tests/test_ai_v2_blindness.py.
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
from langchain_core.tools import tool  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    tool_output,
)
from secretaria.ai.tool_output import (  # noqa: E402
    UNDECLARED_ERROR,
    UNUSABLE_ERROR,
    V2_TOOL_OUTPUTS,
    allowlisted,
    undeclared_keys,
    wrap_tools_with_output_allowlist,
)
from secretaria.ai.tools import ShowMainMenuRequested  # noqa: E402
from secretaria.plugins import registry as reg  # noqa: E402
from secretaria.services.booking_scope import (  # noqa: E402
    BOOKING_TOPOLOGY_MULTI,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_UNKNOWN,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers.shared.llm_context import _flow_handback_tools  # noqa: E402

CANARY = "CANARY-ab12"
TOPOLOGIES = [
    BOOKING_TOPOLOGY_UNKNOWN,
    BOOKING_TOPOLOGY_NONE,
    BOOKING_TOPOLOGY_SOLE,
    BOOKING_TOPOLOGY_MULTI,
]
TENANT_ON = SimpleNamespace(initial_flows={"ai_draft_v2": True})
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


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


@pytest.fixture
def log(monkeypatch):
    recorder = _Log()
    monkeypatch.setattr(tool_output, "logger", recorder)
    return recorder


# --------------------------------------------------------------------------
# The filter (pure)
# --------------------------------------------------------------------------


def test_a_clean_answer_passes_untouched(log):
    answer = {
        "windows": [{"day": "2026-10-06", "start": "08:00", "end": "09:00"}],
        "timezone": "America/Sao_Paulo",
        "slot_minutes": 30,
        "day_from": "2026-10-06",
        "day_to": "2026-10-06",
        "clamped": ["max_days"],
        "note": "x",
    }
    assert allowlisted("get_availability", answer) == answer
    assert undeclared_keys("get_availability", answer) == []
    assert log.events == []


def test_an_undeclared_key_is_dropped_and_logged_by_name_only(log):
    answer = {
        "windows": [],
        "summary": f"Consulta - {CANARY} Silva",
        "busy": [{"id": f"evt-{CANARY}", "summary": CANARY}],
    }
    out = allowlisted("get_availability", answer)
    assert out == {"windows": []}
    assert undeclared_keys("get_availability", answer) == ["summary", "busy"]
    ((event, fields),) = log.events
    assert (event, fields["tool"], fields["keys"]) == (
        "agent_tool_output_dropped",
        "get_availability",
        ["busy", "summary"],
    )
    assert CANARY not in repr(log.events)


def test_a_record_keeps_only_its_declared_fields(log):
    answer = {
        "windows": [
            {"day": "2026-10-06", "start": "08:00", "end": "09:00", "summary": CANARY},
            "not-a-record",
            {"day": "2026-10-07", "start": "08:00", "end": "09:00", "attendees": [CANARY]},
        ]
    }
    out = allowlisted("get_availability", answer)
    assert out == {
        "windows": [
            {"day": "2026-10-06", "start": "08:00", "end": "09:00"},
            {"day": "2026-10-07", "start": "08:00", "end": "09:00"},
        ]
    }
    assert CANARY not in repr(out)
    (fields,) = [f for e, f in log.events if e == "agent_tool_output_dropped"]
    assert fields["keys"] == ["windows[]", "windows[].attendees", "windows[].summary"]


def test_a_nested_structure_under_a_plain_key_is_dropped(log):
    out = allowlisted("get_availability", {"windows": [], "note": {"who": CANARY}})
    assert out == {"windows": []}
    assert CANARY not in repr(out) + repr(log.events)


def test_a_key_that_is_not_an_identifier_is_logged_as_other(log):
    out = allowlisted("list_patient_appointments", {f"Maria {CANARY}": 1, "count": 0})
    assert out == {"count": 0}
    (fields,) = [f for e, f in log.events if e == "agent_tool_output_dropped"]
    assert fields["keys"] == ["<other>"]
    assert CANARY not in repr(log.events)


def test_an_undeclared_tool_answers_an_error_instead_of_its_data(log):
    assert allowlisted("tool_from_tomorrow", {"anything": CANARY}) == {"error": UNDECLARED_ERROR}
    assert undeclared_keys("tool_from_tomorrow", {}) == ["<undeclared tool>"]
    assert log.events == [("agent_tool_output_undeclared", {"tool": "tool_from_tomorrow"})]


def test_text_only_where_declared(log):
    assert allowlisted("iniciar_pre_consulta", "Pré-consulta liberada.") == "Pré-consulta liberada."
    assert allowlisted("create_event", f"evento {CANARY}") == {"error": UNUSABLE_ERROR}
    assert undeclared_keys("create_event", "x") == ["<text>"]


def test_nothing_left_is_an_error_not_an_empty_answer(log):
    assert allowlisted("create_event", {"id": f"evt-{CANARY}", "htmlLink": CANARY}) == {
        "error": UNUSABLE_ERROR
    }


def test_the_staging_and_handback_tools_may_only_ever_say_error():
    for name in (
        "create_event",
        "cancel_event",
        "set_booking_draft",
        "manage_existing_appointment",
        "request_human_handoff",
        "show_main_menu",
    ):
        assert V2_TOOL_OUTPUTS[name].keys == frozenset({"error"})
        assert V2_TOOL_OUTPUTS[name].text is False


@tool("list_units")
async def _leaky_list_units() -> dict:
    """Stand-in with a declared name that leaks an undeclared key."""
    return {"units": [{"name": "Centro", "address": "Rua 1", "owner": CANARY}], "summary": CANARY}


@tool("show_main_menu")
async def _menu() -> str:
    """Hands back."""
    raise ShowMainMenuRequested()


@tool("list_professionals")
def _sync_tool() -> dict:
    """A sync tool would bypass the filter."""
    return {"professionals": []}


@tool
async def _undeclared() -> dict:
    """No declaration."""
    return {"anything": CANARY}


async def test_the_wrapper_filters_keeps_identity_and_lets_handbacks_propagate(log):
    _leaky_list_units.metadata = {"cache_variant": "x"}
    units, menu, sync, undeclared = wrap_tools_with_output_allowlist(
        [_leaky_list_units, _menu, _sync_tool, _undeclared]
    )
    assert (units.name, units.args, units.metadata) == (
        "list_units",
        _leaky_list_units.args,
        {"cache_variant": "x"},
    )
    assert await units.ainvoke({}) == {"units": [{"name": "Centro", "address": "Rua 1"}]}
    with pytest.raises(ShowMainMenuRequested):
        await menu.ainvoke({})
    assert await sync.ainvoke({}) == {"error": UNDECLARED_ERROR}
    assert await undeclared.ainvoke({}) == {"error": UNDECLARED_ERROR}


# --------------------------------------------------------------------------
# Static: every tool a v2 turn can be built with has a declaration
# --------------------------------------------------------------------------


def _v2_turn_tools(topology, addons):
    extras = _flow_handback_tools(TENANT_ON, topology, reg.agent_tools_for(addons))
    return graph.effective_tools(topology, extras, toolset_v2=True)


@pytest.mark.parametrize("addons", [ALL_ADDONS, NO_ADDONS], ids=["all_addons", "no_addons"])
@pytest.mark.parametrize("topology", TOPOLOGIES)
def test_every_tool_of_every_v2_turn_declares_its_output(topology, addons):
    tools = _v2_turn_tools(topology, addons)
    undeclared = {t.name for t in tools} - set(V2_TOOL_OUTPUTS)
    assert not undeclared, f"declare the output of {sorted(undeclared)} in ai/tool_output.py"
    # Async only: a sync tool would run without the filter, so the wrapper refuses it.
    assert all(getattr(t, "coroutine", None) is not None for t in tools)


# --------------------------------------------------------------------------
# The agent wiring
# --------------------------------------------------------------------------


@pytest.fixture
def _recording_build(monkeypatch):
    graph._AGENTS.clear()
    monkeypatch.setattr(
        graph,
        "create_react_agent",
        lambda model, tools, prompt: SimpleNamespace(tools=list(tools)),
    )
    yield
    graph._AGENTS.clear()


async def test_a_v2_agent_wraps_every_tool_inside_the_pseudonymization_guard(
    _recording_build, monkeypatch
):
    handed_to_the_guard: list = []
    real_guard = graph.wrap_tools_with_pseudonymizer

    def _recording_guard(tools):
        handed_to_the_guard.extend(tools)
        return real_guard(tools)

    monkeypatch.setattr(graph, "wrap_tools_with_pseudonymizer", _recording_guard)
    agent = graph.build_agent([_leaky_list_units], BOOKING_TOPOLOGY_SOLE, toolset_v2=True)

    # The guard receives tools that ALREADY pass the allowlist (the allowlist is inside) ...
    (handed,) = [t for t in handed_to_the_guard if t.name == "list_units"]
    assert await handed.ainvoke({}) == {"units": [{"name": "Centro", "address": "Rua 1"}]}
    # ... and the agent gets the guard's wrapper around it (the guard is still on).
    (built,) = [t for t in agent.tools if t.name == "list_units"]
    assert built.coroutine is not handed.coroutine
    assert await built.ainvoke({}) == {"units": [{"name": "Centro", "address": "Rua 1"}]}


async def test_a_v1_agent_is_built_exactly_as_before(_recording_build):
    agent = graph.build_agent([_leaky_list_units], BOOKING_TOPOLOGY_SOLE)
    (built,) = [t for t in agent.tools if t.name == "list_units"]
    # No allowlist on the switch-off path: the tool answers exactly what it returns.
    assert (await built.ainvoke({}))["summary"] == CANARY


def test_a_v2_agent_never_shares_a_cache_entry_with_a_v1_agent(_recording_build):
    """A multi clinic with no extras has the same three scope-free NAMES on v1 and v2."""
    v1 = graph.build_agent((), BOOKING_TOPOLOGY_MULTI)
    v2 = graph.build_agent((), BOOKING_TOPOLOGY_MULTI, toolset_v2=True)
    assert {t.name for t in v1.tools} == {t.name for t in v2.tools}
    assert v1 is not v2
    assert graph.build_agent((), BOOKING_TOPOLOGY_MULTI, toolset_v2=True) is v2
    assert graph.build_agent((), BOOKING_TOPOLOGY_MULTI) is v1
