"""Channel-neutral delivery of the money messages a patient receives (spec section 11).

The `after` flow used to speak only WhatsApp: the Pix request, "sinal recebido" and "expirou"
went through `WhatsAppClient` to `patient.wa_id`. A Portal patient has `wa_id = None` by design,
so the request failed silently and, in `before` timing, would have held the slot until it
expired. Every payment message now goes through ONE resolver, `resolve_sender`, which is the
payments twin of `workers/tasks.py::_reply_sender` and `api/hub/conversations.py::_staff_sender`
(skill `channel-aware-dispatch`): the ONLY place in the payments code that branches on the
patient's channel.

* Brain-Message: `BrainMessageSender` (authored as the bot, own short transaction). The row IS
  the delivery, so the caller never writes a second one.
* WhatsApp: the clinic's own client (fail closed without a credential), addressed to `wa_id`.
  Meta carrying the message is the delivery; the history row is written HERE after a
  successful send, exactly like the reply path does.

A failure is raised (`DeliveryError`), never swallowed: in `before` timing a Pix nobody received
must cancel the charge and free the hold (`booking_charge.py`), and every other caller decides
for itself whether a lost notice is fatal.

`whatsapp_cls` lets `deposit_lifecycle` keep handing its own (test-patchable) `WhatsAppClient`,
so the `after` notices stay byte for byte what they were for WhatsApp.
"""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    BrainMessageSender,
    ChannelSender,
    interactive_history_body,
    sender_persists_outbound,
)
from secretaria.services.whatsapp import (
    TenantWhatsAppCredentialMissing,
    WhatsAppClient,
    interactive_buttons_record,
)

logger = get_logger(__name__)


class DeliveryError(Exception):
    """A payment message could not be delivered. `code` is symbolic (never a phone, a body or
    a provider response)."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"payment message not delivered: {code}")


@dataclass(frozen=True)
class Text:
    body: str


@dataclass(frozen=True)
class Buttons:
    body: str
    buttons: tuple[tuple[str, str], ...]  # (id, label)


Outgoing = Text | Buttons


async def conversation_id_for(
    session: AsyncSession, tenant_id: UUID, patient_id: UUID
) -> UUID | None:
    """The (single) conversation of a patient in this clinic, or None."""
    return await session.scalar(
        select(Conversation.id).where(
            Conversation.tenant_id == tenant_id, Conversation.patient_id == patient_id
        )
    )


async def resolve_sender(
    session: AsyncSession,
    tenant: Tenant,
    patient: Patient,
    *,
    conversation_id: UUID | None,
    waba_token: str | None = None,
    whatsapp_cls=None,
) -> tuple[ChannelSender, str]:
    """The sender that reaches `patient` on THEIR channel, and the address it takes."""
    if patient.channel == CHANNEL_BRAIN_MESSAGE:
        if conversation_id is None:
            raise DeliveryError("no_conversation")
        return (
            BrainMessageSender(
                conversation_id=conversation_id, session_factory=async_session_factory
            ),
            patient.external_id or "",
        )
    if not patient.wa_id:
        raise DeliveryError("no_address")
    token = waba_token if waba_token is not None else await cfg.get_waba_token(session, tenant.id)
    try:
        client = (whatsapp_cls or WhatsAppClient).for_tenant(tenant, token)
    except TenantWhatsAppCredentialMissing as exc:
        raise DeliveryError("no_whatsapp_credential") from exc
    return client, patient.wa_id


def _wam_id(response: dict) -> str | None:
    try:
        return response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


async def _record_whatsapp_history(
    conversation_id: UUID | None, body: str, response: dict, interactive: dict | None
) -> None:
    """The history copy of a message WhatsApp already delivered (best-effort)."""
    if conversation_id is None:
        return
    try:
        async with async_session_factory() as session:
            async with session.begin():
                session.add(
                    Message(
                        conversation_id=conversation_id,
                        direction=MessageDirection.OUTBOUND,
                        sender=MessageSender.BOT,
                        wam_id=_wam_id(response),
                        body=body,
                        interactive=interactive,
                    )
                )
    except Exception as exc:  # noqa: BLE001 - never undo a delivered message over its history
        logger.warning("payment_message_history_failed", error_type=type(exc).__name__)


async def deliver(
    session: AsyncSession,
    tenant: Tenant,
    patient: Patient,
    messages: list[Outgoing],
    *,
    conversation_id: UUID | None = None,
    waba_token: str | None = None,
    whatsapp_cls=None,
) -> None:
    """Send `messages`, in order, on the patient's own channel. Raises `DeliveryError`.

    A transport failure (`httpx` error, a database error on the Portal row) is normalised to
    `DeliveryError("send_failed")` so callers handle one exception type.
    """
    if conversation_id is None:
        conversation_id = await conversation_id_for(session, tenant.id, patient.id)
    sender, to = await resolve_sender(
        session,
        tenant,
        patient,
        conversation_id=conversation_id,
        waba_token=waba_token,
        whatsapp_cls=whatsapp_cls,
    )
    persisted = sender_persists_outbound(sender)
    for message in messages:
        try:
            if isinstance(message, Buttons):
                response = await sender.send_buttons(
                    to=to, body=message.body, buttons=list(message.buttons)
                )
                history = interactive_history_body(
                    message.body, [label for _id, label in message.buttons]
                )
                interactive = interactive_buttons_record(message.body, list(message.buttons))
            else:
                response = await sender.send_text_message(to=to, body=message.body)
                history, interactive = message.body, None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "payment_message_send_failed",
                error_type=type(exc).__name__,
                tenant_id=str(tenant.id),
            )
            raise DeliveryError("send_failed") from exc
        if not persisted:
            await _record_whatsapp_history(conversation_id, history, response, interactive)
