"""LangGraph agent for SecretarIA (Phase 5 / Fase B).

create_react_agent gives us the exact LLM -> tool -> LLM loop validated in
Fase A, now driven by the arq worker. Conversation history is rebuilt from
the messages table on every call so the worker stays stateless.

Multi-tenant: the CalendarService and system prompt are scoped per invocation
via ContextVars so the process-wide cached agent can serve multiple tenants
concurrently without interference.
"""

import asyncio
import re
import ssl
from collections.abc import Sequence
from contextvars import ContextVar
from typing import Any
from uuid import UUID

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.prebuilt import create_react_agent
from openai import APIConnectionError, APITimeoutError
from sqlalchemy import select

from secretaria.ai.prompts import secretary_system_prompt
from secretaria.ai.tools import (
    _calendar_ctx,
    _conversation_id_ctx,
    _redis_ctx,
    _tenant_config_ctx,
    _tenant_id_ctx,
    cancel_event,
    check_availability,
    create_event,
    iniciar_pre_consulta,
    list_free_slots,
)
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Message, MessageSender
from secretaria.services.calendar import CalendarService, CalendarUnavailableError
from secretaria.services.tenant_config import TenantRuntimeConfig

logger = get_logger(__name__)

HISTORY_LIMIT = 30
FALLBACK_REPLY = (
    "Desculpe, tive uma instabilidade rápida aqui 🙏. Pode me repetir sua "
    "última mensagem? Já te respondo."
)
# Returned by run_agent when a tool failed because Google Calendar is
# unreachable / the credentials were rejected. The worker turns this into a
# patient-facing message and hands the conversation to a human secretary.
CALENDAR_UNAVAILABLE_SENTINEL = "__CALENDAR_UNAVAILABLE__"

# Per-async-task TenantRuntimeConfig, used by _prompt_with_today. Defined in
# ai/tools.py (imported above as `_tenant_config_ctx`) rather than here, so a
# plugin tool module (e.g. plugins/multi_professional.py) can read it to build
# a CalendarService without importing graph.py.

# Per-async-task extra (plugin-contributed) tools for the CURRENT turn. Set by
# run_agent, read by invoke_agent when resolving/building the cached agent for
# this call's capability set. Same ContextVar pattern as the vars above: each
# concurrent worker task gets its own slot, and it is reset in run_agent's
# `finally` so it never leaks across turns/tenants.
_extra_tools_ctx: ContextVar[Sequence] = ContextVar("_extra_tools", default=())

# Errors that a single retry usually recovers from: TLS connection torn down
# mid-read, OpenAI gateway returning a brief connection refusal, a timeout
# we want to give one more shot. The openai SDK already retries internally
# (see build_agent), but a top-level retry salvages the turn when even the
# SDK retries get exhausted by a sustained blip.
_TRANSIENT_NETWORK_EXCEPTIONS: tuple[type[BaseException], ...] = (
    APIConnectionError,
    APITimeoutError,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    ssl.SSLEOFError,
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


_BASE_TOOLS = (
    check_availability,
    list_free_slots,
    create_event,
    cancel_event,
    iniciar_pre_consulta,
)

# Compiled agent cache, keyed by the frozenset of tool NAMES the agent was
# built with (base tools + whatever plugins a tenant is entitled to). Replaces
# the old process-wide `_AGENT` singleton: different tenants can be entitled to
# different plugin tool sets concurrently, so a single cached graph is no
# longer enough. Tool set combinations are small and bounded (one entry per
# distinct combination actually seen in production), so this dict stays small.
_AGENTS: dict[frozenset[str], Any] = {}


def _prompt_with_today(state: dict) -> list[BaseMessage]:
    """Prepend a freshly-rendered system prompt so today's date is current.

    Reads TenantRuntimeConfig from the ContextVar set by run_agent. Falls back
    to a settings-based prompt for dev scripts (Fase A convenience).
    """
    config = _tenant_config_ctx.get()
    if config is not None:
        content = secretary_system_prompt(config)
    else:
        # Dev fallback: single-tenant prompt from env vars.
        from secretaria.services.tenant_config import TenantRuntimeConfig as _RC

        s = get_settings()
        content = secretary_system_prompt(
            _RC(
                tenant_id=None,  # type: ignore[arg-type]
                clinic_name="Clínica",
                greeting_message=None,
                persona_notes=None,
                language="pt-BR",
                timezone=s.CLINIC_TIMEZONE,
                appointment_duration_min=30,
                appointment_types=[],
                business_hours={},
                google_calendar_id="primary",
                google_refresh_token=None,
            )
        )
    return [SystemMessage(content=content), *state["messages"]]


def build_agent(extra_tools: Sequence = ()) -> Any:
    """Compile (or fetch from cache) the ReAct agent for base tools + extra_tools.

    Cached per distinct capability set, keyed by tool NAME (see `_AGENTS`
    above) so the process can serve tenants with different plugin
    entitlements concurrently without rebuilding the graph on every call.
    The base tools are always included and unchanged.
    """
    tools = [*_BASE_TOOLS, *extra_tools]
    key = frozenset(getattr(t, "name", str(t)) for t in tools)
    cached = _AGENTS.get(key)
    if cached is not None:
        return cached

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
        # Default is 2 — bumping to 5 cushions us against SSL_EOF /
        # APIConnectionError flakes that show up under sustained load.
        # The openai SDK applies exponential backoff between attempts.
        max_retries=5,
        # Per-request HTTP timeout. Without an explicit value the SDK falls
        # back to "no timeout", which means a stuck socket can block the
        # whole arq job until the worker is killed.
        timeout=60,
    )
    agent = create_react_agent(
        model,
        tools=tools,
        prompt=_prompt_with_today,
    )
    _AGENTS[key] = agent
    return agent


