"""One planned (or shown) reminder of one appointment - TASK-032, spec 4.1.

The schedule lives in the database, one row per reminder, instead of being
derived from time windows on every cron sweep (`plugins/reminders.py`). A row
is versioned by `appointment_start_at` and retired with `invalidated_at`:
rescheduling creates fresh identities even when the time is unchanged or returns
to a previous time. Sent/answered history survives; old buttons stay stale.

Vocabulary columns (`kind`, `status`, `channel`, `answer`, `warn_kind`) are plain
strings with the constants below, NOT native Postgres enums: a native enum is
what made the `appointment_status` type painful to extend, and these sets will
grow (R2-R4). Services validate against the tuples; the database does not.

PII: this table holds ids, timestamps and codes only. `last_error_code` is a
short code, never provider text, and never anything the patient wrote.
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
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

REMINDER_KIND_CUSTOM = "custom"  # clinic-configured lead time
REMINDER_KIND_DAY = "day"  # 24 h before
REMINDER_KIND_HOUR = "hour"  # 1 h before
REMINDER_KIND_CHAT = "chat"  # the opening message of the chat (born already sent)
# TASK-032 R7: the cards the CLINIC's actions put in front of the patient - "Seu médico
# confirmou ..." and "A clínica alterou ...". Born already sent, never warned about; they
# exist so the Confirmar / Cancelar / Alterar Dados taps resolve to a row exactly like a
# reminder's (workers/shared/reminder_actions.py).
REMINDER_KIND_STAFF_CONFIRM = "staff_confirm"
REMINDER_KIND_STAFF_EDIT = "staff_edit"
REMINDER_KINDS: tuple[str, ...] = (
    REMINDER_KIND_CUSTOM,
    REMINDER_KIND_DAY,
    REMINDER_KIND_HOUR,
    REMINDER_KIND_CHAT,
    REMINDER_KIND_STAFF_CONFIRM,
    REMINDER_KIND_STAFF_EDIT,
)
REMINDER_KINDS_STAFF: tuple[str, ...] = (REMINDER_KIND_STAFF_CONFIRM, REMINDER_KIND_STAFF_EDIT)
# Kinds that are shown, never planned: the cron engine, the reconcile backfill and the
# clinic warnings ignore them.
REMINDER_KINDS_UNPLANNED: tuple[str, ...] = (REMINDER_KIND_CHAT, *REMINDER_KINDS_STAFF)

REMINDER_STATUS_PENDING = "pending"
REMINDER_STATUS_SENDING = "sending"
REMINDER_STATUS_SENT = "sent"
REMINDER_STATUS_FAILED = "failed"
REMINDER_STATUS_SKIPPED = "skipped"
REMINDER_STATUS_CANCELLED = "cancelled"
REMINDER_STATUSES: tuple[str, ...] = (
    REMINDER_STATUS_PENDING,
    REMINDER_STATUS_SENDING,
    REMINDER_STATUS_SENT,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_SKIPPED,
    REMINDER_STATUS_CANCELLED,
)

REMINDER_CHANNEL_WHATSAPP = "whatsapp"
REMINDER_CHANNEL_EMAIL = "email"
REMINDER_CHANNEL_CHAT = "chat"
REMINDER_CHANNELS: tuple[str, ...] = (
    REMINDER_CHANNEL_WHATSAPP,
    REMINDER_CHANNEL_EMAIL,
    REMINDER_CHANNEL_CHAT,
)

REMINDER_ANSWER_CONFIRM = "confirm"
REMINDER_ANSWER_CANCEL = "cancel"
REMINDER_ANSWER_OTHER = "other"
REMINDER_ANSWERS: tuple[str, ...] = (
    REMINDER_ANSWER_CONFIRM,
    REMINDER_ANSWER_CANCEL,
    REMINDER_ANSWER_OTHER,
)

REMINDER_WARN_UNCONFIRMED = "unconfirmed"
REMINDER_WARN_DELIVERY_FAILED = "delivery_failed"
REMINDER_WARN_KINDS: tuple[str, ...] = (
    REMINDER_WARN_UNCONFIRMED,
    REMINDER_WARN_DELIVERY_FAILED,
)

# Partial-index predicate: only warnings that still have to go out.
_UNWARNED = text("warned_at IS NULL AND warn_due_at IS NOT NULL")
_ACTIVE = text("invalidated_at IS NULL")


class AppointmentReminder(Base):
    """A reminder of one appointment version (see the module docstring)."""

    __tablename__ = "appointment_reminders"
    __table_args__ = (
        # The cron claims `status = 'pending' AND due_at <= now`.
        Index("ix_appointment_reminders_status_due", "status", "due_at"),
        # The warning cron only scans warnings not yet sent (small, hot set).
        Index(
            "ix_appointment_reminders_warn_due",
            "warn_due_at",
            postgresql_where=_UNWARNED,
            sqlite_where=_UNWARNED,
        ),
        Index(
            "uq_appointment_reminders_version",
            "appointment_id",
            "kind",
            "appointment_start_at",
            unique=True,
            postgresql_where=_ACTIVE,
            sqlite_where=_ACTIVE,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    appointment_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    # SET NULL: erasing a patient (LGPD) keeps the appointment history intact.
    patient_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(String(16))
    # The appointment's start when this row was created (its version).
    appointment_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(16), default=REMINDER_STATUS_PENDING, server_default=REMINDER_STATUS_PENDING
    )
    channel: Mapped[str] = mapped_column(
        String(16), default=REMINDER_CHANNEL_WHATSAPP, server_default=REMINDER_CHANNEL_WHATSAPP
    )
    # False once the patient confirmed twice: the message goes out as a plain
    # reminder without the Confirm button.
    with_prompt: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Retire this identity without erasing sent/answered/warned history. A new
    # appointment generation gets fresh ids even when it returns to the same start.
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    answer: Mapped[str | None] = mapped_column(String(16), nullable=True)
    # When to warn the clinic if the appointment is still unconfirmed.
    warn_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    warn_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
