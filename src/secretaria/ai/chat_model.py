"""Which vendor's chat model answers: Anthropic's Claude Haiku, or OpenAI only without a Claude key.

Owner rule (2026-10-27): Claude Haiku answers EVERY chat call whenever ANTHROPIC_API_KEY is
configured; OpenAI is used only when no Anthropic key is configured at all. There is no other
switch - the presence of the key decides, read at each model build. Every chat call
site (the conversation agent in ai/graph.py, the scoped-help decision in ai/scoped_help.py, the
context-source extraction in workers/shared/context_ingestion.py) builds its model here, so the
two vendors never drift apart in which knobs they get. Audio transcription is NOT a chat call:
it stays on OpenAI (or Groq) whatever this says — Anthropic has no speech-to-text.

Vendor differences this module absorbs (so callers stay vendor-blind):
- Token cap: OpenAI's `max_completion_tokens` vs Anthropic's `max_tokens`. Claude's thinking
  counts against the cap, so it gets its own, larger budget (`ANTHROPIC_MAX_TOKENS`).
- Structured output: Claude Opus/Sonnet 5.5 reject a FORCED tool call (`tool_choice` any/tool),
  which is how "function_calling" structured output works; Claude uses the API's native
  structured output (`output_config.format`, LangChain's `method="json_schema"`) instead.
- Reply text: Claude returns a list of content blocks (thinking + text), not a plain string;
  `reply_text` reads only the text a patient may see.
- Policy declines: on the models that support it, a server-side refusal fallback is on (the
  API re-runs a declined request on another model inside the same call).
"""

from __future__ import annotations

import re
import ssl
import warnings
from typing import Any

import anthropic
import httpx
import openai
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage, SystemMessage

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

OPENAI = "openai"
ANTHROPIC = "anthropic"

# Models that accept the server-side refusal fallback in its "default" form (Claude API).
# Claude Haiku 5.5 has none: a declined Haiku turn simply ends without text, which the turn
# fallback (FALLBACK_REPLY / the fixed apology) already covers.
_REFUSAL_FALLBACK_MODELS = frozenset(
    {"claude-fable-5-1", "claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5"}
)
_REFUSAL_FALLBACK_BETA = "server-side-fallback-2026-07-01"

# Errors a single top-level retry usually recovers from, for BOTH vendors: TLS torn down
# mid-read, a brief gateway refusal, a timeout worth one more shot. The SDKs already retry
# inside one call; see ai/graph.py `_invoke_agent_with_retry`.
TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    httpx.ReadError,
    httpx.ConnectError,
    httpx.RemoteProtocolError,
    httpx.ReadTimeout,
    ssl.SSLEOFError,
)


_fallback_logged = False


def provider() -> str:
    """The vendor that will actually answer: Claude with an Anthropic key, OpenAI without one.

    When no Anthropic key is configured but OPENAI_API_KEY is (a deploy that forgot the key),
    the chat runs on OpenAI and says so once per process at ERROR: patients keep getting
    answers, and the log shows the misconfiguration. With neither key the build fails.
    """
    global _fallback_logged
    s = get_settings()
    if s.ANTHROPIC_API_KEY:
        return ANTHROPIC
    if s.OPENAI_API_KEY:
        if not _fallback_logged:
            logger.error("llm_provider_fallback", wanted=ANTHROPIC, using=OPENAI)
            _fallback_logged = True
        return OPENAI
    return ANTHROPIC  # build_chat_model then fails loudly: ANTHROPIC_API_KEY missing


def model_name() -> str:
    """The model id the configured vendor will be asked for (for logs and traces)."""
    s = get_settings()
    return s.ANTHROPIC_SECRETARIA_MODEL if provider() == ANTHROPIC else s.OPENAI_SECRETARIA_MODEL


def token_cap(openai_cap: int) -> int:
    """The per-call output cap actually sent: the caller's OpenAI cap, or Claude's own budget."""
    return get_settings().ANTHROPIC_MAX_TOKENS if provider() == ANTHROPIC else openai_cap


