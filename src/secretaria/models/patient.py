"""Patient model - a person messaging a clinic via WhatsApp."""

import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Patient(Base):
    """A patient, identified by their WhatsApp id (wa_id) within a tenant."""

    __tablename__ = "patients"
    __table_args__ = (UniqueConstraint("tenant_id", "wa_id", name="uq_patients_tenant_wa_id"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # WhatsApp id == the patient's phone number in E.164-ish digits.
    wa_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Explicit opt-out from proactive reminder sends (plugins/reminders.py).
    # Reminders otherwise ride on the booking relationship itself — a patient
    # who booked an appointment has a legitimate expectation of a reminder
    # about it, so no separate opt-IN flow gates sending them. An explicit
    # opt-out, though, is always honored: this is that one override. A full
    # LGPD consent/preference registry (marketing opt-in, channel prefs, ...)
    # is a separate round — TODO(lgpd-consent-registry).
    reminder_opt_out: Mapped[bool] = mapped_column(
        Boolean, server_default=text("false"), default=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
