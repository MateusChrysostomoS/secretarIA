"""LangGraph agent for SecretarIA (Phase 5 / Fase B).

create_react_agent gives us the exact LLM -> tool -> LLM loop validated in
Fase A, now driven by the arq worker. Conversation history is rebuilt from
the messages table on every call so the worker stays stateless.
"""

import re
from typing import Any
from uuid import UUID

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from sqlalchemy import select

from secretaria.ai.prompts import secretary_system_prompt
from secretaria.ai.tools import (
    cancel_event,
    check_availability,
    create_event,
    list_free_slots,
)
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Message, MessageSender

logger = get_logger(__name__)

HISTORY_LIMIT = 30
FALLBACK_REPLY = (
    "Desculpe, tive um problema técnico agora. Pode tentar de novo em alguns "
    "instantes?"
)

# Patterns the LLM should NEVER emit to a patient. If any match, the reply is
# considered a model hallucination of chat-platform meta-text and we replace
# it with FALLBACK_REPLY before WhatsApp sends it.
_META_TEXT_PATTERNS = [
    re.compile(r"generated\s+by\s+the\s+assistant", re.IGNORECASE),
    re.compile(r"conversation\s+state.{0,40}ignore", re.IGNORECASE),
    re.compile(r"\(\s*this\s+(message|content|text)\s+.{0,80}ignore", re.IGNORECASE),
    re.compile(r"system\s+(message|note|prompt).{0,40}ignore", re.IGNORECASE),
]


def _looks_like_meta_output(text: str) -> bool:
    return any(p.search(text) for p in _META_TEXT_PATTERNS)


_AGENT: Any | None = None


def _prompt_with_today(state: dict) -> list[BaseMessage]:
    """Prepend a freshly-rendered system prompt so today's date is current.

    Called by LangGraph on every agent invocation, so the prompt never goes
    stale even if the worker has been running for days.
    """
    tz = get_settings().CLINIC_TIMEZONE
    return [SystemMessage(content=secretary_system_prompt(tz)), *state["messages"]]


def build_agent() -> Any:
    """Compile the ReAct agent. Idempotent: cached process-wide."""
    global _AGENT
    if _AGENT is not None:
        return _AGENT
    s = get_settings()
    if not s.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in environment.")
    model = ChatOpenAI(
        model=s.OPENAI_MODEL,
        api_key=s.OPENAI_API_KEY,
        # gpt-5 / o-series reasoning models require max_completion_tokens
        # (not max_tokens). langchain-openai 1.x accepts it as a direct
        # kwarg even though it isn't a typed field.
        max_completion_tokens=s.OPENAI_MAX_TOKENS,
    )
    _AGENT = create_react_agent(
        model,
        tools=[check_availability, list_free_slots, create_event, cancel_event],
        prompt=_prompt_with_today,
    )
    return _AGENT


async def invoke_agent(messages: list[BaseMessage]) -> str:
    """Run the agent on a message list, return the last assistant reply.

    Shared by the dev terminal (scripts/test_agent.py) and run_agent below.
    """
    result = await build_agent().ainvoke({"messages": messages})
    last = result["messages"][-1]
    return (getattr(last, "content", "") or "").strip()


async def _load_history(conversation_id: UUID) -> list[BaseMessage]:
    """Reconstruct LangChain message history from the DB.

    Pulls the most recent HISTORY_LIMIT messages, returns them in chronological
    order. The inbound message that just triggered this call MUST already be
    persisted (workers/tasks.py guarantees this before calling run_agent).
    """
    async with async_session_factory() as session:
        rows = await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(HISTORY_LIMIT)
        )
        recent = list(rows)
    recent.reverse()

    out: list[BaseMessage] = []
    for m in recent:
        content = m.body or ""
        if not content:
            continue
        if m.sender == MessageSender.PATIENT:
            out.append(HumanMessage(content=content))
        else:
            # BOT and HUMAN secretary turns both look like assistant turns to
            # the LLM. A human reply acts as the model's prior message.
            out.append(AIMessage(content=content))
    return out


async def run_agent(message: str, context: dict) -> str:
    """arq-side entry point: build history + run agent + return reply text.

    `message` is the latest inbound body (kept for signature compatibility
    with the previous stub); the actual content is read fresh from the DB
    via conversation_id so the LLM sees the full thread.
    """
    conversation_id = UUID(context["conversation_id"])
    try:
        history = await _load_history(conversation_id)
        if not history:
            logger.warning(
                "ai_run_agent_no_history", conversation_id=str(conversation_id)
            )
            # Defensive: if the inbound wasn't persisted yet for some reason,
            # at least feed the message we received.
            history = [HumanMessage(content=message)]
        reply = await invoke_agent(history)
    except Exception as exc:
        logger.error(
            "ai_run_agent_failed",
            error=str(exc),
            conversation_id=str(conversation_id),
        )
        return FALLBACK_REPLY

    if not reply:
        logger.warning(
            "ai_run_agent_empty_reply", conversation_id=str(conversation_id)
        )
        return FALLBACK_REPLY
    if _looks_like_meta_output(reply):
        # Model hallucinated chat-platform narration ("this message was
        # generated by the assistant... ignore"). Block it from reaching the
        # patient and capture the rejected body for prompt-tuning later.
        logger.error(
            "ai_run_agent_meta_output_rejected",
            conversation_id=str(conversation_id),
            rejected_body=reply[:500],
        )
        return FALLBACK_REPLY
    return reply
