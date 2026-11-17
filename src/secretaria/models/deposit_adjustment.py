"""DepositAdjustment - the difference of the sinal when a PAID appointment changes service.

Spec: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md. A child of
`pix_deposits` (whose `appointment_id` stays unique: the original deposit never changes, and
`PixDeposit.amount_cents` is NOT touched by an adjustment). One row per service change:

`kind` increase - the new service costs more: a Pix of ONLY the difference is charged
    (`provider_charge_id`); the service changes after it is paid (`pending_edit` holds the edit
    that is applied in the same transaction as the payment). Statuses: awaiting_payment ->
    applied | expired | cancelled | cancelled_void_pending (cancelled while the provider could
    not void the Pix: the payments sweep retries the void, then `cancelled`). collect_due - the
    Pix could not be created (provider down/erroring): the change stood at once and the clinic
    has an item to collect the difference; never money held (follow-up B, 2026-10-27).
`kind` decrease - the service changed at once; no charge (`provider_charge_id` NULL). Statuses:
    asked (the patient is asked whether they want the difference back) -> refund_requested (an
    attention item for the clinic, never an automatic refund) | kept_credit (the patient said no
    or did not answer in 24 h: the difference is only recorded as a discount on the visit);
    clinic_kept - too close to the visit or under the R$ 10 floor, nobody is asked.

`mode` (TASK-085 R, migration c9e4a2f7b3d1; tasks/TASK-085/sinal-trocas/contrato.md) says how
an `increase` waits for its Pix. NULL = a legacy row (created before TASK-085 R): today's
semantics above, untouched. `suspend` (D-R4, a clinic that books only AFTER the sinal): nothing is
written on the appointment, the patient no longer sees it (`adjustment_suspension`), the old slot
stays (the row is live) and the new one is held by a `booking_holds` row of this adjustment.
Unpaid at `expires_at` -> `awaiting_choice` (the three buttons) -> `kept_old` (Manter / silence) |
`expired` (Reenviar: a NEW adjustment takes over) | `cancelled` (Cancelar ambas). `applied` (D-R5,
a clinic that does not require the sinal to book): the change was written at once; unpaid ->
`collect_due` with a `service_diff_unpaid` item.

LGPD: ids, instants, service labels and amounts only - never a name, document or phone.
`status` / `kind` / `mode` are plain VARCHAR (no PG enum): a new state never needs an
`ALTER TYPE`.
"""

import uuid
from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

KIND_INCREASE = "increase"
KIND_DECREASE = "decrease"

STATUS_AWAITING_PAYMENT = "awaiting_payment"
STATUS_APPLIED = "applied"
STATUS_EXPIRED = "expired"
STATUS_CANCELLED = "cancelled"
STATUS_ASKED = "asked"
STATUS_REFUND_REQUESTED = "refund_requested"
STATUS_KEPT_CREDIT = "kept_credit"
STATUS_CLINIC_KEPT = "clinic_kept"
# Follow-up B (2026-10-27). Plain VARCHAR(24) values: no migration.
STATUS_COLLECT_DUE = "collect_due"
STATUS_CANCELLED_VOID_PENDING = "cancelled_void_pending"
# TASK-085 R (D-R4/D-R8), only on `mode="suspend"` rows. Plain VARCHAR(24) values.
# The difference Pix expired unpaid: the patient chooses (Manter / Reenviar / Cancelar ambas).
STATUS_AWAITING_CHOICE = "awaiting_choice"
# "Manter a antiga" (or no answer by the deadline): the old consultation stands as it was.
STATUS_KEPT_OLD = "kept_old"

# TASK-085 R: how an `increase` waits for its Pix (NULL = legacy row, today's semantics).
MODE_SUSPEND = "suspend"
MODE_APPLIED = "applied"
ADJUSTMENT_MODES = frozenset({MODE_SUSPEND, MODE_APPLIED})
# A `mode="suspend"` adjustment in one of these statuses SUSPENDS its appointment (hidden from the
# patient, edits refused to the clinic except "Liberar horário" - D-R9). Order is stable.
SUSPENDED_STATUSES: tuple[str, ...] = ("awaiting_payment", "awaiting_choice")

ADJUSTMENT_STATUSES = frozenset(
    {
        STATUS_AWAITING_PAYMENT,
        STATUS_APPLIED,
        STATUS_EXPIRED,
        STATUS_CANCELLED,
        STATUS_ASKED,
        STATUS_REFUND_REQUESTED,
        STATUS_KEPT_CREDIT,
        STATUS_CLINIC_KEPT,
        STATUS_COLLECT_DUE,
        STATUS_CANCELLED_VOID_PENDING,
        STATUS_AWAITING_CHOICE,
        STATUS_KEPT_OLD,
    }
)


class DepositAdjustment(Base):
    __tablename__ = "deposit_adjustments"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    deposit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pix_deposits.id", ondelete="CASCADE"), index=True
    )
    # Not unique (a deposit can be adjusted more than once, one at a time).
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), nullable=True, index=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(24))
    # TASK-085 R: `suspend` | `applied` | NULL (legacy). See the module docstring.
    mode: Mapped[str | None] = mapped_column(String(16), nullable=True)
    old_service: Mapped[str] = mapped_column(String(120))
    new_service: Mapped[str] = mapped_column(String(120))
    # The edit applied when the Pix is paid (increase only): service, times, duration.
    pending_edit: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    diff_cents: Mapped[int] = mapped_column(Integer)
    # The provider's charge id of the difference Pix; NULL for a decrease. Unique when set.
    provider_charge_id: Mapped[str | None] = mapped_column(String(96), nullable=True, unique=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    asked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_deposit_adjustments_tenant_status", "tenant_id", "status", "expires_at"),
    )
