"""Doctor hub — per-conversation manual handover control + staff messaging.

GET  /tenants/me/conversations                     - list every conversation
                                                       for the authenticated
                                                       tenant, newest activity
                                                       first.
POST /tenants/me/conversations/{id}/handover        - flip a conversation
                                                       between BOT_ACTIVE and
                                                       HUMAN_ACTIVE.
GET  /tenants/me/conversations/{id}/messages        - full thread for one
                                                       conversation, oldest
                                                       first.
POST /tenants/me/conversations/{id}/messages        - staff sends a message
                                                       from the console.

The state flip itself is never reimplemented here — it goes through
`services/handover.py::HandoverManager`, which also stamps
`last_human_message_at` when a human takes over (that timestamp starts the
inactivity-timeout clock that eventually hands the conversation back to the
bot). The manager flushes but does not commit; this router owns the
transaction boundary, same as `api/hub/professionals.py`.

A staff send is recorded the same way `smb_message_echoes` already records a
human reply sent from the WhatsApp app
(`workers/tasks.py::_persist_human_echo`): `Message(direction=OUTBOUND,
sender=MessageSender.HUMAN)` + `HandoverManager.set_human_active`, so the two
paths never diverge in behavior. Delivery goes through `_staff_sender`, which
picks a `services/channel_sender.py::ChannelSender` by `Patient.channel` — the
single place in this router that knows more than one channel exists.
"""

from datetime import datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.conversation import Conversation, HandoverState
from secretaria.models.message import Message, MessageDirection, MessageSender
from secretaria.models.patient import Patient
from secretaria.schemas.conversation import (
    ConversationRead,
    HandoverUpdate,
    InteractiveRead,
    MessageRead,
    MessageSend,
    interactive_read_or_none,
)
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    RECORDED_MESSAGE_ID,
    BrainMessageSender,
    ChannelSender,
    sender_persists_outbound,
)
from secretaria.services.handover import HandoverManager
from secretaria.services.tenant_config import get_waba_token
from secretaria.services.whatsapp import TenantWhatsAppCredentialMissing, WhatsAppClient

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/conversations", tags=["hub-conversations"])


def _read_model(
    conversation: Conversation, patient: Patient, last_message_at: datetime | None
) -> ConversationRead:
    return ConversationRead(
        id=str(conversation.id),
        patient_wa_id=patient.wa_id,
        patient_name=patient.name,
        handover_state=conversation.handover_state.value,
        last_message_at=last_message_at,
    )


async def _get_conversation(
    session: AsyncSession, tenant: Tenant, conversation_id: str
) -> Conversation:
    try:
        conv_uuid = UUID(conversation_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found") from None
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.id == conv_uuid, Conversation.tenant_id == tenant.id
        )
    )
    if conversation is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found")
    return conversation


async def _last_message_at(session: AsyncSession, conversation_id: UUID) -> datetime | None:
    return await session.scalar(
        select(func.max(Message.created_at)).where(Message.conversation_id == conversation_id)
    )


def _interactive_read(message: Message) -> InteractiveRead | None:
    """The row's `interactive` blob in its wire shape, or None (tolerant)."""
    return interactive_read_or_none(message.interactive, message_id=message.id)


def _message_read_model(message: Message) -> MessageRead:
    return MessageRead(
        id=str(message.id),
        direction=message.direction.value,
        sender=message.sender.value,
        body=message.body,
        created_at=message.created_at,
        interactive=_interactive_read(message),
        interactive_reply_id=message.interactive_reply_id,
    )


def _extract_wam_id(send_response: dict) -> str | None:
    """Pull the wamid from a Cloud API send response, tolerating bad shapes.

    Mirrors `services/whatsapp.py::_extract_message_id` /
    `workers/tasks.py::_extract_sent_wam_id` — duplicated locally rather than
    imported so `api/` never reaches into another layer's private helper (the
    same 4-line parse is already duplicated once in this codebase for that
    reason).
    """
    try:
        return send_response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


async def _staff_sender(
    session: AsyncSession, tenant: Tenant, conversation: Conversation, patient: Patient
) -> tuple[ChannelSender, str | None]:
    """The sender that delivers a staff reply to `patient`, and the address it takes.

    The staff-side twin of `workers/tasks.py::_reply_sender`, keyed on
    `Patient.channel` because this path holds the patient row rather than a
    `_ReplyContext`. Like it, anything that is not Brain-Message is WhatsApp —
    the column holds only those two values today.

    The two channels disagree about what "delivered" MEANS (the asymmetry
    `services/channel_sender.py` documents as `persists_outbound`):

    - WhatsApp: exactly what this endpoint always did — the tenant's own client,
      failing closed on a missing credential (PROMPT_FIX_21), addressed to
      `wa_id`. Meta carrying the message IS the delivery; the caller writes the
      history row afterwards, from the send response.
    - Brain-Message: `BrainMessageSender` authored as HUMAN, writing into THIS
      request's session. No network leg and no phone number: the patient's
      portal polls `/internal/brain-message/conversations/{external_id}/messages`,
      so the row the sender writes IS the delivery — and because it is only
      flushed, it commits together with the caller's handover flip, or not at
      all (a sender committing on its own connection left a window where the
      reply was delivered but the bot still in charge). `external_id` is passed
      for symmetry; the sender addresses by conversation.

    History: this endpoint first built a `WhatsAppClient` unconditionally, and a
    `brain_message` patient (`wa_id=None` by design, migration `c7e1a4b9d0f3`)
    sent `"to": null` to the Graph API — Meta answered 400, surfaced to the
    console as a 502 (reproduced live 2026-09-09). `5b8bfdf` stopped that by
    skipping the call and writing the row in this router. Routing through the
    channel's own sender instead leaves ONE writer for the Brain-Message row
    format, bot and staff alike, rather than two to keep in step.
    """
    if patient.channel == CHANNEL_BRAIN_MESSAGE:
        sender = BrainMessageSender(
            conversation_id=conversation.id,
            session=session,
            author=MessageSender.HUMAN,
        )
        return sender, patient.external_id

    waba_token = await get_waba_token(session, tenant.id)
    return WhatsAppClient.for_tenant(tenant, waba_token), patient.wa_id


