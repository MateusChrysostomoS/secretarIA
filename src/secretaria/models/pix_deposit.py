"""PixDeposit model - the sinal (deposit) payment lifecycle for a booked appointment.

Payment state is DELIBERATELY a separate state machine from `AppointmentStatus`
(models/appointment.py), not folded into it. The appointment's own status
answers "did the patient show up" (scheduled/attended/no_show/cancelled/...);
this table answers "what happened to the money" (awaiting/paid/refunded/
retained/expired). The two axes are orthogonal, not two views of one fact: a
no-show can end up NO_SHOW_RETAINED (the deposit was paid, the clinic keeps
it) or leave nothing to retain at all if the deposit was never paid in the
first place (AWAITING -> EXPIRED via a best-effort void, nothing charged) -
which outcome applies depends on the payment's own timing/state, not on the
appointment's outcome. Similarly a patient-initiated cancel can be
CANCELLED_REFUNDED or CANCELLED_RETAINED depending purely on the refund
window and the tenant's retention policy, regardless of what the appointment
row itself ends up saying. Merging the two enums would force every
appointment-status transition to also encode a money outcome it may not have
(or ever) - so they are tracked independently here and joined only by
`appointment_id`. See services/payments/deposit_lifecycle.py for every
transition.

One deposit per appointment (UNIQUE `appointment_id`): a reschedule updates
the Appointment row IN PLACE (same id, new start_at/end_at - see
services/calendar.py:update_event / api/hub/calendar.py:reschedule_appointment),
so an existing deposit naturally "follows" the rescheduled appointment with
zero migration machinery of its own. `register_reschedule`
(services/payments/deposit_lifecycle.py) only bumps `reschedule_count`
against `tenants.pix_reschedule_limit` - it never re-points this FK, and a
brand-new deposit row is never created for the same appointment twice.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

# What we did with the money, orthogonal to `status` (which is the money itself). A value here
# never needs a PG enum: the column is a plain VARCHAR (spec section 6.1).
FULFILLMENT_NONE = "none"
FULFILLMENT_PROMOTING = "promoting"
FULFILLMENT_CONFIRMED = "confirmed"
FULFILLMENT_NEEDS_ATTENTION = "needs_attention"
FULFILLMENT_RESOLVED = "resolved"

# When the deposit is charged relative to the consultation's confirmation (per clinic).
DEPOSIT_TIMING_BEFORE = "before"
DEPOSIT_TIMING_AFTER = "after"


class PixDepositStatus(str, enum.Enum):  # noqa: UP042 - StrEnum would change str(member)
    AWAITING = "aguardando_sinal"
    PAID = "confirmado_pago"
    CANCELLED_REFUNDED = "cancelado_reembolsado"
    CANCELLED_RETAINED = "cancelado_retido"
    NO_SHOW_RETAINED = "no_show_retido"
    EXPIRED = "expirado"


class PixDeposit(Base):
    """One Pix deposit (sinal) charge tied to exactly one appointment."""

    __tablename__ = "pix_deposits"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # One deposit per appointment - see module docstring for why a reschedule
    # never needs (or gets) a second row. NULLABLE since the payment provider core: in
    # `before` timing the charge is created BEFORE the appointment exists (Postgres lets
    # the UNIQUE hold many NULLs).
    appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), unique=True, index=True, nullable=True
    )
    # SET NULL (not CASCADE): deleting a patient row must never erase the
    # clinic's payment/refund history.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    # Asaas' own payment id. NULL only for the brief in-memory window between
    # "we decided to create a deposit" and "Asaas answered" - a create
    # failure never persists a row at all (services/payments/deposit_lifecycle.py
    # ::maybe_create_deposit). Unique + indexed so the webhook can dispatch to
    # the owning deposit in O(1) (services/payments/deposit_lifecycle.py::apply_asaas_event).
    asaas_payment_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer)
    percent_applied: Mapped[int] = mapped_column(Integer)
    # The Pix "copia e cola" payload. PUBLIC payment data (anyone holding it
    # can only PAY the charge, never read/access anything sensitive) - not a
    # secret, so no `_encrypted` suffix / Fernet encryption is needed here.
    pix_copy_paste: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PixDepositStatus] = mapped_column(
        # Native PG enum, values (not names) persisted - see
        # models/appointment.py's `status` column for why values_callable is
        # required (SQLite's permissive VARCHAR+CHECK rendering hides this).
        SAEnum(
            PixDepositStatus,
            name="pix_deposit_status",
            create_type=True,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        default=PixDepositStatus.AWAITING,
        server_default="aguardando_sinal",
    )
    # How many times THIS appointment (with a live deposit) has been
    # rescheduled, counted against `tenants.pix_reschedule_limit` by
    # services/payments/deposit_lifecycle.py::register_reschedule.
    reschedule_count: Mapped[int] = mapped_column(Integer, server_default="0", default=0)
    # Set once a refund (full or partial) is actually issued via Asaas. NULL
    # until resolved; 0 for a total-retention resolution (nothing refunded,
    # but still "resolved" so it reads differently from "never resolved").
    refunded_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    # --- payment provider core (TASK-016). All additive: code that predates them ignores them ---
    provider: Mapped[str] = mapped_column(String(16), server_default="asaas", default="asaas")
    # The provider's charge id. Dual-written with `asaas_payment_id` until the "narrow" phase;
    # readers of an Asaas deposit must accept EITHER (a row written by pre-core code has only
    # the legacy column).
    provider_charge_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    # OUR reference sent to the provider (= this row's id for new charges, the appointment id
    # for rows that predate the core). Matched against the provider's `externalReference`.
    external_reference: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_payment_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    paid_notice_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fulfillment_status: Mapped[str] = mapped_column(
        String(24), server_default=FULFILLMENT_NONE, default=FULFILLMENT_NONE
    )
    confirming_event_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    paid_amount_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # {conversation_id, professional_id, appointment_type, insurance, attendee_name, start_at,
    # end_at} - what the promotion needs when the hold has already been swept. Never the
    # patient's name (same rule as `booking_holds`).
    booking_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    hold_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)  # no FK: the hold is deleted
    calendar_event_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    deposit_timing: Mapped[str] = mapped_column(
        String(16), server_default=DEPOSIT_TIMING_AFTER, default=DEPOSIT_TIMING_AFTER
    )

    # --- `before` timing (TASK-017, F2). Additive, NULL for every row written before it ---
    # The conversation that is waiting for this charge (`FlowState.AWAITING_DEPOSIT`). SET NULL:
    # the money row outlives a deleted conversation. It is how the webhook-driven side (payment
    # arrives, Pix expires) finds the chat to tell and to move out of the waiting state.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # Set when the "Pix expired" notice went out, claimed AFTER a successful send (the paid twin,
    # `paid_notice_sent_at`, is the F1 column above): a replay never sends it twice and a failed
    # send is retried.
    expired_notice_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("provider", "provider_charge_id", name="uq_pix_deposits_provider_charge"),
        Index("ix_pix_deposits_tenant_status_expires", "tenant_id", "status", "expires_at"),
    )
