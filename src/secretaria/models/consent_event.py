"""ConsentEvent model - LGPD consent/legal-basis audit trail (groundwork).

One row per consent-relevant moment for a patient, identified by `wa_id`
(not a `patient_id` FK — a consent record should outlive whatever happens to
the Patient row's lifecycle bookkeeping and is looked up by the same
`wa_id` the LGPD privacy contract uses, see api/internal_privacy.py). Two
kinds exist by convention:

  - "first_contact_service": recorded exactly ONCE, at patient creation
    (workers/tasks.py:_persist_inbound_message, the branch that creates a
    NEW `Patient` row) — never per message.
  - "reminder_opt_out" / "reminder_opt_in": the convention for whenever
    `Patient.reminder_opt_out` is flipped. Nothing currently mutates that
    flag outside its default (see models/patient.py) — no write path exists
    yet — so no code emits these two kinds today. The NEXT change that adds
    an opt-out/opt-in mutation (e.g. a doctor-hub toggle, or a patient-facing
    "pare de me lembrar" command) must record one of these two kinds at that
    same mutation point.

`legal_basis` is free text pending a lawyer's sign-off on which LGPD basis
(execução de contrato vs. consentimento) actually applies to each kind — see
the literal "TODO_LAWYER" text recorded today by the one write path that
exists.

Erasure (api/internal_privacy.py) hard-deletes every ConsentEvent for a
(tenant_id, wa_id) pair, counted under the `"consent_events"` key.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class ConsentEvent(Base):
    """One LGPD consent/legal-basis moment for a (tenant, wa_id) subject."""

    __tablename__ = "consent_events"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    wa_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(48))
    legal_basis: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
