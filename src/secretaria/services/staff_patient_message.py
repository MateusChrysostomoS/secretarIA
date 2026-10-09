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
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.core.whatsapp_limits import (
    MAX_INTERACTIVE_BODY_CHARS,
    MAX_TEXT_MESSAGE_CHARS,
    truncate_plain,
)
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
    interactive_history_body,
)
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.reminder_text import portal_conversation_link
from secretaria.services.tenant_config import get_waba_token
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import WhatsAppClient, interactive_buttons_record

logger = get_logger(__name__)

DELIVERY_WHATSAPP_TEXT = "whatsapp_text"
DELIVERY_WHATSAPP_TEMPLATE = "whatsapp_template"
DELIVERY_PORTAL_CHAT = "portal_chat"

EMAIL_SENT = "sent"
EMAIL_NO_ADDRESS = "no_email"
EMAIL_NOT_SENT = "not_sent"

# What happened to a clinic-action notice (TASK-032 R4 vocabulary + R7 `whatsapp_sent`).
# The hub sends these to the front verbatim as `patient_notice`.
NOTICE_WHATSAPP_QUEUED = "whatsapp_queued"
NOTICE_WHATSAPP_SENT = "whatsapp_sent"
NOTICE_WHATSAPP_OUTSIDE_WINDOW = "whatsapp_outside_window"
NOTICE_PORTAL_CHAT = "portal_chat"
NOTICE_PORTAL_CHAT_EMAIL = "portal_chat_email"
NOTICE_NO_CHANNEL = "no_channel"
NOTICE_QUEUE_UNAVAILABLE = "queue_unavailable"
NOTICE_FAILED = "notice_failed"
DELIVERED_NOTICES = frozenset(
    {NOTICE_WHATSAPP_QUEUED, NOTICE_WHATSAPP_SENT, NOTICE_PORTAL_CHAT, NOTICE_PORTAL_CHAT_EMAIL}
)
# A template parameter is one line and Meta caps the whole body at 1024 characters
# including the template's own words (same budget as reminder_text.SINGLE_LINE_MAX_CHARS).
TEMPLATE_LINE_MAX_CHARS = 900
LONG_NOTICE_CARD = "Confira os detalhes da consulta na mensagem acima. Você está ciente?"

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
        tenant_id, appointment_id = tenant.id, appointment.id  # rollback expires ORM rows
        try:
            response = await sender.send_text_message(to=patient.external_id or "", body=text)
            await session.commit()
        except Exception as exc:
            await session.rollback()
            logger.error(
                "staff_message_failed",
                tenant_id=str(tenant_id),
                appointment_id=str(appointment_id),
                delivery=DELIVERY_PORTAL_CHAT,
                error_type=type(exc).__name__,
            )
            raise DeliveryFailedError("the Portal message could not be recorded") from exc
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
                event_id=(
                    f"staffmsg:{message_id or appointment.id}:"
                    f"{_wam_id(response) or uuid4().hex}"
                ),
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


# --- TASK-032 R7: every clinic action on the agenda tells the patient ---------------


@dataclass(frozen=True)
class NoticeResult:
    """One `NOTICE_*` code, the free `wa.me` link when the window stopped it, the row id."""

    code: str
    whatsapp_link: str | None = None
    message_id: UUID | None = None

    @property
    def delivered(self) -> bool:
        return self.code in DELIVERED_NOTICES


def paid_notice_authorised(tenant, requested: bool) -> bool:
    """Spec 2026-10-09 §3: the request's own yes, or the clinic's standing one."""
    return bool(requested) or getattr(tenant, "paid_notices_auto_approved", False) is True


