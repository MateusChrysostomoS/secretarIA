"""Doctor hub — per-conversation manual handover control.

GET  /tenants/me/conversations                     - list every conversation
                                                       for the authenticated
                                                       tenant, newest activity
                                                       first.
POST /tenants/me/conversations/{id}/handover        - flip a conversation
                                                       between BOT_ACTIVE and
                                                       HUMAN_ACTIVE.

The state flip itself is never reimplemented here — it goes through
`services/handover.py::HandoverManager`, which also stamps
`last_human_message_at` when a human takes over (that timestamp starts the
inactivity-timeout clock that eventually hands the conversation back to the
bot). The manager flushes but does not commit; this router owns the
transaction boundary, same as `api/hub/professionals.py`.
"""

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.conversation import Conversation, HandoverState
from secretaria.models.message import Message
from secretaria.models.patient import Patient
from secretaria.schemas.conversation import ConversationRead, HandoverUpdate
from secretaria.services.handover import HandoverManager

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
