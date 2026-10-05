"""Throw away a Portal VISIT that was merged into an existing account.

A visit is the conversation a person has BEFORE the clinic knows who they are (greeting,
e-mail, code). When the address turns out to belong to an account that already has its own
conversation at this clinic, brain-api keeps the old identity and the visit's conversation
is left orphaned: nobody can reach it, and every signup used to leave one behind.

What is deleted is the conversation's own data: messages, the pseudonym map, booking holds,
the conversation and the patient row. What is NOT deleted: `consent_events` (keyed by the
visit's string handle, an audit trail on purpose) and `processed_events` (idempotence ledger).
No appointment guard (owner's decision, 2026-10-01): a visit that merges into an existing
account has by construction never booked - the code is asked BEFORE consent and booking.

The function does not commit: the caller owns the transaction. Children are deleted
explicitly because SQLite (tests) does not enforce ON DELETE CASCADE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Conversation, Message, Patient
from secretaria.models.booking_hold import BookingHold
from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE

logger = get_logger(__name__)


@dataclass(frozen=True)
class VisitDiscard:
    status: Literal["discarded", "absent"]
    messages: int = 0


async def discard_visit(
    session: AsyncSession, tenant_id: UUID, visit_external_id: str
) -> VisitDiscard:
    patient = await session.scalar(
        select(Patient).where(
            Patient.tenant_id == tenant_id,
            Patient.channel == CHANNEL_BRAIN_MESSAGE,
            Patient.external_id == visit_external_id,
        )
    )
    if patient is None:
        return VisitDiscard(status="absent")

    conversation_ids = list(
        await session.scalars(select(Conversation.id).where(Conversation.patient_id == patient.id))
    )
    message_count = 0
    if conversation_ids:
        message_count = (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.conversation_id.in_(conversation_ids))
            )
            or 0
        )
        for child in (Message, ConversationPiiTokenMap, BookingHold):
            await session.execute(delete(child).where(child.conversation_id.in_(conversation_ids)))
        await session.execute(delete(Conversation).where(Conversation.id.in_(conversation_ids)))
    await session.execute(delete(Patient).where(Patient.id == patient.id))
    logger.info("visit_discarded", tenant_id=str(tenant_id), messages=message_count)
    return VisitDiscard(status="discarded", messages=message_count)
