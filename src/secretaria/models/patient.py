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
    # WHEN this subject accepted the Terms of Use / Privacy Policy on WhatsApp,
    # or NULL if they never have. Written once, by the consent gate in
    # workers/tasks.py, when the "✅ Concordo" button is tapped.
    #
    # This is the OPERATIONAL flag the gate reads on every inbound turn; the
    # immutable audit trail is the matching ConsentEvent row
    # (kind="terms_accepted"). Two records on purpose, with different
    # lifetimes: `/dangerously-remove-context` DELETES the Patient row (and so
    # this column) but does NOT delete consent_events, so wiping a patient's
    # context replays a genuine first contact — including being asked again —
    # while the legal record of what they once accepted survives.
    #
    # A timestamp rather than a boolean because LGPD asks WHEN consent was
    # given, not merely whether. `is not None` is the truth test everywhere.
    # Compare PreCheck, which encodes the same thing as a transient
    # `sessions.state` of LGPD_PENDING -> ACTIVE and therefore cannot answer
    # "when did this person agree?" at all.
    lgpd_accepted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # The CLINIC's own Asaas customer id for this patient (Asaas accounts are
    # per-tenant — see tenant_credentials.asaas_api_key_encrypted). Reused
    # across deposits so a repeat patient doesn't get a duplicate Asaas
    # customer on every booking. NOT a secret (a foreign-system foreign key,
    # not a credential), so no `_encrypted` suffix.
    asaas_customer_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
