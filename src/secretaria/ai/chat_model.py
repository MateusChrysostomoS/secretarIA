"""The conversational model, whichever provider answers the patient.

`Settings.LLM_PROVIDER` picks it: "openai" (gpt-5-mini, the original) or
"anthropic" (Claude Haiku 5.5, owner decision 2026-10-21 - about half the cost
per turn). Both the agent (ai/graph.py) and the guided help (ai/scoped_help.py)
build their model here, so the provider is one setting and the rest of the code
reads the same LangChain messages either way.

Two things differ between the providers and are absorbed here:
- an Anthropic reply's `content` is a list of blocks (thinking, text, tool use),
  not a string: `message_text` reads the visible text of either shape;
- Claude caches a prompt prefix only where it is told to: `system_message` marks
  the system prompt (tools + system are the prefix every call of a clinic shares).
"""

from typing import Any

import anthropic
import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage

from secretaria.config import get_settings

PROVIDER_OPENAI = "openai"
PROVIDER_ANTHROPIC = "anthropic"

#: Network failures either SDK raises after its own retries: ai/graph.py re-runs the
#: whole turn once on these.
PROVIDER_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
)


def provider() -> str:
    return get_settings().LLM_PROVIDER


def active_model_name() -> str:
    s = get_settings()
    if s.LLM_PROVIDER == PROVIDER_ANTHROPIC:
        return s.ANTHROPIC_SECRETARIA_MODEL
    return s.OPENAI_SECRETARIA_MODEL


def active_max_tokens() -> int:
    s = get_settings()
    return s.ANTHROPIC_MAX_TOKENS if s.LLM_PROVIDER == PROVIDER_ANTHROPIC else s.OPENAI_MAX_TOKENS


def build_chat_model(*, timeout: float) -> BaseChatModel:
    """A chat model for one caller. `timeout` is the per-request HTTP timeout.

    Without an explicit timeout either SDK can wait on a stuck socket until the
    arq job is killed; 5 SDK retries (exponential backoff) cushion the TLS flakes
    seen under load.
    """
    s = get_settings()
    if s.LLM_PROVIDER == PROVIDER_ANTHROPIC:
        if not s.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY missing in environment.")
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(
            model=s.ANTHROPIC_SECRETARIA_MODEL,
            api_key=s.ANTHROPIC_API_KEY,
            max_tokens=s.ANTHROPIC_MAX_TOKENS,
            # Adaptive thinking is Haiku 5.5's default; effort sets how deep. It stays
            # on unless ANTHROPIC_THINKING says otherwise: with it off a model can
            # write a tool call as visible text (accepted at effort high or below).
            thinking={"type": s.ANTHROPIC_THINKING},
            output_config={"effort": s.ANTHROPIC_EFFORT},
            max_retries=5,
            default_request_timeout=timeout,
        )
    if not s.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in environment.")
    from langchain_openai import ChatOpenAI

    return ChatOpenAI(
        model=s.OPENAI_SECRETARIA_MODEL,
        api_key=s.OPENAI_API_KEY,
        # gpt-5 / o-series reasoning models require max_completion_tokens
        # (not max_tokens). langchain-openai 1.x accepts it as a direct
        # kwarg even though it isn't a typed field.
        max_completion_tokens=s.OPENAI_MAX_TOKENS,
        max_retries=5,
        timeout=timeout,
    )


def system_message(content: str) -> SystemMessage:
    """The system prompt as the provider caches it best.

    OpenAI caches a long shared prefix by itself: plain text. Claude caches only up
    to a marked block, so the prompt is one text block with an ephemeral (5-minute)
    cache mark - the tools render before it, so they ride the same cache entry.
    """
    if provider() == PROVIDER_ANTHROPIC:
        return SystemMessage(
            content=[{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        )
    return SystemMessage(content=content)


def message_text(message: BaseMessage | Any) -> str:
    """The visible text of a model message, stripped - "" when it has none.

    A string `content` (OpenAI) is the text; a list (Claude) holds blocks, and only
    the `text` ones are speech - thinking and tool-use blocks are not.
    """
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


def stop_reason(message: BaseMessage | Any) -> str | None:
    """Why the model stopped: OpenAI's `finish_reason`, Claude's `stop_reason`."""
    meta = getattr(message, "response_metadata", None) or {}
    reason = meta.get("finish_reason") or meta.get("stop_reason")
    return str(reason) if reason else None
