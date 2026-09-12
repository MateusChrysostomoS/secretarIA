"""Outbound dispatch, one implementation per channel the clinic can be reached on.

secretaria grew up speaking exactly one protocol: every outbound send in
`workers/tasks.py` built a `WhatsAppClient` and called one of its four methods.
Brain-Message (the web console a patient talks to instead of WhatsApp) needs the
same replies delivered a completely different way - there is no Graph API and no
phone number, the message simply has to EXIST in the database for the patient's
client to poll.

`ChannelSender` is the seam that makes both possible without the ~60 send call
sites in `workers/tasks.py` learning that channels exist. It is deliberately
shaped as the four methods `WhatsAppClient` already has, with the same names and
the same signatures, so `WhatsAppClient` satisfies it structurally and every one
of those call sites is unchanged, byte for byte.

## `persists_outbound` - the one thing the two channels genuinely disagree about

On WhatsApp, a reply EXISTS because Meta delivered it; the `Message` row is a
history copy the caller writes afterwards, from the send response
(`wam_id=_extract_sent_wam_id(result)`). That ordering is load-bearing and was
verified before this module was written: in all three sites that record an
outbound message (`_dispatch_bubbles`, `_send_greeting`, `_send_consent_notice`)
the row is written strictly AFTER a successful call, and a raised send error
returns before the write. Several other sends (action-button replies,
`service_unavailable`, `calendar_unavailable`) never write a row at all - on
WhatsApp that is merely a gap in LLM history.

On Brain-Message that same gap would be the whole message: with no network leg,
an unwritten row means the patient is answered with silence. So the
Brain-Message sender writes the row ITSELF, for every send, and the three
callers that would otherwise write a second one skip theirs when
`persists_outbound` is True. The flag is the honest name for that asymmetry
rather than a special case buried in the callers.
"""

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID

from secretaria.core.logging import get_logger
from secretaria.models import Conversation, Message, MessageDirection, MessageSender
from secretaria.services.whatsapp import interactive_buttons_record, interactive_list_record

logger = get_logger(__name__)

# Channel identifiers. These are the values stored in `Patient.channel`; kept
# here as well as in `models/patient.py` so the send side has a name to branch
# on without importing the model layer for a string.
CHANNEL_WHATSAPP = "whatsapp"
CHANNEL_BRAIN_MESSAGE = "brain_message"


def interactive_history_body(body: str, labels: list[str]) -> str:
    """Render an interactive card as the single line of text history keeps.

    The same idiom `workers/tasks.py::_bubble_history_body` uses for a
    `MenuBubble`, and for the same reason: the next agent turn is rebuilt from
    the `messages` table, so an options card that collapsed to its bare body
    would leave the model unable to see what it had just offered.

    Deliberately a SEPARATE function from `_bubble_history_body` rather than a
    shared one. That function is on the WhatsApp path, where the caller already
    holds the typed bubble and decides its own history spelling; changing it to
    serve this one would be a behaviour change on the production channel for no
    reason. This one is reached only with the flattened `(body, labels)` a
    sender receives.
    """
    if not labels:
        return body
    return f"{body}\n(opções: {', '.join(labels)})"


def sender_persists_outbound(sender) -> bool:
    """Does this sender write the outbound `Message` row itself?

    Read through `getattr` with a False default rather than as a plain
    attribute, and the default is the load-bearing part: False means "the caller
    records it", which is exactly what every send site did before channels
    existed. So anything shaped like the old `WhatsAppClient` - including the
    dozen hand-rolled test doubles that stand in for it, and any future sender
    that implements only the four `send_*` methods - keeps its pre-refactor
    behaviour without having to know this flag exists.

    Opting IN is what changes anything, and only `BrainMessageSender` does.
    """
    return bool(getattr(sender, "persists_outbound", False))


@runtime_checkable
class ChannelSender(Protocol):
    """What `workers/tasks.py` needs of a channel in order to answer a patient.

    Four intents, not four WhatsApp payloads: plain text, a small set of
    tappable choices, a longer pickable list, and a pre-approved out-of-window
    template. Nothing WhatsApp-shaped crosses this boundary except the argument
    NAMES, which are kept identical to `WhatsAppClient`'s so the existing call
    sites need no edit.
    """

    # True when the sender writes the outbound `Message` row itself, so the
    # caller must not write a second one. See this module's docstring.
    persists_outbound: bool

    async def send_text_message(self, to: str, body: str) -> dict: ...

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> dict: ...

    async def send_list(
        self,
        to: str,
        body: str,
        button_label: str,
        rows: list[tuple[str, str, str | None]],
        section_title: str = "Opções",
    ) -> dict: ...

    async def send_template(
        self,
        to: str,
        template: str,
        lang: str,
        variables: list[str],
        button_payloads: list[str] | None = None,
    ) -> dict: ...


