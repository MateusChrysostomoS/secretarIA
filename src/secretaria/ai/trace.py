"""Observability for one agent turn: WHAT the model did, never what anyone said.

The ReAct loop used to be a black box: a turn went in, a string (or the generic
FALLBACK_REPLY) came out, and the only evidence of what happened in between was
whatever a tool happened to log. This reads the message list LangGraph returns
and reduces it to a flat, countable summary - how many model calls, which tools
with which outcome, token spend (reasoning tokens split out, because on gpt-5
they are what silently eats the output budget), and why the model stopped.

Privacy: the summary carries tool NAMES, argument KEY names, lengths and counts.
Never argument values, tool results or reply text - those quote the patient. The
opt-in content capture (`LLM_TRACE_CONTENT`) is a separate, explicit event that
only ever receives the already-pseudonymized history.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

# `finish_reason` values that mean the model did NOT finish a thought. "length"
# is the dangerous one: the budget (reasoning + visible text) ran out, so the
# visible text is empty or cut mid-sentence.
TRUNCATED_FINISH_REASONS = frozenset({"length", "content_filter"})


def _reasoning_tokens(message: AIMessage) -> int:
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("output_token_details") or {}
    return int(details.get("reasoning") or 0)


def summarize_turn(new_messages: Sequence[BaseMessage]) -> dict[str, Any]:
    """Reduce the messages a turn ADDED (not the history it received) to a summary.

    `new_messages` is everything after the input history: alternating AIMessage
    (a model call, possibly with tool_calls) and ToolMessage (a tool result).
    """
    model_calls = 0
    input_tokens = 0
    output_tokens = 0
    reasoning_tokens = 0
    finish_reasons: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    tool_outcomes: list[dict[str, Any]] = []
    empty_model_calls = 0

    for message in new_messages:
        if isinstance(message, AIMessage):
            model_calls += 1
            usage = getattr(message, "usage_metadata", None) or {}
            input_tokens += int(usage.get("input_tokens") or 0)
            output_tokens += int(usage.get("output_tokens") or 0)
            reasoning_tokens += _reasoning_tokens(message)
            reason = (getattr(message, "response_metadata", None) or {}).get("finish_reason")
            if reason:
                finish_reasons.append(str(reason))
            has_text = bool(str(getattr(message, "content", "") or "").strip())
            has_tools = bool(getattr(message, "tool_calls", None))
            if not has_text and not has_tools:
                empty_model_calls += 1
            for call in getattr(message, "tool_calls", None) or []:
                args = call.get("args") or {}
                tool_calls.append(
                    {
                        "name": call.get("name"),
                        # Keys only: the values are what the patient said.
                        "arg_keys": sorted(args) if isinstance(args, dict) else [],
                    }
                )
        elif isinstance(message, ToolMessage):
            tool_outcomes.append(
                {
                    "name": getattr(message, "name", None),
                    "status": getattr(message, "status", None) or "success",
                    "result_len": len(str(getattr(message, "content", "") or "")),
                }
            )

    last = new_messages[-1] if new_messages else None
    final_text = ""
    if isinstance(last, AIMessage):
        final_text = str(getattr(last, "content", "") or "").strip()
    truncated = any(r in TRUNCATED_FINISH_REASONS for r in finish_reasons)

    return {
        "model_calls": model_calls,
        "tool_call_count": len(tool_calls),
        "tool_calls": tool_calls,
        # Names only, in the order the model asked for them: the flat list a log search can
        # filter on ("every turn that called set_booking_draft") without unpacking tool_calls.
        "tools_called": [str(call["name"]) for call in tool_calls if call.get("name")],
        "tool_outcomes": tool_outcomes,
        "tool_errors": sum(1 for o in tool_outcomes if o["status"] == "error"),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "reasoning_tokens": reasoning_tokens,
        "finish_reasons": finish_reasons,
        "truncated": truncated,
        "empty_model_calls": empty_model_calls,
        "final_reply_len": len(final_text),
        "ended_on_tool_call": bool(last is not None and getattr(last, "tool_calls", None)),
    }


def diagnose(summary: dict[str, Any]) -> str:
    """One stable word for WHY a turn produced no usable text, or "ok".

    Chosen to be grouped on in a log search ("how many truncated turns today?"),
    in order of how much each one explains. Pure over `summarize_turn` output.
    """
    if summary["final_reply_len"] > 0 and not summary["truncated"]:
        return "ok"
    if summary["truncated"] and summary["final_reply_len"] == 0:
        # gpt-5 reasoning ate the whole max_completion_tokens budget.
        return "truncated_no_text"
    if summary["truncated"]:
        return "truncated_partial_text"
    if summary["ended_on_tool_call"]:
        return "ended_on_tool_call"
    if summary["empty_model_calls"] > 0:
        return "empty_model_output"
    return "no_text"
