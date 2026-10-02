"""db - split out of workers/tasks.py (TASK-023)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import (
    Conversation,
    Message,
    Patient,
    ProcessedEvent,
    Tenant,
)


async def _event_already_processed(session: AsyncSession, event_id: str) -> bool:
    """True when `event_id` is already in the `processed_events` ledger."""
    found = await session.scalar(
        select(ProcessedEvent.id).where(ProcessedEvent.event_id == event_id)
    )
    return found is not None

async def _conversation_message_count(session: AsyncSession, conversation_id: UUID) -> int:
    """How many messages this conversation already carries, in the caller's txn.

    "Has this thread begun?" asked identically by the two functions that decide
    a first-contact turn — `_route_inbound_turn` and
    `_open_brain_message_conversation`. One spelling, because the day the two
    disagree about what counts as a started conversation is the day one of them
    greets a patient twice.
    """
    return (
        await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
    ) or 0

async def _get_or_create_conversation(
    session: AsyncSession,
    tenant: Tenant,
    patient: Patient,
) -> Conversation:
    """Return the conversation for (tenant, patient), creating it if needed."""
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.tenant_id == tenant.id,
            Conversation.patient_id == patient.id,
        )
    )
    if conversation is None:
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
    return conversation