@router.get("", response_model=list[ConversationRead])
async def list_conversations(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[ConversationRead]:
    # One query: join Conversation -> Patient, LEFT OUTER join a grouped
    # subquery for the most recent message per conversation (regardless of
    # who sent it). No pagination — deliberately minimal for the dashboard.
    last_msg_sub = (
        select(Message.conversation_id, func.max(Message.created_at).label("last_message_at"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    stmt = (
        select(Conversation, Patient, last_msg_sub.c.last_message_at)
        .join(Patient, Patient.id == Conversation.patient_id)
        .outerjoin(last_msg_sub, last_msg_sub.c.conversation_id == Conversation.id)
        .where(Conversation.tenant_id == tenant.id)
        # NULLS LAST is native to SQLite >= 3.30 and Postgres alike, so this
        # stays portable across the sqlite test DB and the real Postgres DB.
        .order_by(last_msg_sub.c.last_message_at.desc().nulls_last())
    )
    rows = (await session.execute(stmt)).all()
    return [
        _read_model(conversation, patient, last_message_at)
        for conversation, patient, last_message_at in rows
    ]


@router.post("/{conversation_id}/handover", response_model=ConversationRead)
async def update_handover(
    conversation_id: str,
    body: HandoverUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ConversationRead:
    # Same message for "doesn't exist" and "belongs to another tenant" - don't
    # leak existence across tenants.
    conversation = await _get_conversation(session, tenant, conversation_id)

    manager = HandoverManager(session)
    if body.state == HandoverState.HUMAN_ACTIVE:
        await manager.set_human_active(conversation)
    else:
        await manager.set_bot_active(conversation)
    await session.commit()
    await session.refresh(conversation)

    patient = await session.get(Patient, conversation.patient_id)
    last_message_at = await _last_message_at(session, conversation.id)
    logger.info(
        "hub_conversation_handover_updated",
        tenant_id=str(tenant.id),
        conversation_id=str(conversation.id),
        state=conversation.handover_state.value,
    )
    return _read_model(conversation, patient, last_message_at)


@router.get("/{conversation_id}/messages", response_model=list[MessageRead])
async def list_messages(
    conversation_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[MessageRead]:
    # Same tenant-scoped 404 as every other conversation lookup in this router.
    conversation = await _get_conversation(session, tenant, conversation_id)
    rows = (
        await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation.id)
            .order_by(Message.created_at.asc())
        )
    ).all()
    return [_message_read_model(message) for message in rows]


@router.post("/{conversation_id}/messages", response_model=MessageRead)
async def send_message(
    conversation_id: str,
    body: MessageSend,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> MessageRead:
    conversation = await _get_conversation(session, tenant, conversation_id)
    patient = await session.get(Patient, conversation.patient_id)

    # Both 502s come only from the WhatsApp branch — an upstream the clinic
    # depends on. A Brain-Message send is a write into this request's own
    # transaction: if it, or anything after it up to the commit below, fails,
    # the error propagates as the 500 it is and the whole unit of work rolls
    # back — no reply delivered, no takeover — so a retry cannot duplicate it.
    try:
        sender, to = await _staff_sender(session, tenant, conversation, patient)
        send_response = await sender.send_text_message(to=to, body=body.body)
    except TenantWhatsAppCredentialMissing:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "WhatsApp not configured for this tenant"
        ) from None
    except httpx.HTTPError:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Failed to deliver message via WhatsApp"
        ) from None

    if sender_persists_outbound(sender):
        # Brain-Message: the sender already wrote the row into this session, and
        # that row IS the delivery (the commit below publishes it together with
        # the takeover). Answer with it; writing here would be a second.
        message = await session.get(Message, UUID(send_response[RECORDED_MESSAGE_ID]))
    else:
        # WhatsApp: the history copy of what Meta carried, in the same shape as
        # `smb_message_echoes` (workers/tasks.py::_persist_human_echo).
        message = Message(
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND,
            sender=MessageSender.HUMAN,
            wam_id=_extract_wam_id(send_response),
            body=body.body,
        )
        session.add(message)
    # A human send always takes the conversation over, whichever channel carried
    # it and whether it came from the WhatsApp app or from here.
    await HandoverManager(session).set_human_active(conversation)
    await session.commit()
    await session.refresh(message)

    logger.info(
        "hub_conversation_message_sent",
        tenant_id=str(tenant.id),
        conversation_id=str(conversation.id),
        channel=patient.channel,
    )
    return _message_read_model(message)