class BrainMessageSender:
    """Deliver a bot reply to a Brain-Message patient by recording it.

    There is no network leg. The patient's console polls
    `GET /internal/brain-message/conversations/{external_id}/messages`, so
    writing the row IS the delivery - which is why `persists_outbound` is True
    and why every method here writes one.

    Each send opens its own short transaction, mirroring `_dispatch_bubbles`:
    the inbound turn's transaction has already committed by the time replies go
    out, and one failed bubble must not roll back the ones before it.

    `to` (the patient's `external_id`) is accepted and ignored: the conversation
    this sender was built for is the address. Keeping the parameter is what lets
    `WhatsAppClient` and this class share call sites.
    """

    persists_outbound = True

    def __init__(self, *, conversation_id: UUID, session_factory) -> None:
        self._conversation_id = conversation_id
        # Injected rather than imported so tests wire the same in-memory engine
        # they already monkeypatch onto `workers.tasks.async_session_factory`.
        self._session_factory = session_factory

    async def _record(self, body: str, interactive: dict | None = None) -> dict:
        """Persist one outbound message. Returns the empty send response.

        `interactive` is the card a reply-button / list send put on the
        patient's screen (`Message.interactive`), the same record
        `workers/tasks.py::_record_outbound` stores for WhatsApp; `body` stays
        the flattened history text either way.

        `{}` on purpose: `_extract_sent_wam_id` reads `["messages"][0]["id"]`
        out of it and already returns None for a shape it cannot walk, so a
        caller that still writes its own row (none do today - they check
        `persists_outbound` first) would produce `wam_id=None`, which is exactly
        right for a message Meta never saw.
        """
        async with self._session_factory() as session:
            async with session.begin():
                session.add(
                    Message(
                        conversation_id=self._conversation_id,
                        direction=MessageDirection.OUTBOUND,
                        sender=MessageSender.BOT,
                        # No Meta id exists for a message Meta never carried.
                        wam_id=None,
                        body=body,
                        interactive=interactive,
                    )
                )
                conversation = await session.get(Conversation, self._conversation_id)
                if conversation is not None:
                    conversation.last_bot_message_at = datetime.now(UTC)
        logger.info(
            "brain_message_outbound_recorded",
            conversation_id=str(self._conversation_id),
        )
        return {}

    async def send_text_message(self, to: str, body: str) -> dict:
        return await self._record(body)

    async def send_buttons(self, to: str, body: str, buttons: list[tuple[str, str]]) -> dict:
        # The card is recorded through the SAME builder WhatsApp's payload is
        # built from, so the portal's buttons suffer the same caps (3 buttons,
        # truncated titles) as the real thing and the ids it can tap back are
        # exactly the ids a WhatsApp patient could.
        return await self._record(
            interactive_history_body(body, [label for _, label in buttons]),
            interactive=interactive_buttons_record(body, buttons),
        )

    async def send_list(
        self,
        to: str,
        body: str,
        button_label: str,
        rows: list[tuple[str, str, str | None]],
        section_title: str = "Opções",
    ) -> dict:
        return await self._record(
            interactive_history_body(body, [row[1] for row in rows]),
            interactive=interactive_list_record(body, button_label, rows, section_title),
        )

    async def send_template(
        self,
        to: str,
        template: str,
        lang: str,
        variables: list[str],
        button_payloads: list[str] | None = None,
    ) -> dict:
        """Refuse, loudly, and write nothing.

        A WhatsApp template is a body approved by Meta that this process does
        not have: it holds only the NAME and the positional variables. On
        WhatsApp that is enough because Meta renders it. Here it is not - the
        honest options are to refuse or to invent a body, and inventing one
        would put text the clinic never approved in a patient's console.

        Reminders/HSM are explicitly out of scope for the channel-neutral
        pipeline (they exist to reopen WhatsApp's 24h window, which
        Brain-Message does not have). Proactive Brain-Message reminders are a
        real and probably simpler feature, but they need their own message
        source, not this one. See docs/CHECKPOINT_brain_message_pipeline.md.
        """
        logger.warning(
            "brain_message_template_unsupported",
            conversation_id=str(self._conversation_id),
            template=template,
        )
        return {}
