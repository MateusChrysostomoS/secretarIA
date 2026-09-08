"""LangChain-facing half of the pseudonymization step.

`services/pii_pseudonymization.py` owns the map (load, carry, persist); this
module owns the two places the map meets LangChain:

  * `scrub_messages` - mask a history list on the way INTO the model.
  * `wrap_tools_with_pseudonymizer` - the symmetric guard at the TOOL boundary.

WHY THE TOOL BOUNDARY NEEDS ITS OWN GUARD
-----------------------------------------
`run_agent` masks the history and re-hydrates the reply, but the ReAct loop runs
tool calls BETWEEN those two points, inside a single `ainvoke`. Two things cross
that boundary and neither passes through the history or the reply:

  IN  - tool RESULTS re-enter the model's context. `check_availability` returns
        real Google Calendar event titles from the CLINIC's agenda
        (services/calendar.py says so in as many words: it "surfaces real
        summaries to the LLM, which it can quote back to the patient").

  OUT - tool ARGUMENTS leave for real external systems. `create_event`'s
        docstring instructs the model to title the event "Consulta - Joao
        Silva", and the model takes that name from the history it was shown.
        Masking the history WITHOUT this guard would therefore write
        "Consulta - [PACIENTE_a1b2]" onto the doctor's real calendar - not a
        leak, a corruption, and a guaranteed one rather than a corner case.

So the guard is symmetric and sits at the boundary itself rather than in three
individual tools: every current tool AND every plugin tool a tenant is entitled
to (plugins/registry.py::agent_tools_for) is covered by construction, including
ones written after this.

WHAT IT DOES NOT DO
-------------------
Scrubbing a tool result masks PATTERNS (phone/CPF/e-mail) and values already
KNOWN to the map - notably this patient's own name coming back from Google
Calendar. It cannot mask an unknown third party's name: the engine detects
names by lookup, not by inference, so "Consulta - Maria Silva" sitting in the
clinic's calendar still reaches the model as itself. Narrowing what
`check_availability` returns is the fix for that, and it is a product decision
about what the agent may quote back, not a pseudonymization one.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage
from pseudonymize_core import Pseudonymizer, has_unresolved_tokens

from secretaria.core.logging import get_logger
from secretaria.services.pii_pseudonymization import current_pseudonymizer

logger = get_logger(__name__)


def scrub_messages(messages: Sequence[BaseMessage], p: Pseudonymizer) -> list[BaseMessage]:
    """Mask every message's content, preserving its TYPE.

    `model_copy` rather than mutation: the caller's list may be shared (the
    fallback single-message history is built from the worker's own string), and
    a HumanMessage must stay a HumanMessage - the model reads turn ownership
    from the class, not from the text.
    """
    out: list[BaseMessage] = []
    for m in messages:
        content = getattr(m, "content", None)
        if not content:
            out.append(m)
            continue
        out.append(m.model_copy(update={"content": p.scrub(content)}))
    return out


def _wrap_one(tool: Any) -> Any:
    """One tool, guarded on both sides. Returns the tool itself if unwrappable."""
    inner = getattr(tool, "coroutine", None)
    if inner is None:
        # Sync tools would need the same treatment, but every tool in this repo
        # (and every plugin tool) is async. Returning the original unchanged is
        # the honest failure: a silent partial wrap would be worse than a loud
        # one, and this is logged at build time, once per agent build.
        logger.warning("pii_tool_wrap_skipped_not_async", tool=getattr(tool, "name", str(tool)))
        return tool

    async def _guarded(*args: Any, **kwargs: Any) -> Any:
        p = current_pseudonymizer()
        if p is None:
            # No pseudonymized turn in flight (dev terminal, direct callers):
            # behave exactly as before this feature existed.
            return await inner(*args, **kwargs)
        # OUT: whatever the model wrote is un-tokenized before it reaches
        # Google Calendar / the DB.
        args = tuple(p.rehydrate(a) for a in args)
        kwargs = {k: p.rehydrate(v) for k, v in kwargs.items()}
        # NOT wrapped in try/except: CalendarUnavailableError and the
        # ShowMainMenuRequested / SelectProfessionalRequested /
        # ManageAppointmentRequested / GuidedBookingRequested sentinels
        # propagate out of the ToolNode by design, and run_agent catches them
        # by type. Swallowing one here would silently disable a hand-back.
        result = await inner(*args, **kwargs)
        # IN: whatever comes back is masked before re-entering the model's
        # context. This MUTATES the shared map (a phone found in an event
        # title becomes a new entry) - which is why run_agent persists AFTER
        # the invoke, not before it.
        return p.scrub(result)

    # model_copy keeps name, description, args_schema, return_direct and every
    # other field - so the agent cache key in graph.py (a frozenset of tool
    # NAMES) is unchanged, and the model sees exactly the tool it saw before.
    return tool.model_copy(update={"coroutine": _guarded})


def wrap_tools_with_pseudonymizer(tools: Sequence[Any]) -> tuple[Any, ...]:
    """Wrap a tool sequence for the agent. Safe to call once per agent build.

    The wrapper resolves the Pseudonymizer at CALL time from the ContextVar,
    never at wrap time, so one wrapped tuple serves every conversation and the
    process-wide agent cache stays valid.
    """
    return tuple(_wrap_one(t) for t in tools)


def rehydrate_reply(reply: str, p: Pseudonymizer, conversation_id: Any) -> str:
    """Un-tokenize the bot's reply, and shout if anything is left over.

    A token that survives here is printed to the patient verbatim
    ("[PACIENTE_a7f3]"), so this must never fail quietly - the log line is the
    only way to learn that the map lost an entry.
    """
    out = p.rehydrate(reply)
    if has_unresolved_tokens(out):
        logger.error(
            "pii_unresolved_tokens_in_reply",
            conversation_id=str(conversation_id),
            # Never the text itself - it quotes the patient back (PROMPT_FIX_21).
            reply_len=len(out),
            token_count=len(p.tokens),
        )
    return out
