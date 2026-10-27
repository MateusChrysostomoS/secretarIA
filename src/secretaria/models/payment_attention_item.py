"""PaymentAttentionItem - a payment that needs the CLINIC's decision (decision D15/D16).

Whenever a charge is PAID but the consultation is not (or stopped being) booked/confirmed,
the system NEVER refunds: it writes one row here and the clinic decides (refund in the
provider's own panel, rebook, keep as credit) and resolves the item with a mandatory note.

Why a table and not columns on `pix_deposits` (spec section 6.5): some cases have no deposit
row of ours (an orphan payment); one charge can raise more than one item over its life; the
resolution (who, when, what, note) is audit, not money state; and the badge count is a plain
query on (tenant_id, status).

LGPD: `patient_id` only - the display name is read from `patients` when the item is shown,
never copied here. No payer name, document, e-mail, phone or webhook body, ever. `patient_id`
is SET NULL when a patient is erased; the rest of the row is kept (accounting obligation).
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

ATTENTION_OPEN = "open"
ATTENTION_RESOLVED = "resolved"

REASON_LATE_SLOT_TAKEN = "late_slot_taken"
REASON_PAID_THEN_CANCELLED = "paid_then_cancelled"
REASON_REFUND_DUE_POLICY = "refund_due_policy"
REASON_DUPLICATE_PAYMENT = "duplicate_payment"
REASON_AMOUNT_UNDER = "amount_under"
REASON_AMOUNT_OVER = "amount_over"
REASON_ORPHAN_PAYMENT = "orphan_payment"
REASON_REFERENCE_MISMATCH = "reference_mismatch"
REASON_PROMOTION_FAILED = "promotion_failed"
REASON_REFUNDED_EXTERNALLY = "refunded_externally"
# TASK-052: the clinic released a PAID slot and the refund fell due (cancelled, no answer by B or
# in 24 h, or the clinic refused "Manter") - the clinic returns the deposit in the provider's panel.
REASON_RELEASED_REFUND_DUE = "released_refund_due"
# 2026-10-26: the patient changed to a CHEAPER service and asked for the sinal difference back; the
# clinic returns it in the provider's panel (never automatic).
REASON_SERVICE_DIFF_REFUND_DUE = "service_diff_refund_due"
# 2026-10-26: the Pix of a service-change difference was paid and the row changed, but the agenda
# event could not follow (the longer window is no longer free, or the agenda was unreachable):
# the clinic adjusts the agenda - never two overlapping events, never the payment undone.
REASON_SERVICE_DIFF_AGENDA_PENDING = "service_diff_agenda_pending"
# 2026-10-27 (follow-up B): the Pix of a service-change difference could not be created (the
# provider was down or erroring), so the dearer service stood at once without it: the clinic
# collects the difference (`amount_cents`) itself. Money NOT received - never part of a refund.
REASON_SERVICE_DIFF_COLLECT_DUE = "service_diff_collect_due"

ATTENTION_REASONS = frozenset(
    {
        REASON_LATE_SLOT_TAKEN,
        REASON_PAID_THEN_CANCELLED,
        REASON_REFUND_DUE_POLICY,
        REASON_DUPLICATE_PAYMENT,
        REASON_AMOUNT_UNDER,
        REASON_AMOUNT_OVER,
        REASON_ORPHAN_PAYMENT,
        REASON_REFERENCE_MISMATCH,
        REASON_PROMOTION_FAILED,
        REASON_REFUNDED_EXTERNALLY,
        REASON_RELEASED_REFUND_DUE,
        REASON_SERVICE_DIFF_REFUND_DUE,
        REASON_SERVICE_DIFF_AGENDA_PENDING,
        REASON_SERVICE_DIFF_COLLECT_DUE,
    }
)

RESOLUTION_ACTIONS = frozenset(
    {"refunded_in_provider_panel", "rebooked", "kept_as_credit", "other"}
)


class PaymentAttentionItem(Base):
    __tablename__ = "payment_attention_items"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # NULL for an orphan payment (no deposit row of ours).
    deposit_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("pix_deposits.id", ondelete="SET NULL"), nullable=True, index=True
    )
    provider: Mapped[str] = mapped_column(String(16))
    provider_charge_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    reason: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(
        String(16), server_default=ATTENTION_OPEN, default=ATTENTION_OPEN
    )
    amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paid_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # The ORIGINAL slot (from the booking snapshot or the appointment).
    slot_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    slot_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    service_label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # Only for refund_due_policy / paid_then_cancelled: what the clinic's policy says is due.
    suggested_refund_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    provider_refund_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # `<provider>:<charge id or event id>:<reason>` - replay, reconciliation and sweeps can
    # never duplicate an item because this is unique per tenant.
    dedupe_key: Mapped[str] = mapped_column(String(220))
    # Set once the clinic was e-mailed about this item (`services/payments/alerts.py`, F2): the
    # alert sweep only ever picks items where it is NULL, so an item is announced exactly once.
    alerted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # --- resolution (written by the hub `resolve` endpoint, F2) ---
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resolved_by_name: Mapped[str | None] = mapped_column(String(80), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "dedupe_key", name="uq_payment_attention_items_dedupe"),
        Index("ix_payment_attention_items_tenant_status", "tenant_id", "status", "detected_at"),
    )
