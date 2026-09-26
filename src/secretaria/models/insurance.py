"""Convênio (health-insurance plan) catalog: global list + per-clinic + per-doctor.

Until these tables existed, a clinic's convênios were a JSON list of free
strings on `Tenant.insurances`, typed by hand in the hub. "Unimed", "unimed" and
"Unimed BH" were three plans, and nothing could say which DOCTOR takes which
plan - so the booking flow could only ever record the answer, never use it.

Three tables, three levels:

- `insurance_catalog` - GLOBAL, shared by every tenant (owner's decision,
  2026-09-23: "catálogo... select dinâmico"). The canonical list of Brazilian
  operators, seeded by migration with a payment `mechanism` + `note` whose
  provenance lives in docs/CHECKPOINT_convenio_catalogo.md. Metadata only: it
  never filters or blocks anything and is not a promise about any contract.
- `tenant_insurance_plans` - which catalog entries THIS clinic accepts, plus
  the clinic's own `charge_deposit` flag per plan (decision 2026-09-25: the
  same operator can be direct billing in one clinic and reimbursement in
  another, so the Pix-deposit behaviour is per clinic AND per plan, never
  global and never inferred from the operator's name).
- `professional_insurance_plans` - which of the CLINIC's plans each doctor
  accepts. Always a subset of the clinic's: the composite foreign key onto
  `tenant_insurance_plans (tenant_id, catalog_id)` makes a doctor row for a
  plan the clinic did not enable unwritable in Postgres, and removing a plan
  from the clinic cascades it off every doctor. The hub validates the same
  rule first so the client gets a 422 rather than an IntegrityError.

`Tenant.insurances` (the legacy strings) is kept for one deploy cycle; see
services/insurance_catalog.py::sync_legacy_insurances for how the two stay in
step until the hub frontend reads only from here.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from secretaria.core.database import Base

# The closed set of `InsuranceCatalog.mechanism` values. `variavel_por_plano`
# and `desconhecido` are first-class answers, not failures: most operators sell
# lines with AND without reimbursement/coparticipation, and inventing one rule
# for all of them is exactly what the catalog must not do.
INSURANCE_MECHANISMS: tuple[str, ...] = (
    "reembolso",
    "coparticipacao",
    "desconto_direto",
    "variavel_por_plano",
    "desconhecido",
)


class InsuranceCatalog(Base):
    """One canonical convênio operator, shared by every tenant."""

    __tablename__ = "insurance_catalog"

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Stable machine key ("bradesco-saude"); the seed's identity, never shown.
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    # What patients see in the convênio list. <= 24 chars keeps it whole in a
    # WhatsApp list row (send_list truncates row titles there).
    name: Mapped[str] = mapped_column(String(120))
    # services/service_catalog.py::normalize(name) - the matching key for the
    # legacy-string migration and for the flow's typed answers.
    normalized_name: Mapped[str] = mapped_column(String(160), unique=True)
    mechanism: Mapped[str] = mapped_column(
        String(32), server_default=text("'desconhecido'"), default="desconhecido"
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class TenantInsurancePlan(Base):
    """A catalog entry this clinic accepts, with the clinic's deposit policy for it."""

    __tablename__ = "tenant_insurance_plans"
    __table_args__ = (
        # Also the target of professional_insurance_plans' composite FK.
        UniqueConstraint("tenant_id", "catalog_id", name="uq_tenant_insurance_plans"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    catalog_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("insurance_catalog.id", ondelete="CASCADE"), index=True
    )
    # False = a booking under this plan never gets a Pix deposit, whatever the
    # tenant's pix_deposit_* policy says (services/payments/deposit_lifecycle.py,
    # DEPOSIT_SKIP_INSURANCE_NO_CHARGE). Default True = today's behaviour: the
    # convênio changes nothing until the clinic unticks it for this plan.
    charge_deposit: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProfessionalInsurancePlan(Base):
    """A clinic plan this doctor accepts. Never a plan the clinic did not enable."""

    __tablename__ = "professional_insurance_plans"
    __table_args__ = (
        UniqueConstraint("professional_id", "catalog_id", name="uq_professional_insurance_plans"),
        # THE subset rule, in the schema: (tenant_id, catalog_id) must be a
        # plan the clinic accepts. CASCADE: unticking the plan at clinic level
        # unticks it for every doctor, in the same statement.
        ForeignKeyConstraint(
            ["tenant_id", "catalog_id"],
            ["tenant_insurance_plans.tenant_id", "tenant_insurance_plans.catalog_id"],
            ondelete="CASCADE",
            name="fk_professional_insurance_plans_tenant_plan",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    professional_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), index=True
    )
    # Denormalized from the professional so the composite FK above can pin the
    # plan to the SAME clinic; the hub always writes the professional's own.
    tenant_id: Mapped[uuid.UUID] = mapped_column(index=True)
    catalog_id: Mapped[uuid.UUID] = mapped_column()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
