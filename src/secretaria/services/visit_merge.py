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
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Conversation, Message, MessageDirection, Patient
from secretaria.models.appointment import Appointment
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


@dataclass(frozen=True)
class EmptyVisitDiscard:
    status: Literal["discarded", "absent", "not_empty"]


async def discard_empty_visit(
    session: AsyncSession, tenant_id: UUID, external_id: str
) -> EmptyVisitDiscard:
    """Discard a visit that only opened the link — refusing anything the patient touched.

    TASK-042 retention (owner, 2026-10-09): brain-api deletes a Portal visit that nobody
    typed into after 24 h and asks this service first. brain-api cannot see the
    conversation, so the emptiness rule that matters lives HERE: a single message from the
    patient, any appointment (any status) or a live booking hold makes the visit
    `not_empty`, and nothing is deleted. What an empty visit has is the bot's own opening
    (and maybe its menu), which `discard_visit` removes with the same scope as a merge.

    The caller owns the commit, like `discard_visit`.
    """
    patient = await session.scalar(
        select(Patient).where(
            Patient.tenant_id == tenant_id,
            Patient.channel == CHANNEL_BRAIN_MESSAGE,
            Patient.external_id == external_id,
        )
    )
    if patient is None:
        return EmptyVisitDiscard(status="absent")
    conversation_ids = select(Conversation.id).where(Conversation.patient_id == patient.id)
    patient_spoke = await session.scalar(
        select(func.count())
        .select_from(Message)
        .where(
            Message.conversation_id.in_(conversation_ids),
            Message.direction == MessageDirection.INBOUND,
        )
    )
    # By patient AND by conversation: a booking for someone else (TASK-007) may carry only
    # the conversation that made it.
    booked = await session.scalar(
        select(func.count())
        .select_from(Appointment)
        .where(
            or_(
                Appointment.patient_id == patient.id,
                Appointment.conversation_id.in_(conversation_ids),
            )
        )
    )
    held = await session.scalar(
        select(func.count())
        .select_from(BookingHold)
        .where(
            BookingHold.conversation_id.in_(conversation_ids),
            BookingHold.expires_at > datetime.now(UTC),
        )
    )
    if patient_spoke or booked or held:
        logger.info(
            "visit_retention_refused",
            tenant_id=str(tenant_id),
            patient_spoke=bool(patient_spoke),
            booked=bool(booked),
            held=bool(held),
        )
        return EmptyVisitDiscard(status="not_empty")
    await discard_visit(session, tenant_id, external_id)
    return EmptyVisitDiscard(status="discarded")