def build_chat_model(*, openai_max_tokens: int, max_retries: int, timeout: float) -> BaseChatModel:
    """A LangChain chat model for the configured vendor.

    `openai_max_tokens` is the caller's existing OpenAI budget (OPENAI_MAX_TOKENS or the
    extraction budget); Claude ignores it in favour of ANTHROPIC_MAX_TOKENS (see module doc).
    Raises RuntimeError when the vendor's key is missing, like the call sites always did.
    """
    s = get_settings()
    if provider() == ANTHROPIC:
        from langchain_anthropic import ChatAnthropic

        if not s.ANTHROPIC_API_KEY:
            raise RuntimeError("ANTHROPIC_API_KEY missing in environment.")
        kwargs: dict[str, Any] = {}
        effort = (s.ANTHROPIC_EFFORT or "").strip().lower()
        if effort:
            kwargs["output_config"] = {"effort": effort}
        if (s.ANTHROPIC_THINKING or "").strip().lower() == "disabled":
            kwargs["thinking"] = {"type": "disabled"}
        if s.ANTHROPIC_SECRETARIA_MODEL in _REFUSAL_FALLBACK_MODELS:
            kwargs["betas"] = [_REFUSAL_FALLBACK_BETA]
            kwargs["model_kwargs"] = {"fallbacks": "default"}
        return ChatAnthropic(
            model=s.ANTHROPIC_SECRETARIA_MODEL,
            api_key=s.ANTHROPIC_API_KEY,
            max_tokens=s.ANTHROPIC_MAX_TOKENS,
            max_retries=max_retries,
            default_request_timeout=timeout,
            **kwargs,
        )

    from langchain_openai import ChatOpenAI

    if not s.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY missing in environment.")
    return ChatOpenAI(
        model=s.OPENAI_SECRETARIA_MODEL,
        api_key=s.OPENAI_API_KEY,
        # gpt-5 / o-series reasoning models require max_completion_tokens
        # (not max_tokens). langchain-openai 1.x accepts it as a direct
        # kwarg even though it isn't a typed field.
        max_completion_tokens=openai_max_tokens,
        max_retries=max_retries,
        timeout=timeout,
    )


def build_structured_model(
    schema: Any,
    *,
    openai_max_tokens: int,
    max_retries: int,
    timeout: float,
    native_schema: bool = True,
) -> Any:
    """`build_chat_model(...).with_structured_output(schema)` with the vendor's right method.

    On Claude, `native_schema=True` uses the API's constrained structured output (the answer
    always matches the schema). A big schema (the context extraction's) is refused there with
    400 "Schema is too complex": pass `native_schema=False` and Claude is ASKED, not forced, to
    call one tool shaped like the schema (forcing is rejected on Opus/Sonnet 5.5); a reply
    without that call raises, which callers already treat as a model failure.
    """
    model = build_chat_model(
        openai_max_tokens=openai_max_tokens, max_retries=max_retries, timeout=timeout
    )
    native = provider() == ANTHROPIC and native_schema
    method = "json_schema" if native else "function_calling"
    if provider() == ANTHROPIC and not native:
        # langchain-anthropic warns on every build that forcing is unavailable; that is the
        # point of this path, and the missing-call case raises (see above).
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            return model.with_structured_output(schema, method=method)
    return model.with_structured_output(schema, method=method)


def system_message(content: str) -> SystemMessage:
    """The system prompt as the vendor caches it best.

    OpenAI caches a long shared prefix by itself: plain text. Claude caches only up to a marked
    block, so the prompt is one text block with an ephemeral (5-minute) cache mark - the tools
    render before it, so they ride the same cache entry.
    """
    if provider() == ANTHROPIC:
        return SystemMessage(
            content=[{"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}]
        )
    return SystemMessage(content=content)


def reply_text(message: BaseMessage | None) -> str:
    """The visible text of a model message, stripped; thinking and tool blocks never count.

    OpenAI hands back a string; Claude a list of blocks (`thinking`, `text`, `tool_use`).
    """
    if message is None:
        return ""
    content = getattr(message, "content", "") or ""
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(str(block.get("text") or ""))
    return "".join(parts).strip()


_MARKDOWN_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")


def whatsapp_emphasis(text: str) -> str:
    """Markdown bold (`**x**`, Claude's habit) -> WhatsApp bold (`*x*`).

    WhatsApp and the Brain-Message bubbles bold `*x*`; `**x**` would show stray asterisks.
    """
    return _MARKDOWN_BOLD.sub(r"*\1*", text)
