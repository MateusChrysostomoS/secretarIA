"""Fulfillment: what happens to a charge AFTER it is paid.

`fulfill_paid_charge` is the retryable, idempotent step that runs from the paid state of the
deposit (never from the webhook ledger, so a crash after the commit is re-run by the
promotion sweep). In F1 only the `after` timing exists: the consultation is already booked,
the patient was told by the post-commit notice, and fulfillment just records `confirmed`.

The `before` timing (F2) creates the consultation from `deposit.booking_snapshot` here. Until
that promoter exists this function REFUSES TO SWALLOW the money: a paid `before` charge opens
a `promotion_failed` attention item so the clinic sees it. F2 replaces exactly that branch.
"""

from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models.payment_attention_item import REASON_PROMOTION_FAILED
from secretaria.models.pix_deposit import (
    DEPOSIT_TIMING_BEFORE,
    FULFILLMENT_CONFIRMED,
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_NONE,
    FULFILLMENT_PROMOTING,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.services.payments import attention

logger = get_logger(__name__)


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def fulfill_paid_charge(session: AsyncSession, *, deposit_id: UUID) -> str:
    """Run the post-payment step for one deposit. Idempotent; does not commit. Returns a short
    outcome: missing | not_paid | noop | confirmed_after | promotion_not_available."""
    deposit = await session.get(PixDeposit, deposit_id)
    if deposit is None:
        return "missing"
    if deposit.status != PixDepositStatus.PAID:
        return "not_paid"
    if deposit.fulfillment_status not in (FULFILLMENT_NONE, FULFILLMENT_PROMOTING):
        return "noop"  # confirmed, needs_attention or resolved: nothing left to do here

    if deposit.deposit_timing != DEPOSIT_TIMING_BEFORE:
        attention.mark_fulfillment(deposit, FULFILLMENT_CONFIRMED)
        return "confirmed_after"

    snapshot = deposit.booking_snapshot or {}
    await attention.open_attention_item(
        session,
        tenant_id=deposit.tenant_id,
        reason=REASON_PROMOTION_FAILED,
        dedupe_key=attention.attention_dedupe_key(
            deposit.provider,
            deposit.provider_charge_id or str(deposit.id),
            REASON_PROMOTION_FAILED,
        ),
        deposit=deposit,
        slot_start=_iso(snapshot.get("start_at")),
        slot_end=_iso(snapshot.get("end_at")),
        service_label=snapshot.get("appointment_type"),
    )
    attention.mark_fulfillment(deposit, FULFILLMENT_NEEDS_ATTENTION)
    logger.error("payment_fulfillment_not_available", tenant_id=str(deposit.tenant_id))
    return "promotion_not_available"
