"""Service model — the clinic's CANONICAL catalog of bookable services.

Until this table existed, a service was just a string inside a JSON list, once
per professional (`Professional.appointment_types`, falling back to
`Tenant.appointment_types`). Nothing tied two professionals' "Limpeza" to the
same thing, and nothing stopped "Limpeza", "limpeza dental" and "Limpeza " from
becoming three services. Everything that has to REASON about services suffered
for it: the Pix deposit prices a booking by exact name match
(services/payments/deposit_lifecycle.py), "which other doctor offers what this
patient booked?" is a string comparison, and per-service reporting splits the
same service across rows.

This gives a service a stable IDENTITY, at the level it actually belongs to —
the clinic. `normalized_name` is the identity key: trimmed, casefolded,
accent-folded, whitespace-collapsed (services/service_catalog.py::normalize),
and UNIQUE per tenant, so the duplicate cannot even be written.

What stays per professional (inside `Professional.appointment_types`, which is
UNCHANGED): `price`, `duration_min` and whether that professional offers it at
all (`is_active`). Those legitimately differ between doctors in one clinic.
What moves here is what must NOT differ: the name and the descriptive copy.

Rollout (see docs/CHECKPOINT_service_catalog.md): this table is additive. The
per-professional JSON entries gain an OPTIONAL `service_id` key and keep every
field they have today, so every existing reader keeps working untouched while
tenants are backfilled one at a time
(scripts/backfill_service_catalog.py). Dropping the now-duplicated descriptive
fields from the JSON is a separate, later delivery.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base


class Service(Base):
    """One canonical, bookable service offered by a tenant."""

    __tablename__ = "services"
    __table_args__ = (
        # THE identity rule. Two spellings of the same service cannot coexist
        # in one clinic, and no clinic can ever see another's catalog.
        UniqueConstraint("tenant_id", "normalized_name", name="uq_services_tenant_normalized"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )

    # What patients see. Free to re-case/re-punctuate at will: renaming here
    # renames everywhere, because professionals reference `id`, never the name.
    name: Mapped[str] = mapped_column(String(120))
    # The identity key — derived from `name`, never entered by hand. Kept as a
    # stored column (not an expression index) so the uniqueness rule is legible
    # in the schema and portable to SQLite in tests.
    normalized_name: Mapped[str] = mapped_column(String(160))

    # Descriptive copy, owned HERE and nowhere else. This is what stops a
    # per-professional save from silently blanking descriptions: once an entry
    # references a service, resolution reads the copy from this row, so a
    # professional payload that omits it can no longer zero it.
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    long_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Pre-consult orientations ("Jejum de 8 horas"). Clinic-level on purpose:
    # the preparation for a service is a property of the SERVICE, not of which
    # doctor performs it. Same shape as the legacy JSON entry's `requirements`.
    requirements: Mapped[list | None] = mapped_column(JSON, nullable=True, default=list)

    # Whether the CLINIC still offers this service at all. A professional
    # additionally chooses whether THEY offer it (the per-professional entry's
    # own `is_active`); an inactive service is offered by nobody.
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    sort_order: Mapped[int] = mapped_column(Integer, server_default=text("0"), default=0)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
