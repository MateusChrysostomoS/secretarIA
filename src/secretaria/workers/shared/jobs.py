"""jobs - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    HandoverState,
    ProcessedEvent,
)
from secretaria.services.email import (
    send_transactional_email_message,
)

logger = get_logger(__name__)


async def send_transactional_email(ctx: dict, template: str, to: str, variables: dict) -> None:
    """arq job: render + send one onboarding transactional email.

    Enqueued by `POST /internal/notifications/email`
    (api/internal_provisioning.py). Never raises: `send_transactional_email_message`
    already swallows every failure (disabled, unknown template, SMTP error)
    and returns a bool - this wrapper just adapts arq's `(ctx, ...)` calling
    convention and logs the outcome. Positional order (template BEFORE to)
    matches the contract's function signature exactly.
    """
    sent = await send_transactional_email_message(to=to, template=template, variables=variables)
    logger.info("worker_transactional_email_processed", template=template, sent=sent)

async def check_handover_timeouts(ctx: dict) -> None:
    """arq cron: hand stale HUMAN_ACTIVE conversations back to the bot.

    A conversation a human secretary has not touched within
    HANDOVER_TIMEOUT_MINUTES is flipped back to BOT_ACTIVE so the bot answers
    the patient's next message. Registered in arq_worker.WorkerSettings.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=get_settings().HANDOVER_TIMEOUT_MINUTES)
    flipped = 0
    async with async_session_factory() as session:
        async with session.begin():
            stale = await session.scalars(
                select(Conversation).where(
                    Conversation.handover_state == HandoverState.HUMAN_ACTIVE,
                    Conversation.last_human_message_at.is_not(None),
                    Conversation.last_human_message_at < cutoff,
                )
            )
            for conversation in stale:
                conversation.handover_state = HandoverState.BOT_ACTIVE
                flipped += 1
                logger.info(
                    "worker_handover_timeout_reset",
                    conversation_id=str(conversation.id),
                )
    if flipped:
        logger.info("worker_handover_timeouts_swept", flipped=flipped)

async def _claim_event(key: str) -> bool:
    """Insert `key` into the ProcessedEvent ledger. True iff THIS call claimed it."""
    async with async_session_factory() as session:
        try:
            async with session.begin():
                existing = await session.scalar(
                    select(ProcessedEvent.id).where(ProcessedEvent.event_id == key)
                )
                if existing is not None:
                    return False
                session.add(ProcessedEvent(event_id=key))
        except IntegrityError:
            return False
    return True

async def _release_event(key: str, *, event: str = "cancellation_notice_release_failed") -> None:
    """Give back a claim whose send did not happen — nothing more.

    A release states one fact: the key was NOT consumed, so whoever runs next
    is free to claim it. It does not schedule anything and it does not promise
    a retry. The caller is what decides whether one exists — in
    `send_cancellation_notice` that means raising `arq.Retry` right after
    releasing, which is the only thing that actually re-runs the job.

    `event` names the failure line, so a second caller can be told apart in the
    logs without a second copy of this function. It defaults to the original
    caller's name precisely so that caller's log output is unchanged.
    """
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning(event, key=key, error=str(exc))
