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
paths never diverge in behavior. Delivery goes through `_deliver_to_patient`,
which branches on `Patient.channel` — the single place in this router that
knows more than one channel exists.
"""

from datetime import datetime
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
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
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
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
    """The row's `interactive` blob in its wire shape, or None.

    Tolerant on purpose: a blob that no longer validates costs THAT message its
    controls (the console still draws its text `body`), never the whole thread.
    One bad row failing the entire response is how the console once lost every
    thread of a tenant (`ConversationRead.patient_wa_id` on a NULL wa_id).
    """
    if not message.interactive:
        return None
    try:
        return InteractiveRead.model_validate(message.interactive)
    except ValidationError:
        logger.warning("hub_message_interactive_invalid", message_id=str(message.id))
        return None


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


async def _deliver_to_patient(
    session: AsyncSession, tenant: Tenant, patient: Patient, body: str
) -> dict:
    """Deliver `body` to `patient` on the channel they actually reached the clinic on.

    Raises rather than pretending to send, so the caller never persists a Message for
    a delivery that did not happen.

    The two channels disagree about what "delivered" MEANS, and that is the whole
    reason this branches (the same asymmetry `services/channel_sender.py` documents
    for the bot path):

    - WhatsApp: the reply exists because Meta carried it. The network call IS the
      delivery and the `Message` row written afterwards is a history copy.
    - Brain-Message: there is no network leg and no phone number. The patient's
      console polls `GET /internal/brain-message/conversations/{external_id}/messages`,
      which returns every row on the conversation regardless of sender — so the row
      the caller writes next IS the delivery. `{}` is the honest send response for
      that: `_extract_wam_id` walks it to None, which is exactly right for a message
      Meta never saw.

    Deliberately NOT routed through `channel_sender.BrainMessageSender`, even though
    that class exists and covers the bot path. It hardcodes `sender=BOT` and stamps
    `conversation.last_bot_message_at`; using it here would label a human staff reply
    as the secretarIA in the patient's transcript and start the bot's clock on a
    human turn. It also sets `persists_outbound`, so this endpoint — which must write
    and RETURN its own row — would produce two. The row this router already writes is
    the correct one; all Brain-Message needs from delivery is to not make a call.

    Before this branch existed, a `brain_message` patient (`wa_id=None` by design,
    migration `c7e1a4b9d0f3`) sent `"to": null` to the Graph API, which answered 400
    and surfaced to the console as a 502 "Failed to deliver message via WhatsApp" —
    staff simply could not reply on the new channel.
    """
    if patient.channel == CHANNEL_BRAIN_MESSAGE:
        return {}

    waba_token = await get_waba_token(session, tenant.id)
    client = WhatsAppClient.for_tenant(tenant, waba_token)
    return await client.send_text_message(to=patient.wa_id, body=body)


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

    try:
        send_response = await _deliver_to_patient(session, tenant, patient, body.body)
    except TenantWhatsAppCredentialMissing:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "WhatsApp not configured for this tenant"
        ) from None
    except httpx.HTTPError:
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY, "Failed to deliver message via WhatsApp"
        ) from None

    # Same persistence shape as `smb_message_echoes`
    # (workers/tasks.py::_persist_human_echo): a human send always takes the
    # conversation over, whether it came from the WhatsApp app or from here.
    message = Message(
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        sender=MessageSender.HUMAN,
        wam_id=_extract_wam_id(send_response),
        body=body.body,
    )
    session.add(message)
    await HandoverManager(session).set_human_active(conversation)
    await session.commit()
    await session.refresh(message)

    logger.info(
        "hub_conversation_message_sent",
        tenant_id=str(tenant.id),
        conversation_id=str(conversation.id),
    )
    return _message_read_model(message)
