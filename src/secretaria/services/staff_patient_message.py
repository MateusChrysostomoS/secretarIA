"""Staff free text to a patient, on the patient's own channel (TASK-032 R4, spec 4.4).

The clinic sees a red appointment and wants to ask the patient "did you get our
reminder?". Where that text goes depends on the channel, and the two channels
disagree about what "sent" means (services/channel_sender.py documents the
asymmetry as `persists_outbound`):

* **WhatsApp** - inside Meta's 24 h window a free text is allowed and free;
  outside it WhatsApp accepts only an approved template and bills it. The clinic
  must say yes to that cost (`allow_paid`, shown with the price in the hub) -
  same rule as `send_cancellation_notice`. The template is the clinic's existing
  one-variable reminder template (`REMINDER_TEMPLATE_NAME`), the text flattened to
  one line (Meta rejects newlines/tabs in a parameter).
* **Portal (Brain-Message)** - there is no window and no network leg: writing the
  `Message` row IS the delivery. A Portal patient may not be looking at the
  console, so a generic e-mail nudge (never the text itself, which may be
  clinical) points them back to the conversation; no e-mail on file is fine.

Deliberate: this does NOT take the conversation over from the bot (the staff
console's `send_message` does). A nudge about a reminder should not silence the
secretaria for the patient's answer.

Never logs a phone number, an e-mail address or the message text.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services import cancellation_notice
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    RECORDED_MESSAGE_ID,
    BrainMessageSender,
)
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.reminder_text import portal_conversation_link
from secretaria.services.tenant_config import get_waba_token
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

DELIVERY_WHATSAPP_TEXT = "whatsapp_text"
DELIVERY_WHATSAPP_TEMPLATE = "whatsapp_template"
DELIVERY_PORTAL_CHAT = "portal_chat"

EMAIL_SENT = "sent"
EMAIL_NO_ADDRESS = "no_email"
EMAIL_NOT_SENT = "not_sent"

_PORTAL_LINK_FALLBACK = "Acesse o portal da clínica e abra a sua conversa."


class StaffMessageError(Exception):
    """Base of the typed failures the hub maps to HTTP errors."""

    code = "staff_message_error"


class NoChannelError(StaffMessageError):
    code = "no_channel"


class OutsideWindowError(StaffMessageError):
    code = "outside_window_not_authorised"

    def __init__(self, whatsapp_link: str | None) -> None:
        super().__init__("outside the 24h window and the paid template was not authorised")
        self.whatsapp_link = whatsapp_link


class DeliveryFailedError(StaffMessageError):
    code = "delivery_failed"


@dataclass(frozen=True)
class StaffMessageResult:
    delivery: str
    email_nudge: str | None
    message_id: UUID | None


async def conversation_id_for(
    session: AsyncSession,
    tenant_id: UUID,
    appointment: Appointment,
    patient: Patient | None,
) -> UUID | None:
    """The conversation a message to this appointment's patient belongs to.

    Tenant-scoped on every branch: the appointment's own `conversation_id` is
    used only when that conversation belongs to the same clinic; otherwise (or
    when it is unset) the patient's most recent conversation in the clinic.
    """
    if appointment.conversation_id is not None:
        own = await session.scalar(
            select(Conversation.id).where(
                Conversation.id == appointment.conversation_id,
                Conversation.tenant_id == tenant_id,
                Conversation.patient_id == appointment.patient_id,
            )
        )
        if own is not None:
            return own
    if patient is None:
        return None
    return await session.scalar(
        select(Conversation.id)
        .where(Conversation.tenant_id == tenant_id, Conversation.patient_id == patient.id)
        .order_by(Conversation.created_at.desc())
        .limit(1)
    )


async def nudge_portal_patient(tenant: Tenant, patient: Patient) -> str:
    """E-mail a Portal patient that the clinic wrote to them. Never raises.

    The body is generic on purpose - it never carries the clinic's text.
    """
    address = (patient.email or "").strip()
    if not address:
        return EMAIL_NO_ADDRESS
    link = portal_conversation_link(tenant.id)
    outcome = await send_transactional_email_result(
        address,
        "clinic_message_patient",
        {"clinic_name": tenant.clinic_name, "link_line": link or _PORTAL_LINK_FALLBACK},
    )
    return EMAIL_SENT if outcome is EmailOutcome.SENT else EMAIL_NOT_SENT


def _one_line(text: str) -> str:
    """A WhatsApp template parameter: no newline, tab or run of spaces (Meta's rule)."""
    return " ".join(text.split())


def _wam_id(response: dict) -> str | None:
    try:
        return response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


async def send_staff_message(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    text: str,
    *,
    allow_paid: bool = False,
    now: datetime | None = None,
) -> StaffMessageResult:
    """Deliver `text` to the appointment's patient; commits `session` on success.

    Raises `NoChannelError` (no patient / no number / Portal patient with no
    conversation), `OutsideWindowError` (WhatsApp, outside 24 h, not authorised)
    or `DeliveryFailedError` (the channel refused). Nothing is recorded on error.
    The caller has already proved the appointment belongs to `tenant`.
    """
    if patient is None or patient.tenant_id != tenant.id:
        raise NoChannelError("no patient on this appointment")

    if patient.channel == CHANNEL_BRAIN_MESSAGE:
        conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
        if conversation_id is None:
            raise NoChannelError("the Portal patient has no conversation yet")
        sender = BrainMessageSender(
            conversation_id=conversation_id, session=session, author=MessageSender.HUMAN
        )
        response = await sender.send_text_message(to=patient.external_id or "", body=text)
        await session.commit()
        nudge = await nudge_portal_patient(tenant, patient)
        logger.info(
            "staff_message_sent",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
            delivery=DELIVERY_PORTAL_CHAT,
            email_nudge=nudge,
        )
        return StaffMessageResult(DELIVERY_PORTAL_CHAT, nudge, UUID(response[RECORDED_MESSAGE_ID]))

    to = patient.wa_id or appointment.phone
    if not to:
        raise NoChannelError("the patient has no WhatsApp number")
    last_inbound = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
    inside = cancellation_notice.is_inside_window(last_inbound, now=now)
    if not inside and not allow_paid:
        raise OutsideWindowError(cancellation_notice.whatsapp_deep_link(to))

    waba_token = await get_waba_token(session, tenant.id)
    try:
        client = WhatsAppClient.for_tenant(tenant, waba_token)
        if inside:
            response = await client.send_text_message(to=to, body=text)
        else:
            response = await client.send_template(
                to=to,
                template=get_settings().REMINDER_TEMPLATE_NAME,
                lang=cancellation_notice.meta_language_code(tenant.language),
                variables=[_one_line(text)],
            )
    except Exception as exc:
        logger.error(
            "staff_message_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
            inside_window=inside,
            error_type=type(exc).__name__,
        )
        raise DeliveryFailedError("the channel refused the message") from exc

    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    message_id = None
    if conversation_id is not None:
        message = Message(
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            sender=MessageSender.HUMAN,
            wam_id=_wam_id(response),
            body=text,
        )
        session.add(message)
        await session.flush()
        message_id = message.id
    await session.commit()

    if not inside:
        # After the commit and fail-open: the message already left, so a metering
        # hiccup must never turn into a second billed send (same rule as
        # workers/whatsapp/notifications.py::_emit_cancellation_usage).
        try:
            await emit_usage_event(
                tenant_id=str(tenant.id),
                feature="reminders",
                amount=1,
                event_id=f"staffmsg:{message_id or appointment.id}:{_wam_id(response) or 'x'}",
            )
        except Exception as exc:
            logger.warning("usage_emit_failed", error_type=type(exc).__name__)
    logger.info(
        "staff_message_sent",
        tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
        delivery=DELIVERY_WHATSAPP_TEXT if inside else DELIVERY_WHATSAPP_TEMPLATE,
    )
    return StaffMessageResult(
        DELIVERY_WHATSAPP_TEXT if inside else DELIVERY_WHATSAPP_TEMPLATE, None, message_id
    )
