"""A slot the clinic released with a Pix deposit, the patient's choice, and what it schedules.

TASK-052, owner decisions 2026-10-12 (docs/superpowers/specs/2026-10-11-liberar-horario-com-
sinal-pago-design.md). The platform never returns money (decision D15) and does not decide for
the patient. One `DepositReleaseOffer` per released appointment that had a deposit, with two
independent axes:

`decision` - what the patient (and, for "Manter", the clinic) did:
    none / rescheduling      - still open ("Remarcar" tapped but not concluded = rescheduling);
    keep_requested           - "Manter horário" tapped: the CLINIC decides within 24 h of the tap
                               (`keep_deadline_at`); no B/E while it is open;
    kept                     - final: the clinic accepted; the deposit moved to
                               `new_appointment_id`;
    keep_declined            - the clinic refused, or did not decide in 24 h (`keep_outcome`); the
                               refund is due; the patient may still reschedule or cancel;
    rebooked                 - final: a "Remarcar" booking carried the deposit;
    cancelled                - "Cancelar" tapped (the patient may still reschedule);
    no_answer_cancelled      - final: 24 h after the release without a final choice
                               ("Cancelado por falta de resposta"); never reopened;
    not_offered              - the deposit was NOT paid at the release: a record for the clinic's
                               list only, no buttons, no deadlines.

`refund_status` - the money:
    not_due -> due (cancel, or B with no answer) -> marked (the clinic says it refunded in the
    provider's panel) -> received / not_received (the patient's answer); due -> clinic_decided
    (the clinic resolved the attention item another way).

Every send is one `DepositReleaseEmail` row, claimed before it is sent, so a retry or a second
worker never sends it twice. LGPD: ids, instants, states and kinds only.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

DECISION_NONE = "none"
DECISION_RESCHEDULING = "rescheduling"
DECISION_KEEP_REQUESTED = "keep_requested"
DECISION_KEPT = "kept"
DECISION_KEEP_DECLINED = "keep_declined"
DECISION_REBOOKED = "rebooked"
DECISION_CANCELLED = "cancelled"
DECISION_NO_ANSWER = "no_answer_cancelled"
DECISION_NOT_OFFERED = "not_offered"
# B and E apply only to these (no answer yet, or an unfinished "Remarcar").
SILENT_DECISIONS = (DECISION_NONE, DECISION_RESCHEDULING)
KEEP_FROM = (DECISION_NONE, DECISION_RESCHEDULING)
RESCHEDULE_FROM = (
    DECISION_NONE,
    DECISION_RESCHEDULING,
    DECISION_CANCELLED,
    DECISION_KEEP_REQUESTED,
    DECISION_KEEP_DECLINED,
)
CANCEL_FROM = (
    DECISION_NONE,
    DECISION_RESCHEDULING,
    DECISION_KEEP_REQUESTED,
    DECISION_KEEP_DECLINED,
)

KEEP_OUTCOME_ACCEPTED = "accepted"
KEEP_OUTCOME_REFUSED = "refused"
KEEP_OUTCOME_TIMEOUT = "timeout"

REFUND_NOT_DUE = "not_due"
REFUND_DUE = "due"
REFUND_MARKED = "marked"
REFUND_RECEIVED = "received"
REFUND_NOT_RECEIVED = "not_received"
REFUND_CLINIC_DECIDED = "clinic_decided"
# The states in which the patient's "Recebido" / "Não recebido" mean something.
REFUND_ANSWERABLE = (REFUND_MARKED, REFUND_NOT_RECEIVED)
# The clinic has not said it refunded: a "Remarcar" booking still carries the deposit.
REFUND_CARRYABLE = (REFUND_NOT_DUE, REFUND_DUE)

REFUND_SOURCE_PATIENT = "patient"
REFUND_SOURCE_TIMEOUT = "timeout"
REFUND_SOURCE_KEEP_REFUSED = "keep_refused"
REFUND_SOURCE_KEEP_TIMEOUT = "keep_timeout"
# The provider check at the "Remarcar" confirmation said "do not reuse" (owner 2026-10-12).
REFUND_SOURCE_CARRY_REFUNDED = "carry_refunded"  # Asaas: refunded / refund requested / in progress
REFUND_SOURCE_CARRY_UNKNOWN = "carry_unknown"  # Asaas unreachable / error / unreadable: fail-safe

# `DepositReleaseOffer.carry_check`: what the payment provider said when the patient confirmed the
# booking of a "Remarcar" (recorded once; the first answer stands).
CARRY_CHECK_REUSABLE = "reusable"  # paid, no refund: the new booking carries the deposit
CARRY_CHECK_REFUNDED = "refunded"  # refunded or being refunded: a NEW Pix is charged
CARRY_CHECK_UNKNOWN = "unknown"  # could not tell: FAIL-SAFE = a NEW Pix (patient may pay twice)
CARRY_CHECKS = (CARRY_CHECK_REUSABLE, CARRY_CHECK_REFUNDED, CARRY_CHECK_UNKNOWN)

# `DepositReleaseEmail.kind` (<= 24 characters).
EMAIL_REFUND_CANCEL = "clinic_refund_cancel"  # at the "Cancelar" tap, always right away
EMAIL_REFUND_NOANSWER = "clinic_refund_noanswer"  # at B, nobody answered
EMAIL_REFUND_DAY = "clinic_refund_day"  # at E = T + 24 h
EMAIL_REFUND_REMINDER = "clinic_refund_reminder"  # "Não recebido" (seq 1, 2, ...)
EMAIL_KEEP_REQUEST = "clinic_keep_request"  # "Manter horário" tapped: the clinic decides
EMAIL_KEEP_DECLINED = "clinic_keep_declined"  # refused / no decision in 24 h: refund due
EMAIL_REBOOKED_DEPOSIT = "clinic_rebooked_deposit"  # "REMARCADA levando o sinal"
EMAIL_CARRY_REFUSED = "clinic_carry_refused"  # new Pix charged at the Remarcar: refund the old one
EMAIL_PATIENT_KEEP_ACCEPTED = "patient_keep_accepted"
EMAIL_PATIENT_KEEP_DECLINED = "patient_keep_declined"
EMAIL_PATIENT_CHECK = "patient_check"  # "Recebido / Não recebido", when the clinic marks refunded
# The clinic's refund notices (paused while a keep request is open; cancelled by a carry-over).
REFUND_NOTICE_KINDS = (
    EMAIL_REFUND_CANCEL,
    EMAIL_REFUND_NOANSWER,
    EMAIL_REFUND_DAY,
    EMAIL_REFUND_REMINDER,
    EMAIL_KEEP_DECLINED,
)

OUTCOME_SENDING = "sending"  # claimed; a send interrupted mid-way stays here (at most once)
OUTCOME_SENT = "sent"
OUTCOME_SKIPPED = "skipped"
OUTCOME_NO_CONTACT = "skipped_no_contact"
OUTCOME_GIVEN_UP = "given_up"


class DepositReleaseOffer(Base):
    __tablename__ = "deposit_release_offers"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    deposit_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pix_deposits.id", ondelete="CASCADE"), index=True
    )
    released_appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE")
    )
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True, index=True
    )
    deposit_paid: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    decision: Mapped[str] = mapped_column(
        String(24), default=DECISION_NONE, server_default=DECISION_NONE
    )
    refund_status: Mapped[str] = mapped_column(
        String(16), default=REFUND_NOT_DUE, server_default=REFUND_NOT_DUE
    )
    refund_source: Mapped[str | None] = mapped_column(String(16), nullable=True)
    released_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # T
    slot_start_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # S
    decide_by_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # B
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))  # E = T + 24 h
    # The clinic's "may send a billed WhatsApp template" at release time, reused for the check.
    paid_notice_allowed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    first_contact_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    refund_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    refund_marked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    patient_answered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_reminder_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    after_start_card_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    keep_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    keep_deadline_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )  # K = the "Manter" tap + 24 h
    keep_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    keep_resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    keep_resolved_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    new_appointment_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # TASK-052 (owner 2026-10-12): the provider check made when the patient confirmed the
    # booking of a "Remarcar" (CARRY_CHECK_*), and the instant the paid deposit was reused - from
    # then on "marcar devolvido" is refused (409 `deposit_reused`).
    carry_check: Mapped[str | None] = mapped_column(String(16), nullable=True)
    carry_checked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    deposit_reused_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "released_appointment_id", name="uq_deposit_release_offers_released_appointment_id"
        ),
        Index("ix_deposit_release_offers_decision_decide_by", "decision", "decide_by_at"),
        Index("ix_deposit_release_offers_decision_expires", "decision", "expires_at"),
        Index("ix_deposit_release_offers_decision_keep_deadline", "decision", "keep_deadline_at"),
    )


class DepositReleaseEmail(Base):
    __tablename__ = "deposit_release_emails"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    offer_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("deposit_release_offers.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(24))
    seq: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint(
            "offer_id", "kind", "seq", name="uq_deposit_release_emails_offer_kind_seq"
        ),
        Index("ix_deposit_release_emails_due", "claimed_at", "due_at"),
    )
