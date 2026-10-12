"""The patient never goes unanswered - and the guarantee cannot be turned into a weapon.

Every inbound turn must end with SOMETHING the patient can read. Before this
module, several paths ended the turn in silence: an exception anywhere in the
reply pipeline, a model reply that parsed to zero bubbles, a job killed by arq's
timeout, a send that failed and was logged-and-forgotten. The patient saw their
own message sit there with no answer and no hint that anything was wrong.

The net is deliberately dumb. It sends ONE fixed apology that costs nothing: no
LLM call, no tool, no tenant data. That is what makes it safe to promise - it
cannot itself fail the way the thing it is covering for just failed.

Abuse bound: someone who wants to make the service burn resources can send a
message that reliably breaks a turn, over and over. The apology is therefore
capped per conversation per window (`TURN_FALLBACK_MAX_PER_WINDOW` in
`TURN_FALLBACK_WINDOW_SECONDS`). Past the cap the turn stays silent and a
`turn_fallback_throttled` WARNING is logged, because by then the conversation is
either a bug looping or a sender worth rate limiting - not a patient waiting.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

# The turn's send ledger: a one-element list so every task in the turn shares
# ONE counter by reference (asyncio copies the ContextVar value into child
# tasks, and a copied list is still the same list). Incremented at the two
# places a message actually leaves - `WhatsAppClient._post` and
# `BrainMessageSender._record` - rather than at each of the dozens of call
# sites, so a send path added next year is counted without anyone remembering.
# NOT measured by rows in `messages`: `_send_simple_text` and friends send on
# WhatsApp without recording one, so a row count would call those turns silent.
_turn_sends: ContextVar[list[int] | None] = ContextVar("turn_sends", default=None)


# TASK-038: the agent's own words for the patient on a hand-back to the buttons
# (ai/graph.py::HANDBACK_INTRO_PREFIX). Held, never sent alone: the next bubble batch
# the turn sends (workers/shared/dispatch.py::_dispatch_bubbles) takes them in front of
# the card. A hand-back that ends up sending nothing leaves them unsent - the turn
# stays silent and the apology below still answers - and `end_turn` drops them.
_held_intro: ContextVar[list[str] | None] = ContextVar("held_intro", default=None)


def begin_turn() -> Token:
    """Open a fresh send ledger for one inbound turn; reset it with `end_turn`."""
    return _turn_sends.set([0])


def end_turn(token: Token) -> None:
    _turn_sends.reset(token)
    _held_intro.set(None)


def hold_intro(text: str) -> None:
    """Hold words to go out in front of this turn's next card. No-op outside a turn."""
    if _turn_sends.get() is not None:
        _held_intro.set([text])


def take_held_intro() -> str | None:
    """The held words, once (the first batch to ask gets them), or None."""
    box = _held_intro.get()
    return box.pop() if box else None


def note_send() -> None:
    """Record that one message really left. No-op outside a turn (reminders, staff)."""
    box = _turn_sends.get()
    if box is not None:
        box[0] += 1


def sends_in_turn() -> int:
    box = _turn_sends.get()
    return box[0] if box is not None else 0


# Says what happened (a hiccup on our side), asks for the one thing that
# helps (repeat), and names the way out that never needs the model (the menu).
TURN_FALLBACK_MESSAGE = (
    "Desculpe, tive uma instabilidade aqui 🙏. Pode repetir sua mensagem? "
    "Se preferir, digite *menu* para ver as opções."
)


def _key(conversation_id) -> str:
    return f"turnfallback:{conversation_id}"


async def fallback_allowed(redis, conversation_id) -> bool:
    """Whether this conversation may receive another safety-net apology now.

    Fail-OPEN when Redis is missing or erroring: the apology is the cheap, fixed
    message, and a Redis outage must not be the reason a patient is left
    unanswered. (The WhatsApp flood limit in workers/tasks.py::_is_rate_limited
    fails open for the same reason.)
    """
    if redis is None:
        return True
    settings = get_settings()
    key = _key(conversation_id)
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, settings.TURN_FALLBACK_WINDOW_SECONDS)
        return count <= settings.TURN_FALLBACK_MAX_PER_WINDOW
    except Exception as exc:
        logger.warning(
            "turn_fallback_throttle_check_failed",
            error_type=type(exc).__name__,
            conversation_id=str(conversation_id),
        )
        return True
