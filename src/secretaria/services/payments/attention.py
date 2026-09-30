"""Attention items: a payment that needs the CLINIC's decision (decisions D15/D16).

`open_attention_item` is the ONLY function that inserts into `payment_attention_items`. It is
idempotent by construction: the row's `(tenant_id, dedupe_key)` is unique and the insert is
`ON CONFLICT DO NOTHING`, so a replayed webhook, the reconciliation job and the sweeps can
all reach the same situation without ever duplicating (or hiding) an item.

The system NEVER refunds. When a charge is PAID but the consultation is not (or stopped being)
booked, this is what it does instead: write one item, flag the deposit
(`fulfillment_status = needs_attention`) and let the clinic decide in the provider's own
panel. The hub endpoints that list/resolve items, and the clinic e-mail alert, are F2.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_REASONS,
    PaymentAttentionItem,
)
from secretaria.models.pix_deposit import (
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_RESOLVED,
    PixDeposit,
)
from secretaria.services.payments.db_utils import insert_ignore

_DEDUPE_MAX = 220


def attention_dedupe_key(provider: str, reference: str, reason: str) -> str:
    """`<provider>:<charge id or event id>:<reason>`, capped to the column width (the reason
    is the tail that makes two reasons on one charge two items, so the reference is cut)."""
    suffix = f":{reason}"
    head = f"{provider}:{reference}"
    return head[: _DEDUPE_MAX - len(suffix)] + suffix


@dataclass(frozen=True)
class AttentionOutcome:
    """`created` False means the situation already had its item (a replay): not an error."""

    item_id: UUID | None
    created: bool


def mark_fulfillment(deposit: PixDeposit, status: str) -> bool:
    """Set `deposit.fulfillment_status` WITHOUT ever overwriting `needs_attention` (only the
    clinic's resolution clears it). Returns whether the value changed."""
    if deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION and status not in (
        FULFILLMENT_NEEDS_ATTENTION,
        FULFILLMENT_RESOLVED,
    ):
        return False
    deposit.fulfillment_status = status
    return True


async def open_attention_item(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    reason: str,
    dedupe_key: str,
    deposit: PixDeposit | None = None,
    provider: str | None = None,
    provider_charge_id: str | None = None,
    patient_id: UUID | None = None,
    amount_cents: int | None = None,
    paid_amount_cents: int | None = None,
    paid_at: datetime | None = None,
    slot_start: datetime | None = None,
    slot_end: datetime | None = None,
    service_label: str | None = None,
    suggested_refund_cents: int | None = None,
) -> AttentionOutcome:
    """Open the attention item for one situation, once. Does not commit.

    Anything not passed is taken from `deposit`. A `deposit` that belongs to another tenant
    is a programming error (ValueError) and writes nothing: an item can never point across
    clinics. Stores ids, amounts, dates and the service label only - never a payer name,
    document, e-mail, phone or webhook body.
    """
    if reason not in ATTENTION_REASONS:
        raise ValueError(f"unknown attention reason: {reason!r}")
    if deposit is not None:
        if deposit.tenant_id != tenant_id:
            raise ValueError("the deposit belongs to another tenant")
        provider = provider or deposit.provider
        provider_charge_id = (
            provider_charge_id or deposit.provider_charge_id or deposit.asaas_payment_id
        )
        patient_id = patient_id or deposit.patient_id
        amount_cents = amount_cents if amount_cents is not None else deposit.amount_cents
        paid_at = paid_at or deposit.paid_at
    if not provider:
        raise ValueError("an attention item needs a provider")

    item_id = await insert_ignore(
        session,
        PaymentAttentionItem,
        {
            "tenant_id": tenant_id,
            "deposit_id": deposit.id if deposit is not None else None,
            "provider": provider,
            "provider_charge_id": provider_charge_id,
            "patient_id": patient_id,
            "reason": reason,
            "status": ATTENTION_OPEN,
            "amount_cents": amount_cents,
            "paid_amount_cents": paid_amount_cents,
            "paid_at": paid_at,
            "slot_start": slot_start,
            "slot_end": slot_end,
            "service_label": service_label[:120] if service_label else None,
            "suggested_refund_cents": suggested_refund_cents,
            "dedupe_key": dedupe_key,
        },
        index_elements=["tenant_id", "dedupe_key"],
    )
    if item_id is not None and deposit is not None:
        mark_fulfillment(deposit, FULFILLMENT_NEEDS_ATTENTION)
    return AttentionOutcome(item_id=item_id, created=item_id is not None)


async def mark_refund_seen(
    session: AsyncSession, *, tenant_id: UUID, deposit_id: UUID, now: datetime
) -> bool:
    """The provider reported a refund (the clinic did it in the provider's panel): stamp the
    OPEN items of this deposit with `provider_refund_seen_at`. Never resolves them - the
    decision and the note are the clinic's. True when at least one item was stamped."""
    result = await session.execute(
        update(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.tenant_id == tenant_id,
            PaymentAttentionItem.deposit_id == deposit_id,
            PaymentAttentionItem.status == ATTENTION_OPEN,
            PaymentAttentionItem.provider_refund_seen_at.is_(None),
        )
        .values(provider_refund_seen_at=now)
        .execution_options(synchronize_session=False)
    )
    return bool(result.rowcount)


async def has_open_item(session: AsyncSession, *, tenant_id: UUID, deposit_id: UUID) -> bool:
    count = await session.scalar(
        select(func.count())
        .select_from(PaymentAttentionItem)
        .where(
            PaymentAttentionItem.tenant_id == tenant_id,
            PaymentAttentionItem.deposit_id == deposit_id,
            PaymentAttentionItem.status == ATTENTION_OPEN,
        )
    )
    return bool(count)