async def send_clinic_notice(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    body: str,
    buttons: list[tuple[str, str]] | None = None,
    allow_paid: bool,
    usage_key: str,
    now: datetime | None = None,
) -> NoticeResult:
    """Tell the appointment's patient what the clinic did. Never raises.

    * Portal: the text (or the button card) is written in the conversation, authored
      by the clinic (`HUMAN`, like R4), then the generic e-mail nudge. Commits.
    * WhatsApp inside 24 h: free text or the button card; the history row is recorded.
    * WhatsApp outside 24 h: only with `allow_paid`, and then the one-variable
      `REMINDER_TEMPLATE_NAME` with the body flattened to one line - buttons cannot
      ride along until Meta approves a button template (docs/LEMBRETES_MODELOS_META.md);
      a usage event bills it (after the send, fail-open). Without `allow_paid` nothing
      is sent and the free `wa.me` link comes back.

    The caller has proved the appointment belongs to `tenant`. Rows the caller left
    pending in `session` are committed with the delivery.
    """
    appointment_id = appointment.id  # before a rollback can expire the row
    try:
        if patient is None or patient.tenant_id != tenant.id:
            return NoticeResult(NOTICE_NO_CHANNEL)
        if patient.channel == CHANNEL_BRAIN_MESSAGE:
            return await _clinic_notice_portal(session, tenant, appointment, patient, body, buttons)
        return await _clinic_notice_whatsapp(
            session,
            tenant,
            appointment,
            patient,
            body,
            buttons,
            allow_paid=allow_paid,
            usage_key=usage_key,
            now=now,
        )
    except Exception as exc:  # defensive: the clinic's action already stands
        logger.error(
            "clinic_notice_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return NoticeResult(NOTICE_FAILED)


async def _clinic_notice_portal(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    body: str,
    buttons: list[tuple[str, str]] | None,
) -> NoticeResult:
    appointment_id = appointment.id
    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    if conversation_id is None:
        return NoticeResult(NOTICE_NO_CHANNEL)
    sender = BrainMessageSender(
        conversation_id=conversation_id, session=session, author=MessageSender.HUMAN
    )
    to = patient.external_id or ""
    try:
        if buttons:
            card_body = body
            if len(body) > MAX_INTERACTIVE_BODY_CHARS:
                # Portal details have no Meta limit; its shared card builder does.
                await sender.send_text_message(to=to, body=body)
                card_body = LONG_NOTICE_CARD
            response = await sender.send_buttons(to, card_body, buttons)
        else:
            response = await sender.send_text_message(to=to, body=body)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.error(
            "clinic_notice_failed",
            appointment_id=str(appointment_id),
            delivery=DELIVERY_PORTAL_CHAT,
            error_type=type(exc).__name__,
        )
        return NoticeResult(NOTICE_FAILED)
    nudge = await nudge_portal_patient(tenant, patient)
    code = NOTICE_PORTAL_CHAT_EMAIL if nudge == EMAIL_SENT else NOTICE_PORTAL_CHAT
    return NoticeResult(code, message_id=UUID(response[RECORDED_MESSAGE_ID]))


async def _clinic_notice_whatsapp(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    body: str,
    buttons: list[tuple[str, str]] | None,
    *,
    allow_paid: bool,
    usage_key: str,
    now: datetime | None,
) -> NoticeResult:
    tenant_id, appointment_id = tenant.id, appointment.id
    to = patient.wa_id or appointment.phone
    if not to:
        return NoticeResult(NOTICE_NO_CHANNEL)
    last_inbound = await cancellation_notice.last_inbound_at(session, tenant_id, patient.id)
    inside = cancellation_notice.is_inside_window(last_inbound, now=now)
    if not inside and not allow_paid:
        logger.info(
            "clinic_notice_not_sent",
            appointment_id=str(appointment_id),
            reason="outside_window_not_authorised",
        )
        return NoticeResult(
            NOTICE_WHATSAPP_OUTSIDE_WINDOW,
            whatsapp_link=cancellation_notice.whatsapp_deep_link(to),
        )

    waba_token = await get_waba_token(session, tenant_id)
    details: list[tuple[str, str | None]] = []
    try:
        client = WhatsAppClient.for_tenant(tenant, waba_token)
        if inside and buttons and len(body) > MAX_INTERACTIVE_BODY_CHARS:
            for offset in range(0, len(body), MAX_TEXT_MESSAGE_CHARS):
                chunk = body[offset : offset + MAX_TEXT_MESSAGE_CHARS]
                sent_detail = await client.send_text_message(to=to, body=chunk)
                details.append((chunk, _wam_id(sent_detail)))
            response = await client.send_buttons(to, LONG_NOTICE_CARD, buttons)
            history = interactive_history_body(LONG_NOTICE_CARD, [label for _, label in buttons])
            interactive = interactive_buttons_record(LONG_NOTICE_CARD, buttons)
        elif inside and buttons:
            response = await client.send_buttons(to, body, buttons)
            history = interactive_history_body(body, [label for _, label in buttons])
            interactive = interactive_buttons_record(body, buttons)
        elif inside:
            chunks = [
                body[offset : offset + MAX_TEXT_MESSAGE_CHARS]
                for offset in range(0, len(body), MAX_TEXT_MESSAGE_CHARS)
            ] or [""]
            for chunk in chunks[:-1]:
                sent_detail = await client.send_text_message(to=to, body=chunk)
                details.append((chunk, _wam_id(sent_detail)))
            response = await client.send_text_message(to=to, body=chunks[-1])
            history, interactive = chunks[-1], None
        else:
            line = truncate_plain(_one_line(body), TEMPLATE_LINE_MAX_CHARS)
            response = await client.send_template(
                to=to,
                template=get_settings().REMINDER_TEMPLATE_NAME,
                lang=cancellation_notice.meta_language_code(tenant.language),
                variables=[line],
            )
            history, interactive = line, None
    except Exception as exc:
        logger.error(
            "clinic_notice_failed",
            appointment_id=str(appointment_id),
            inside_window=inside,
            error_type=type(exc).__name__,
        )
        return NoticeResult(NOTICE_FAILED)

    wam_id = _wam_id(response)
    message_id = None
    try:
        conversation_id = await conversation_id_for(session, tenant_id, appointment, patient)
        if conversation_id is not None:
            for detail, detail_wam_id in details:
                session.add(Message(
                    conversation_id=conversation_id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.HUMAN,
                    wam_id=detail_wam_id,
                    body=detail,
                ))
            message = Message(
                conversation_id=conversation_id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.HUMAN,
                wam_id=wam_id,
                body=history,
                interactive=interactive,
            )
            session.add(message)
            await session.flush()
            message_id = message.id
        await session.commit()
    except Exception as exc:  # the message already left: never resend it
        await session.rollback()
        message_id = None
        logger.warning(
            "clinic_notice_history_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
    if not inside:
        try:
            await emit_usage_event(
                tenant_id=str(tenant_id),
                feature="reminders",
                amount=1,
                event_id=f"{usage_key}:{wam_id or uuid4().hex}",
            )
        except Exception as exc:
            logger.warning("usage_emit_failed", error_type=type(exc).__name__)
    return NoticeResult(NOTICE_WHATSAPP_SENT, message_id=message_id)
