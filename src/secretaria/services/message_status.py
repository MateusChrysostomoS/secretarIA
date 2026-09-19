"""Delivery state of a message: WhatsApp receipts and Brain-Message read marks.

The row keeps FACTS (`Message.delivered_at` / `read_at` / `failed_at`); the status a
bubble draws is derived from them (models/message.py::status_of). Two writers:

- `apply_whatsapp_statuses` - Meta's `statuses[]` callback, matched by `wam_id` inside
  the tenant that owns the receiving `phone_number_id`. Called from the arq worker only
  (`whatsapp-webhook-arq`: the webhook ACKs, the job writes).
- `mark_read` - a Brain-Message side reporting what it has seen: the patient (brain-api
  -> `/internal/brain-message/messages/read`) or the clinic's staff (hub
  `POST /tenants/me/conversations/{id}/messages/read`).

Every write is CONDITIONAL on the column still being NULL. That one rule makes the
whole thing idempotent (Meta re-delivers webhooks; a replay matches no row) and
monotonic (a "delivered" arriving after "read" finds `delivered_at` already filled -
`read` fills it too - and changes nothing). No read-modify-write, so two workers racing
on the same receipt cannot interleave into a regression either.

`updated_at` is set on every such write: it is the transcript's poll cursor, and a
status change is exactly what a poller that already holds the row needs to see.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Conversation, Message, MessageDirection, Tenant
from secretaria.schemas.webhook import WebhookStatus

logger = get_logger(__name__)

STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_READ = "read"
STATUS_FAILED = "failed"

_FAILURE_REASON_MAX = 255


@dataclass
class StatusApplyResult:
    """What one batch of receipts did. `unmatched` are receipts whose message was not
    found - possibly not committed yet (the send's row is written after Graph answers,
    and a fast receipt can overtake it), so the caller may try them once more later."""

    applied: int = 0
    unchanged: int = 0
    ignored: int = 0
    unmatched: list[WebhookStatus] = field(default_factory=list)


def meta_timestamp(raw: str | int | None) -> datetime | None:
    """Meta's epoch-seconds `timestamp` as an AWARE UTC datetime, or None if unusable."""
    if raw is None:
        return None
    try:
        return datetime.fromtimestamp(int(raw), tz=UTC)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def failure_reason(status: WebhookStatus) -> str:
    """'<code>: <title>' from the first error - Meta's generic text, never personal data."""
    if not status.errors:
        return "unknown"
    error = status.errors[0]
    parts = [str(part) for part in (error.code, error.title) if part not in (None, "")]
    return (": ".join(parts) or "unknown")[:_FAILURE_REASON_MAX]


async def _outbound_ids(session: AsyncSession, *, phone_number_id: str, wam_id: str) -> list[UUID]:
    """The OUTBOUND rows carrying `wam_id` in the tenant that owns `phone_number_id`.

    Scoped by tenant even though wamids are Meta-unique: a receipt only ever speaks for
    the number that received it, so it must never touch another clinic's row."""
    rows = await session.scalars(
        select(Message.id)
        .join(Conversation, Conversation.id == Message.conversation_id)
        .join(Tenant, Tenant.id == Conversation.tenant_id)
        .where(
            Tenant.phone_number_id == phone_number_id,
            Message.wam_id == wam_id,
            Message.direction == MessageDirection.OUTBOUND,
        )
    )
    return list(rows.all())


async def apply_whatsapp_statuses(
    session: AsyncSession, *, phone_number_id: str, statuses: list[WebhookStatus]
) -> StatusApplyResult:
    """Write each receipt onto its message; the caller owns the transaction.

    Logs counts only - never a wamid, never the recipient."""
    result = StatusApplyResult()
    now = datetime.now(UTC)
    for status in statuses:
        kind = (status.status or "").lower()
        if not status.id or kind not in (STATUS_SENT, STATUS_DELIVERED, STATUS_READ, STATUS_FAILED):
            result.ignored += 1
            logger.info("whatsapp_status_ignored", status=kind or None)
            continue
        if kind == STATUS_SENT:
            # The row exists since the send; "sent" adds nothing to it.
            result.unchanged += 1
            continue

        ids = await _outbound_ids(session, phone_number_id=phone_number_id, wam_id=status.id)
        if not ids:
            result.unmatched.append(status)
            continue

        at = meta_timestamp(status.timestamp) or now
        if kind == STATUS_DELIVERED:
            stmt = (
                update(Message)
                .where(Message.id.in_(ids), Message.delivered_at.is_(None))
                .values(delivered_at=at, updated_at=func.now())
            )
        elif kind == STATUS_READ:
            # Read implies delivered: a missing or late "delivered" is filled here, so
            # it then finds its column taken and changes nothing.
            stmt = (
                update(Message)
                .where(Message.id.in_(ids), Message.read_at.is_(None))
                .values(
                    read_at=at,
                    delivered_at=func.coalesce(Message.delivered_at, at),
                    updated_at=func.now(),
                )
            )
        else:
            stmt = (
                update(Message)
                .where(Message.id.in_(ids), Message.failed_at.is_(None))
                .values(failed_at=at, failure_reason=failure_reason(status), updated_at=func.now())
            )
        executed = await session.execute(stmt.execution_options(synchronize_session=False))
        if executed.rowcount:
            result.applied += 1
        else:
            result.unchanged += 1
    logger.info(
        "whatsapp_statuses_applied",
        applied=result.applied,
        unchanged=result.unchanged,
        ignored=result.ignored,
        unmatched=len(result.unmatched),
    )
    return result


async def read_cutoff(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    up_to_message_id: UUID | None,
    up_to: datetime | None,
) -> datetime | None:
    """The instant up to which a reader has seen this conversation, or None.

    By message id: that message's `created_at`, looked up INSIDE this conversation only
    (an id from another conversation is simply unknown here). By instant: the instant,
    clamped to now - a reader cannot have seen what does not exist yet.
    """
    if up_to_message_id is not None:
        return await session.scalar(
            select(Message.created_at).where(
                Message.id == up_to_message_id, Message.conversation_id == conversation_id
            )
        )
    if up_to is None:
        return None
    return min(up_to, datetime.now(UTC))


async def mark_read(
    session: AsyncSession,
    conversation_id: UUID,
    *,
    direction: MessageDirection,
    cutoff: datetime,
) -> int:
    """Mark this conversation's `direction` messages up to `cutoff` as read.

    Returns how many rows changed; rows already read (or failed) stay exactly as they
    are. `direction` is the side the READER did not write: the patient reads OUTBOUND
    rows, the staff reads INBOUND ones. Brain-Message only - the callers check the
    channel; a WhatsApp read is Meta's to report (`apply_whatsapp_statuses`)."""
    stmt = (
        update(Message)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == direction,
            Message.created_at <= cutoff,
            Message.read_at.is_(None),
            Message.failed_at.is_(None),
        )
        .values(
            read_at=func.now(),
            delivered_at=func.coalesce(Message.delivered_at, func.now()),
            updated_at=func.now(),
        )
        .execution_options(synchronize_session=False)
    )
    return (await session.execute(stmt)).rowcount or 0
