"""E-mail the clinic when a patient cancels a consultation whose deposit was already paid
(owner answer 13 of 2026-09-29; spec section 10A.4).

The items are written by many callers (the event core, the promoter, the reconciliation job, the
cancellation hooks), all through `attention.open_attention_item`. Rather than teach each of them
to send an e-mail, ONE sweep looks for the open items that deserve one and sends ONE e-mail per
item:

* ONLY the reasons in `EMAIL_REASONS` are ever mailed. `refund_due_policy` is the item
  `deposit_lifecycle.on_appointment_cancelled` opens when the PATIENT cancels a consultation whose
  deposit was paid. Every other reason (`paid_then_cancelled`, `late_slot_taken`, the amount and
  reference mismatches, ...) is visible on screen only, in the Chat notice and in "Pagamentos que
  precisam de decisão": the sweep does not even select them, so it never marks them either;
* EXACTLY ONE e-mail per item, never repeated. The item is CLAIMED first with an atomic
  `UPDATE ... SET alerted_at = :now WHERE id = :id AND alerted_at IS NULL` (one winner among
  concurrent workers and ticks) and only the winner sends. There is no per-clinic debounce and no
  Redis: the idempotency key is the item's own id;
* a send that did NOT leave (no SMTP, a refusing server) releases the claim, so a later tick tries
  again; a send that went out is never repeated. A clinic without a `contact_email` is skipped
  without claiming anything: the item is owed until the clinic has an address, and the notice in
  the Chat does not depend on the e-mail;
* the body names no patient and no amount: it says a paid consultation was cancelled and where to
  decide.

Push notifications, WhatsApp to the staff and in-console notifications are out of scope (spec
19): the badge in the Chat is the primary channel, this e-mail is the secondary one.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    REASON_REFUND_DUE_POLICY,
    PaymentAttentionItem,
)
from secretaria.services import email

logger = get_logger(__name__)

# The ONLY reasons that send an e-mail. Extending this set is a product decision, not a refactor.
EMAIL_REASONS = frozenset({REASON_REFUND_DUE_POLICY})
MAX_ITEMS_PER_TICK = 100


@dataclass
class AlertReport:
    candidates: int = 0
    emailed: int = 0
    skipped_no_contact: int = 0
    lost_the_claim: int = 0
    released: int = 0


def _is_mine(item_id: UUID):
    """The row this sweep may claim: open, of an e-mailed reason, not announced yet."""
    return (
        PaymentAttentionItem.id == item_id,
        PaymentAttentionItem.status == ATTENTION_OPEN,
        PaymentAttentionItem.reason.in_(EMAIL_REASONS),
        PaymentAttentionItem.alerted_at.is_(None),
    )


async def alert_new_items(session: AsyncSession, *, now: datetime | None = None) -> AlertReport:
    """One tick of the sweep. Commits per item; one item failing never stops the others."""
    now = now or datetime.now(UTC)
    report = AlertReport()
    rows = (
        await session.execute(
            select(PaymentAttentionItem.id, PaymentAttentionItem.tenant_id)
            .where(
                PaymentAttentionItem.status == ATTENTION_OPEN,
                PaymentAttentionItem.reason.in_(EMAIL_REASONS),
                PaymentAttentionItem.alerted_at.is_(None),
            )
            .order_by(PaymentAttentionItem.detected_at)
            .limit(MAX_ITEMS_PER_TICK)
        )
    ).all()
    for item_id, tenant_id in rows:
        report.candidates += 1
        try:
            await _alert_one(session, item_id, tenant_id, now, report)
        except Exception as exc:  # noqa: BLE001 - one item never stops the batch
            await session.rollback()
            logger.warning("payment_attention_alert_failed", error_type=type(exc).__name__)
    return report


async def _alert_one(
    session: AsyncSession, item_id: UUID, tenant_id: UUID, now: datetime, report: AlertReport
) -> None:
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None or not tenant.contact_email:
        report.skipped_no_contact += 1
        return
    claim = await session.execute(
        update(PaymentAttentionItem)
        .where(*_is_mine(item_id))
        .values(alerted_at=now)
        .execution_options(synchronize_session=False)
    )
    await session.commit()  # the claim is durable BEFORE the send: nobody else can take it now
    if claim.rowcount != 1:
        report.lost_the_claim += 1
        return
    if await email.send_paid_cancellation_alert(tenant.contact_email, tenant.clinic_name):
        report.emailed += 1
        return
    # Nothing left the building (no SMTP, a refusing server): give the item back, so a later tick
    # tries again. This is the only path that un-stamps an item, and it runs only when the
    # message was NOT sent.
    await session.execute(
        update(PaymentAttentionItem)
        .where(PaymentAttentionItem.id == item_id)
        .values(alerted_at=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    report.released += 1
