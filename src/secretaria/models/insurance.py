"""Convênio (health-insurance plan) catalog: global list + per-clinic + per-doctor.

Until these tables existed, a clinic's convênios were a JSON list of free
strings on `Tenant.insurances`, typed by hand in the hub. "Unimed", "unimed" and
"Unimed BH" were three plans, and nothing could say which DOCTOR takes which
plan - so the booking flow could only ever record the answer, never use it.

TASK-006 built three tables (global catalog, per-clinic plans, per-doctor
plans). TASK-008 extends them with three ideas the owner asked for after
reviewing that round:

- **`Tenant.insurance_mode`** (models/tenant.py) - the clinic chooses one of
  three acceptance topologies, no silent default. See that column's docstring.
- **Extensible catalog**: `InsuranceCatalog.aliases` lets an admin register the
  variant spellings a clinic's free-text legacy strings actually used, and
  `insurance_catalog_unmatched` (this module) tracks which of those strings
  still have no match, so adding a catalog row can reconcile them without a
  code migration.
- **Convênio "Outro"**: a clinic (or, in `independent` mode, a professional)
  can register a plan outside the catalog - `custom_name` +
  `custom_payment_note`, shown to the patient in place of catalog metadata.

Three tables, three levels:

- `insurance_catalog` - GLOBAL, shared by every tenant (owner's decision,
  2026-09-23: "catálogo... select dinâmico"). The canonical list of Brazilian
  operators, seeded by migration with a payment `mechanism` + `note` whose
  provenance lives in docs/CHECKPOINT_convenio_catalogo.md. Metadata only: it
  never filters or blocks anything and is not a promise about any contract.
  `aliases` are extra normalized strings (TASK-008) that also count as a match
  for this entry - the admin catalog endpoint accepts them so an operator can
  register "GEAP Saúde" as an alias of the "GEAP" row without another entry.
- `tenant_insurance_plans` - which catalog entries THIS clinic accepts, plus
  the clinic's own `charge_deposit` flag per plan (decision 2026-09-25: the
  same operator can be direct billing in one clinic and reimbursement in
  another, so the Pix-deposit behaviour is per clinic AND per plan, never
  global and never inferred from the operator's name). TASK-008: `catalog_id`
  is now nullable - a row with `custom_name`/`custom_payment_note` instead is
  the clinic's own "Outro" convênio, exactly one of the two must be set.
- `professional_insurance_plans` - what ONE doctor accepts. TASK-008 gives it
  three mutually-exclusive shapes (`ck_professional_insurance_plans_exactly_one_source`),
  because `Tenant.insurance_mode` changes what "a doctor's plan" even refers to:
  - `tenant_plan_id` set: modes `shared`/`clinic_with_exceptions` - "I accept
    THIS row of the clinic's own list" (catalog-backed or the clinic's
    "Outro"). FK'd straight at `tenant_insurance_plans.id`, so removing that
    row from the clinic still cascades off every doctor, exactly like the
    TASK-006 composite FK did - the difference is this FK no longer tries to
    ALSO be the enforcement mechanism for `independent` mode (see below).
  - `catalog_id` set: mode `independent` - the doctor picked straight from the
    GLOBAL catalog, with no requirement that the clinic enabled it too (an
    independent doctor's list is the whole catalog, not a clinic subset).
  - `custom_name` set: either mode - the doctor's OWN "Outro" convênio
    (`independent`), never linked to any clinic row.

  TASK-006's composite FK onto `(tenant_insurance_plans.tenant_id, catalog_id)`
  is GONE: it encoded "a doctor's plan is always a subset of the clinic's",
  which stopped being universally true the moment `independent` mode could let
  a doctor pick a plan the clinic never enabled. The subset rule now applies
  ONLY to `clinic_with_exceptions` (enforced in services/insurance_catalog.py,
  same 422-before-IntegrityError shape TASK-006 used) - `shared` never lets a
  doctor pick at all, and `independent` has no subset to enforce.
- `insurance_catalog_unmatched` - one row per (tenant, distinct legacy string)
  that has never matched any catalog entry. Populated by the TASK-006 backfill
  and by `sync_legacy_insurances`' unmatched branch; resolved automatically by
  `reconcile_unmatched_insurances` the moment an admin adds (or extends the
  aliases of) a catalog row that matches it - see services/insurance_catalog.py.
  This is the "actionable" home for what used to be only a log line
  (`insurance_migration_unmatched`), which keeps existing for audit purposes.

`Tenant.insurances` (the legacy strings) is kept for one deploy cycle; see
services/insurance_catalog.py::sync_legacy_insurances for how the two stay in
step until the hub frontend reads only from here.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
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

# The three acceptance topologies a clinic can choose for `Tenant.insurance_mode`
# (TASK-008 §2/§3.1). NULL (not one of these) means "not chosen yet" and is a
# first-class fourth state everywhere this tuple is consulted - never a
# default, per the owner's explicit decision.
INSURANCE_MODES: tuple[str, ...] = ("shared", "clinic_with_exceptions", "independent")

# Portable "exactly one of these columns is set" CHECK, written with CASE/SUM
# instead of a boolean cast so it works unmodified on both Postgres (real
# deployments/migrations) and SQLite (the test suite's `create_all` engine -
# see docs/CHECKPOINT_convenio_catalogo.md's note on dialect portability).
_EXACTLY_ONE_OF_TWO = (
    "(CASE WHEN catalog_id IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN custom_name IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_EXACTLY_ONE_OF_THREE = (
    "(CASE WHEN tenant_plan_id IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN catalog_id IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN custom_name IS NOT NULL THEN 1 ELSE 0 END) = 1"
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
    # TASK-008: extra normalized spellings that also match this entry (e.g. a
    # clinic's old free-text "GEAP Saúde" matching a canonical "GEAP" row).
    # Stored as typed strings (not pre-normalized) so the admin screen can show
    # back what the operator typed; matching always re-normalizes via
    # services/insurance_catalog.py::catalog_match_keys.
    aliases: Mapped[list] = mapped_column(JSON, server_default=text("'[]'"), default=list)
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
    """A convênio this clinic accepts: a catalog entry, or the clinic's own "Outro".

    `charge_deposit` works identically for both shapes (TASK-008 §3.3): it is a
    property of THIS CLINIC'S line, not of the catalog operator.
    """

    __tablename__ = "tenant_insurance_plans"
    __table_args__ = (
        # Also the target of professional_insurance_plans.tenant_plan_id.
        UniqueConstraint("tenant_id", "catalog_id", name="uq_tenant_insurance_plans"),
        CheckConstraint(_EXACTLY_ONE_OF_TWO, name="ck_tenant_insurance_plans_exactly_one_source"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # NULL for the clinic's own "Outro" (custom_name set instead) - TASK-008.
    catalog_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("insurance_catalog.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # The clinic's own convênio name, outside the catalog. Exactly one of
    # (catalog_id, custom_name) is set - see the CHECK constraint above.
    custom_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    # How the clinic says payment works for this custom convênio. Shown to the
    # patient at booking confirmation, and again with the Pix-deposit ask when
    # charge_deposit is true (services/payments/deposit_lifecycle.py).
    custom_payment_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # False = a booking under this plan never gets a Pix deposit, whatever the
    # tenant's pix_deposit_* policy says (services/payments/deposit_lifecycle.py,
    # DEPOSIT_SKIP_INSURANCE_NO_CHARGE). Default True = today's behaviour: the
    # convênio changes nothing until the clinic unticks it for this plan.
    charge_deposit: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProfessionalInsurancePlan(Base):
    """What ONE doctor accepts. Shape depends on `Tenant.insurance_mode` — see
    the module docstring for the three mutually-exclusive column groups.
    """

    __tablename__ = "professional_insurance_plans"
    __table_args__ = (
        UniqueConstraint(
            "professional_id", "tenant_plan_id", name="uq_professional_insurance_plans_tenant_plan"
        ),
        UniqueConstraint(
            "professional_id", "catalog_id", name="uq_professional_insurance_plans_catalog"
        ),
        CheckConstraint(
            _EXACTLY_ONE_OF_THREE, name="ck_professional_insurance_plans_exactly_one_source"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    professional_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("professionals.id", ondelete="CASCADE"), index=True
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    # modes shared/clinic_with_exceptions: which of the CLINIC's own rows (catalog
    # or custom) this doctor accepts. CASCADEs off every doctor when the clinic
    # removes that row - the same behaviour TASK-006's composite FK gave.
    tenant_plan_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant_insurance_plans.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # mode independent: a direct pick from the GLOBAL catalog - no clinic
    # enablement required.
    catalog_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("insurance_catalog.id", ondelete="CASCADE"), nullable=True
    )
    # mode independent: this doctor's OWN "Outro" convênio.
    custom_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    custom_payment_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Meaningful only for the two `independent`-mode shapes (catalog_id /
    # custom_name): there is no clinic-level charge_deposit to inherit for a
    # plan the clinic itself never enabled. Ignored for tenant_plan_id rows,
    # which read the CLINIC's own charge_deposit instead (single source of
    # truth for modes shared/clinic_with_exceptions).
    charge_deposit: Mapped[bool] = mapped_column(Boolean, server_default=text("true"), default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class InsuranceCatalogUnmatched(Base):
    """A clinic's legacy convênio text that has never matched a catalog entry.

    The actionable half of what TASK-006 only logged
    (`insurance_migration_unmatched`/`insurance_legacy_unmatched`): as long as a
    row here has `resolved_at IS NULL`, `raw_text` is still being offered to
    that clinic's patients as an id-less, unmarkable option
    (services/insurance_catalog.py::load_tenant_insurance). The moment an admin
    adds a catalog entry (or alias) that matches it,
    `reconcile_unmatched_insurances` grants the clinic a real
    `tenant_insurance_plans` row and stamps `resolved_at`/
    `resolved_insurance_catalog_id` here - the row is kept (not deleted) as a
    resolved audit trail, mirroring how `resolved_at` is used elsewhere in this
    repo (e.g. booking_hold.py).
    """

    __tablename__ = "insurance_catalog_unmatched"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "normalized_text", name="uq_insurance_catalog_unmatched_tenant_text"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    raw_text: Mapped[str] = mapped_column(String(255))
    # services/service_catalog.py::normalize(raw_text) - the same key
    # reconciliation matches on, and what makes the uniqueness constraint above
    # dedupe "Geap", "GEAP" and " geap " as the one candidate they are.
    normalized_text: Mapped[str] = mapped_column(String(255), index=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_insurance_catalog_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("insurance_catalog.id", ondelete="SET NULL"), nullable=True
    )
