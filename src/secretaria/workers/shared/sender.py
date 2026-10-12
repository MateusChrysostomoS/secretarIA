"""sender - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger, wa_suffix
from secretaria.models import (
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Tenant,
)
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    BrainMessageSender,
    ChannelSender,
)
from secretaria.services.whatsapp import (
    TenantWhatsAppCredentialMissing,
    WhatsAppClient,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)

logger = get_logger(__name__)


def _reply_sender(
    reply: _ReplyContext,
    tenant: Tenant | None,
    waba_token: str | None,
) -> ChannelSender | None:
    """The thing that delivers this reply, chosen by the channel it came in on.

    The ONE place a channel is branched on. Everything downstream calls the four
    `send_*` methods without knowing which implementation answered, because
    `WhatsAppClient` and `BrainMessageSender` present the same four signatures
    (services/channel_sender.py).

    Fail-closed on both branches, for different reasons: WhatsApp because
    sending on another clinic's number is worse than not sending (PROMPT_FIX_21),
    Brain-Message because a reply with no conversation has nowhere to be
    written and would vanish silently rather than loudly.
    """
    if reply.channel == CHANNEL_BRAIN_MESSAGE:
        if reply.conversation_id is None:
            logger.error(
                "brain_message_sender_no_conversation",
                tenant_id=str(getattr(tenant, "id", None)),
            )
            return None
        return BrainMessageSender(
            conversation_id=reply.conversation_id,
            session_factory=async_session_factory,
        )
    return _tenant_client(tenant, waba_token)

async def _record_outbound(
    conversation_id: UUID | None,
    body: str,
    send_response: dict,
    interactive: dict | None = None,
) -> None:
    """Write the history copy of a message the CHANNEL has already delivered.

    Extracted verbatim from the three call sites that each held their own copy
    of it (`_dispatch_bubbles`, `_send_greeting`, `_send_consent_notice`) when
    the sender abstraction landed, because those three now have to SKIP it when
    the sender persisted the row itself. One spelling, one place to gate.

    Called strictly AFTER a successful send, and that ordering is the answer to
    the question this refactor started from: on WhatsApp the row is derived from
    the send response (`wam_id`), so persistence has never been independent of
    the Graph API call. See services/channel_sender.py.

    `interactive` is the structure of a reply-button / list send
    (`Message.interactive`, what the staff console draws); `body` stays the
    history text either way.
    """
    async with async_session_factory() as session:
        async with session.begin():
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    wam_id=_extract_sent_wam_id(send_response),
                    body=body,
                    interactive=interactive,
                )
            )
            conversation = await session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.last_bot_message_at = datetime.now(UTC)

def _tenant_client(tenant: Tenant | None, waba_token: str | None) -> WhatsAppClient | None:
    """This tenant's WhatsApp client, or None when it cannot be built.

    The single fail-closed seam for every worker send (PROMPT_FIX_21). There is
    NO fallback to the global `META_*` env scaffold: without this tenant's own
    `phone_number_id` + decrypted token the message would go out from another
    clinic's WhatsApp number, so it does not go out at all. `for_tenant` emits
    `whatsapp_credential_missing` before raising, so the outcome is always
    visible in the logs; callers just degrade quietly.
    """
    if tenant is None:
        logger.error("whatsapp_credential_missing", tenant_id=None, missing="tenant")
        return None
    try:
        return WhatsAppClient.for_tenant(tenant, waba_token)
    except TenantWhatsAppCredentialMissing:
        return None

async def _send_simple_text(to: str, body: str, *, client: WhatsAppClient) -> None:
    """Send a single plain-text message, swallowing send errors (MVP: no retry).

    `client` is REQUIRED and must be tenant-scoped (`_tenant_client` /
    `WhatsAppClient.for_tenant`): there is no implicit global-scaffold default
    any more, so no caller can accidentally send from the wrong WABA.
    """
    try:
        await client.send_text_message(to=to, body=body)
    except Exception as exc:
        logger.error(
            "worker_simple_text_send_failed",
            error_type=type(exc).__name__,
            to_suffix=wa_suffix(to),
        )

def _extract_sent_wam_id(send_response: dict) -> str | None:
    """Pull the wamid from a Cloud API send response, tolerating bad shapes."""
    try:
        return send_response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None
