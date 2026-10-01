"""Operational handoff mail with durable per-occurrence, per-recipient claims.

The active ProcessedEvent pointer rotates in the same locked transaction as a
BOT_ACTIVE to HUMAN_ACTIVE transition. It survives staff activity and clock
changes. Mail happens after commit; successful recipient claims survive replay.
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, HandoverState, ProcessedEvent
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.handover import HandoverManager

logger = get_logger(__name__)

TEMPLATE_ID = "human_handoff_alert"

_REASON_TEXT = {
    "patient_requested_human": "O paciente pediu para falar com uma pessoa.",
    "clinical_sensitive": "A conversa trata de um assunto que exige avaliação humana.",
    "could_not_help": "A secretária virtual não conseguiu resolver o pedido do paciente.",
}

_ATTEMPTS = 3


def _retry_sleep(attempt: int) -> float:
    """Seconds to wait before the next attempt (patched to 0 in tests)."""
    return float(2**attempt)


def _active_key(conversation_id: UUID) -> str:
    return f"humanhandoff-active:{conversation_id}"


async def activate_human_handoff(
    session, conversation: Conversation, *, manager=None
) -> UUID | None:
    """Lock and activate; return a new durable occurrence only for a real transition.

    Caller owns commit. Repeated staff sends still refresh the inactivity clock,
    but do not rotate the occurrence or generate another notification.
    """
    conversation = await session.scalar(
        select(Conversation).where(Conversation.id == conversation.id)
        .with_for_update().execution_options(populate_existing=True)
    )
    occurrence_id = None
    if conversation.handover_state == HandoverState.BOT_ACTIVE:
        pointer = await session.scalar(
            select(ProcessedEvent).where(ProcessedEvent.event_id == _active_key(conversation.id))
        )
        occurrence_id = uuid4()
        if pointer is None:
            session.add(ProcessedEvent(id=occurrence_id, event_id=_active_key(conversation.id)))
        else:
            pointer.id = occurrence_id
            pointer.processed_at = datetime.now(UTC)
    await (manager or HandoverManager(session)).set_human_active(conversation)
    return occurrence_id


async def _current_occurrence(tenant_id: UUID, conversation_id: UUID) -> UUID:
    async with async_session_factory() as session:
        current = await session.scalar(
            select(ProcessedEvent.id).where(ProcessedEvent.event_id == _active_key(conversation_id))
        )
    # Existing human conversations predate the pointer; stable compatibility key.
    return current or uuid5(NAMESPACE_URL, f"humanhandoff-legacy:{tenant_id}:{conversation_id}")


def _ledger_key(occurrence_id: UUID) -> str:
    return f"humanhandoff:{occurrence_id}"


async def _claim(key: str) -> bool:
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if await session.scalar(
                    select(ProcessedEvent.id).where(ProcessedEvent.event_id == key)
                ):
                    return False
                session.add(ProcessedEvent(event_id=key))
        except IntegrityError:
            return False
    return True


async def _release(key: str) -> None:
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning("human_handoff_release_failed", error_type=type(exc).__name__)


async def notify_human_handoff(
    *, tenant, conversation_id, professional_id, reason, occurrence_id=None
) -> int:
    """Send a best-effort operational alert, deduplicated per recipient per occurrence."""
    sent = 0
    try:
        recipients = set()
        clinic_email = (getattr(tenant, "contact_email", None) or "").strip().lower()
        if clinic_email:
            recipients.add(clinic_email)
        if professional_id is not None:
            try:
                emails = await fetch_professional_emails(tenant.id)
                doctor_email = (emails or {}).get(str(professional_id))
                if doctor_email and doctor_email.strip():
                    recipients.add(doctor_email.strip().lower())
            except Exception as exc:
                logger.warning("human_handoff_contact_lookup_failed", error_type=type(exc).__name__)
        variables = {
            "tenant_id": str(tenant.id),
            "conversation_id": str(conversation_id),
            "reason": _REASON_TEXT.get(reason, _REASON_TEXT["could_not_help"]),
        }
        occurrence_id = occurrence_id or await _current_occurrence(tenant.id, conversation_id)
        window_key = _ledger_key(occurrence_id)
        for to in sorted(recipients):
            digest = hashlib.sha256(f"{tenant.id}:{to}".encode()).hexdigest()[:32]
            key = f"{window_key}:{digest}"
            if not await _claim(key):
                continue
            delivered = False
            try:
                for attempt in range(_ATTEMPTS):
                    try:
                        outcome = await send_transactional_email_result(
                            to=to, template=TEMPLATE_ID, variables=variables
                        )
                    except Exception:
                        outcome = EmailOutcome.SEND_FAILED
                    if outcome is EmailOutcome.SENT:
                        delivered = True
                        sent += 1
                        break
                    if not outcome.is_transient:
                        break
                    if attempt < _ATTEMPTS - 1:
                        await asyncio.sleep(_retry_sleep(attempt))
            finally:
                if not delivered:
                    await _release(key)
                    logger.error(
                        "human_handoff_notification_undelivered",
                        alarm="human_handoff_notification_undelivered",
                        conversation_id=str(conversation_id), tenant_id=str(tenant.id),
                    )
        logger.info("human_handoff_notification_completed", conversation_id=str(conversation_id),
                    tenant_id=str(tenant.id), recipients=sent)
    except Exception as exc:
        logger.error("human_handoff_notification_undelivered",
                     alarm="human_handoff_notification_undelivered",
                     error_type=type(exc).__name__)
    return sent
