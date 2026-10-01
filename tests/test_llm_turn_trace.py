"""The agent turn's black box: trace summary, diagnosis and the turn timeout."""

import asyncio
import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest  # noqa: E402
from langchain_core.messages import AIMessage, ToolMessage  # noqa: E402

from secretaria.ai import graph  # noqa: E402
from secretaria.ai.trace import diagnose, summarize_turn  # noqa: E402


def _ai(content="", tool_calls=None, finish="stop", out=10, reasoning=0):
    return AIMessage(
        content=content,
        tool_calls=tool_calls or [],
        response_metadata={"finish_reason": finish},
        usage_metadata={
            "input_tokens": 100,
            "output_tokens": out,
            "total_tokens": 100 + out,
            "output_token_details": {"reasoning": reasoning},
        },
    )


def test_ok_turn_with_a_tool_call_is_summarized_without_argument_values() -> None:
    msgs = [
        _ai(tool_calls=[{"name": "list_free_slots", "args": {"date": "2026-10-02"}, "id": "c1"}]),
        ToolMessage(content="[...]", name="list_free_slots", tool_call_id="c1"),
        _ai(content="Estes são os horários livres."),
    ]
    summary = summarize_turn(msgs)
    assert summary["model_calls"] == 2
    assert summary["tool_calls"] == [{"name": "list_free_slots", "arg_keys": ["date"]}]
    assert "2026-10-02" not in str(summary)
    assert summary["input_tokens"] == 200
    assert diagnose(summary) == "ok"


def test_gpt5_reasoning_eating_the_budget_is_named_truncated_no_text() -> None:
    summary = summarize_turn([_ai(content="", finish="length", out=2500, reasoning=2500)])
    assert summary["reasoning_tokens"] == 2500
    assert diagnose(summary) == "truncated_no_text"


def test_partial_text_cut_by_length_is_not_ok() -> None:
    summary = summarize_turn([_ai(content="Claro, posso aj", finish="length")])
    assert diagnose(summary) == "truncated_partial_text"


def test_empty_model_output_without_truncation_is_named() -> None:
    assert diagnose(summarize_turn([_ai(content="")])) == "empty_model_output"


def test_run_ending_on_a_tool_call_is_named() -> None:
    msgs = [_ai(tool_calls=[{"name": "show_main_menu", "args": {}, "id": "c1"}])]
    assert diagnose(summarize_turn(msgs)) == "ended_on_tool_call"


def test_tool_error_is_counted() -> None:
    msgs = [
        _ai(tool_calls=[{"name": "create_event", "args": {"summary": "x"}, "id": "c1"}]),
        ToolMessage(content="boom", name="create_event", tool_call_id="c1", status="error"),
        _ai(content="Não consegui."),
    ]
    assert summarize_turn(msgs)["tool_errors"] == 1


async def test_a_turn_that_outlives_its_budget_answers_with_the_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _no_history(_cid):  # noqa: ANN001
        return []

    async def _hang(_messages):  # noqa: ANN001
        await asyncio.sleep(5)
        return "tarde demais"

    monkeypatch.setattr(graph, "_load_history", _no_history)
    monkeypatch.setattr(graph, "invoke_agent", _hang)
    monkeypatch.setenv("LLM_TURN_TIMEOUT_SECONDS", "1")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    try:
        reply = await graph.run_agent(
            "oi", context={"conversation_id": "11111111-2222-3333-4444-555555555555"}
        )
    finally:
        monkeypatch.delenv("LLM_TURN_TIMEOUT_SECONDS")
        get_settings.cache_clear()
    assert reply == graph.FALLBACK_REPLY
