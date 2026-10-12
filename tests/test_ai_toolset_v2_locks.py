"""The v2 toolset's second lock: each withheld or legacy tool refuses by itself (TASK-030 P4).

On a turn that runs on the AI toolset v2 the agent is built without the tools that read a busy
interval or write to the agenda: `ai/tools.py::AI_TOOLSET_V2_WITHHELD` (no v2 variant at all)
and the LEGACY implementations of `AI_TOOLSET_V2_STAGING` (`create_event`/`cancel_event`: on
v2 those names are the blind tools of ai/staging_tools.py, which only stage the patient's
confirmation card) - lock one, asserted in tests/test_ai_toolset_v2.py. This file pins lock
two, the repo's own pattern (`_blocked_tenant_level`): one of them that arrives anyway - a
stale cached graph, a hand-rolled call, a future caller - returns an error BEFORE any Google
call or DB write.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import re  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.ai import (  # noqa: E402
    graph,
    tools as ai_tools,
)
from secretaria.plugins import (  # noqa: E402
    multi_professional as mp,
    multi_unit as mu,
    registry as reg,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402

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
_HANDBACKS = {"manage_existing_appointment", "set_booking_draft", "request_human_handoff"}


class _Log:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def __getattr__(self, level):
        def _log(event, **fields):
            self.events.append((event, fields))

        return _log


class _ExplodingCalendar:
    """Any use is a bug: a refused tool must never reach the calendar."""

    def __getattr__(self, name):
        raise AssertionError(f"calendar must not be touched (called {name})")


_WINDOW = {"start": "2026-10-08T08:00:00", "end": "2026-10-08T08:30:00"}
_LOCKED_CALLS = [
    (ai_tools.check_availability, dict(_WINDOW)),
    (ai_tools.list_free_slots, {"day": "2026-10-08"}),
    (ai_tools.create_event, {**_WINDOW, "summary": "Consulta"}),
    (ai_tools.cancel_event, {"event_id": "evt-1"}),
    (mp.list_free_slots_for_professional, {"professional_name": "Dra. Ana", "day": "2026-10-08"}),
    (
        mp.create_event_for_professional,
        {"professional_name": "Dra. Ana", **_WINDOW, "summary": "Consulta"},
    ),
    (mu.create_event_at_unit, {"unit_name": "Centro", **_WINDOW, "summary": "Consulta"}),
]


@pytest.fixture
def _v2_turn():
    tokens = [
        (ai_tools._ai_toolset_v2_ctx, ai_tools._ai_toolset_v2_ctx.set(True)),
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(uuid4())),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(_ExplodingCalendar())),
    ]
    yield
    for var, token in reversed(tokens):
        var.reset(token)


def test_the_tested_tools_are_exactly_the_locked_names():
    locked = {*ai_tools.AI_TOOLSET_V2_WITHHELD, *ai_tools.AI_TOOLSET_V2_STAGING}
    assert {t.name for t, _ in _LOCKED_CALLS} == locked == _BUSY_AND_WRITE
    assert set(ai_tools.AI_TOOLSET_V2_STAGING) == {"create_event", "cancel_event"}
    assert set(ai_tools.AI_TOOLSET_V2_WITHHELD).isdisjoint(ai_tools.AI_TOOLSET_V2_STAGING)
    assert len(ai_tools.AI_TOOLSET_V2_WITHHELD) == 5


def test_the_blind_variant_marker_is_stable():
    assert ai_tools.BLIND_STAGING_VARIANT == "blind_v2"
    assert ai_tools.TOOL_BLOCK_TOOLSET_V2 == "toolset_v2"


@pytest.mark.parametrize(("tool", "args"), _LOCKED_CALLS, ids=lambda v: getattr(v, "name", ""))
async def test_a_withheld_or_legacy_tool_refuses_on_a_v2_turn_without_touching_the_calendar(
    _v2_turn, monkeypatch, tool, args
):
    log = _Log()
    monkeypatch.setattr(ai_tools, "logger", log)
    result = await tool.ainvoke(args)
    assert set(result) == {"error"}
    assert "get_availability" in result["error"]
    (fields,) = [f for e, f in log.events if e == "agent_tool_blocked"]
    assert (fields["tool"], fields["reason"]) == (tool.name, "toolset_v2")
    # Names and an enum only: never the arguments, which carry the patient's own words.
    assert "Consulta" not in repr(log.events)


@pytest.mark.parametrize(("tool", "args"), _LOCKED_CALLS[:4], ids=lambda v: getattr(v, "name", ""))
async def test_switch_off_leaves_every_base_tool_as_it_was(monkeypatch, tool, args):
    """The lock is armed only by the v2 context: with it off the tool reaches its own logic
    (here, the calendar - which this test makes refuse loudly). `cancel_event` must also
    pass its owner check (Task 1), so every id counts as the patient's own here."""

    async def _all_theirs(event_ids):
        return set(event_ids)

    monkeypatch.setattr(ai_tools, "_own_google_event_ids", _all_theirs)
    assert ai_tools._ai_toolset_v2_ctx.get() is False
    assert ai_tools._blocked_by_toolset_v2(tool.name) is None
    token = ai_tools._calendar_ctx.set(_ExplodingCalendar())
    try:
        with pytest.raises(AssertionError, match="calendar must not be touched"):
            await tool.ainvoke(args)
    finally:
        ai_tools._calendar_ctx.reset(token)


_CALENDAR_LIKE = re.compile(
    r"(create|cancel|update|delete|reschedule)_event|free_slots|availability"
)


def test_every_calendar_touching_tool_is_classified():
    """A tool added tomorrow that reads or writes the agenda must be put on the withheld or
    the staging list before it can reach a v2 AI - this fails until someone classifies it."""
    everything = EntitlementSummary(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons={"multi_professional": True, "multi_unit": True},
        limits={},
    )
    universe = {
        *(t.name for t in graph._BASE_TOOLS),
        *(t.name for t in reg.agent_tools_for(everything)),
        *_HANDBACKS,
        "start_guided_booking",
    }
    assert {name for name in universe if _CALENDAR_LIKE.search(name)} == _BUSY_AND_WRITE