async def invoke_agent(messages: list[BaseMessage]) -> str:
    """Run the agent on a message list, return the last assistant reply.

    Shared by the dev terminal (scripts/test_agent.py) and run_agent below.
    Reads `_extra_tools_ctx` (set by run_agent) rather than taking extra_tools
    as a parameter, so this function's signature — and every existing test
    that monkeypatches it with a `(messages)`-only fake — stays unchanged.
    """
    result = await build_agent(_extra_tools_ctx.get()).ainvoke({"messages": messages})
    last = result["messages"][-1]
    return (getattr(last, "content", "") or "").strip()


async def _invoke_agent_with_retry(
    messages: list[BaseMessage],
    conversation_id: UUID,
) -> str:
    """Top-level safety net: one extra attempt on transient network errors.

    The openai SDK already retries APIConnectionError / APITimeoutError 5x
    inside a single LLM call. This wrapper covers the remaining failure
    mode: a TLS connection that survives the SDK retries but dies between
    the LLM call and the tool call (or vice-versa) inside the ReAct loop.
    One full re-invocation of the agent salvages the turn at the cost of
    re-doing the work already done in the failed attempt.
    """
    try:
        return await invoke_agent(messages)
    except _TRANSIENT_NETWORK_EXCEPTIONS as exc:
        logger.warning(
            "ai_run_agent_transient_retry",
            error=str(exc),
            error_type=type(exc).__name__,
            conversation_id=str(conversation_id),
        )
        await asyncio.sleep(1)
        return await invoke_agent(messages)


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


async def run_agent(
    message: str,
    context: dict,
    tenant_config: TenantRuntimeConfig | None = None,
    extra_tools: Sequence = (),
    redis=None,
) -> str:
    """arq-side entry point: build history + run agent + return reply text.

    `tenant_config` provides per-tenant Calendar credentials and prompt data.
    When None, falls back to the single-tenant env-var scaffold (Fase A / dev).
    `extra_tools` are plugin-contributed LangChain tools for THIS tenant's
    entitlements (see plugins/registry.py:agent_tools_for) — appended to the
    base tools; the agent for that exact combination is resolved/cached by
    build_agent (via invoke_agent, which reads them off `_extra_tools_ctx`).
    `redis` is the arq Redis pool (workers/tasks.py's `ctx["redis"]`), threaded
    through via `_redis_ctx` purely so a tool deep inside the agent loop
    (ai/tools.py:_persist_appointment) can fire-and-forget enqueue
    `run_post_booking_hooks` after a successful booking. `None` (dev scripts,
    tests) makes that enqueue a silent no-op.
    """
    conversation_id = UUID(context["conversation_id"])

    # Build a per-tenant CalendarService and inject it via ContextVar so the
    # cached process-wide agent uses the right credentials for this call.
    cal = (
        CalendarService.from_tenant_config(tenant_config)
        if tenant_config is not None
        else CalendarService()
    )
    tok_cal = _calendar_ctx.set(cal)
    tok_cfg = _tenant_config_ctx.set(tenant_config)
    tok_conv = _conversation_id_ctx.set(conversation_id)
    tok_tid = _tenant_id_ctx.set(tenant_config.tenant_id if tenant_config else None)
    tok_tools = _extra_tools_ctx.set(extra_tools)
    tok_redis = _redis_ctx.set(redis)

    try:
        history = await _load_history(conversation_id)
        if not history:
            logger.warning("ai_run_agent_no_history", conversation_id=str(conversation_id))
            history = [HumanMessage(content=message)]
        reply = await _invoke_agent_with_retry(history, conversation_id)
    except CalendarUnavailableError:
        # A calendar tool failed (token revoked / Google down / 5xx). It
        # propagates unwrapped out of the LangGraph ToolNode, so we catch it by
        # type here. The worker turns this sentinel into a patient message and
        # hands the conversation to a human secretary. (A ContextVar flag would
        # NOT work: LangGraph runs tool nodes in a copied context.)
        logger.warning(
            "ai_run_agent_calendar_unavailable",
            conversation_id=str(conversation_id),
        )
        return CALENDAR_UNAVAILABLE_SENTINEL
    except Exception as exc:
        logger.error(
            "ai_run_agent_failed",
            error=str(exc),
            error_type=type(exc).__name__,
            conversation_id=str(conversation_id),
        )
        return FALLBACK_REPLY
    finally:
        _calendar_ctx.reset(tok_cal)
        _tenant_config_ctx.reset(tok_cfg)
        _conversation_id_ctx.reset(tok_conv)
        _tenant_id_ctx.reset(tok_tid)
        _extra_tools_ctx.reset(tok_tools)
        _redis_ctx.reset(tok_redis)

    if not reply:
        logger.warning("ai_run_agent_empty_reply", conversation_id=str(conversation_id))
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
