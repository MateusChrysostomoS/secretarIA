"""insurance mode + extensible catalog + convênio "Outro"

TASK-008 (docs/CHECKPOINT_convenio_catalogo.md). Purely additive, widen-before-
narrow (skill frozen-contract-migration): every new column is nullable with no
backfilled value (except the two boolean flags below, which default to
today's behaviour), and the one FK whose TARGET changes
(`appointments.insurance_plan_id`) is re-pointed at a table that already holds
one row per catalog id it used to reference — see `_repoint_insurance_plan_id`.

What this adds, and why (full rationale: models/insurance.py, models/tenant.py):

- `tenants.insurance_mode` — nullable enum, NO default for ANY tenant (owner's
  explicit decision, not reopened here). While NULL, the deterministic flow
  skips the convênio question, same as an empty catalog does today.
- `insurance_catalog.aliases` — JSON array of extra spellings that also match
  this entry (the extensible-catalog admin endpoint).
- `insurance_catalog_unmatched` — one row per (tenant, distinct legacy string)
  that has never matched a catalog entry. Backfilled below by RE-DERIVING from
  `tenants.insurances` against the CURRENT catalog (not by copying the
  migration-time log from c9f4e2a7b815), so it also captures anything that
  became unmatched later via `sync_legacy_insurances` — a superset of what the
  original backfill logged, which is the correct, more complete state.
- `tenant_insurance_plans.catalog_id` → nullable, `+custom_name`/
  `custom_payment_note` — the clinic's "Outro" convênio.
- `professional_insurance_plans` — TASK-006's composite FK onto
  `(tenant_insurance_plans.tenant_id, catalog_id)` is DROPPED: it encoded "a
  doctor's plan is always a subset of the clinic's", which stops being true
  the moment `independent` mode lets a doctor pick straight from the global
  catalog. Replaced by `tenant_plan_id` (FK straight at
  `tenant_insurance_plans.id`, used by shared/clinic_with_exceptions),
  `catalog_id` (now a plain FK at `insurance_catalog.id`, used by
  independent), `custom_name`/`custom_payment_note` (a doctor's own "Outro"),
  and `charge_deposit`. Exactly one of the three "which plan" columns is set
  per row (CHECK constraint) — the subset-of-the-clinic rule for
  clinic_with_exceptions moves to the application layer
  (services/insurance_catalog.py), matching how independent's "any catalog
  entry" and shared's "no per-doctor row at all" already have to be enforced
  there.
- `professionals.insurance_plans_customized` — distinguishes "never touched
  the picker" (inherit all clinic plans) from "explicitly saved an empty set"
  (accept nothing), which zero rows in `professional_insurance_plans` cannot
  tell apart on its own.
- `appointments.insurance_plan_id` is RE-POINTED from `insurance_catalog.id`
  to `tenant_insurance_plans.id` (the clinic's own row is the only identifier
  that exists for both catalog-backed AND custom clinic plans), and
  `appointments.insurance_professional_plan_id` is added for bookings made
  under `independent` mode, where there is no clinic row to point at.

Revision ID: d4f8a2c6e913
Revises: c9f4e2a7b815
Create Date: 2026-09-26
"""

import re
import unicodedata
import uuid
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8a2c6e913"
down_revision: str | None = "c9f4e2a7b815"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_WHITESPACE = re.compile(r"\s+")

_EXACTLY_ONE_OF_TWO = (
    "(CASE WHEN catalog_id IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN custom_name IS NOT NULL THEN 1 ELSE 0 END) = 1"
)
_EXACTLY_ONE_OF_THREE = (
    "(CASE WHEN tenant_plan_id IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN catalog_id IS NOT NULL THEN 1 ELSE 0 END + "
    "CASE WHEN custom_name IS NOT NULL THEN 1 ELSE 0 END) = 1"
)


def _normalize(name: str | None) -> str:
    text_value = _WHITESPACE.sub(" ", (name or "").strip())
    decomposed = unicodedata.normalize("NFKD", text_value)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def upgrade() -> None:
    op.add_column("tenants", sa.Column("insurance_mode", sa.String(length=32), nullable=True))
    op.create_check_constraint(
        "ck_tenants_insurance_mode",
        "tenants",
        "insurance_mode IS NULL OR insurance_mode IN "
        "('shared', 'clinic_with_exceptions', 'independent')",
    )

    op.add_column(
        "insurance_catalog",
        sa.Column("aliases", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )

    op.create_table(
        "insurance_catalog_unmatched",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("raw_text", sa.String(length=255), nullable=False),
        sa.Column("normalized_text", sa.String(length=255), nullable=False),
        sa.Column(
            "first_seen_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "resolved_insurance_catalog_id",
            sa.Uuid(),
            sa.ForeignKey("insurance_catalog.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint(
            "tenant_id", "normalized_text", name="uq_insurance_catalog_unmatched_tenant_text"
        ),
    )
    op.create_index(
        "ix_insurance_catalog_unmatched_tenant_id", "insurance_catalog_unmatched", ["tenant_id"]
    )
    op.create_index(
        "ix_insurance_catalog_unmatched_normalized_text",
        "insurance_catalog_unmatched",
        ["normalized_text"],
    )

    # --- tenant_insurance_plans: catalog_id nullable + custom columns -------
    with op.batch_alter_table("tenant_insurance_plans") as batch:
        batch.alter_column("catalog_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(sa.Column("custom_name", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("custom_payment_note", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_tenant_insurance_plans_exactly_one_source", "tenant_insurance_plans", _EXACTLY_ONE_OF_TWO
    )

    # --- professional_insurance_plans: drop the TASK-006 composite FK, add the
    # three-shape columns -----------------------------------------------------
    op.drop_constraint(
        "fk_professional_insurance_plans_tenant_plan",
        "professional_insurance_plans",
        type_="foreignkey",
    )
    with op.batch_alter_table("professional_insurance_plans") as batch:
        batch.alter_column("catalog_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(
            sa.Column(
                "tenant_plan_id",
                sa.Uuid(),
                sa.ForeignKey("tenant_insurance_plans.id", ondelete="CASCADE"),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("custom_name", sa.String(length=120), nullable=True))
        batch.add_column(sa.Column("custom_payment_note", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("charge_deposit", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch.create_foreign_key(
            "fk_professional_insurance_plans_catalog_id",
            "insurance_catalog",
            ["catalog_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_foreign_key(
            "fk_professional_insurance_plans_tenant_id",
            "tenants",
            ["tenant_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch.create_unique_constraint(
            "uq_professional_insurance_plans_tenant_plan", ["professional_id", "tenant_plan_id"]
        )
    op.create_index(
        "ix_professional_insurance_plans_tenant_plan_id",
        "professional_insurance_plans",
        ["tenant_plan_id"],
    )
    # The OLD unique constraint (professional_id, catalog_id) already exists
    # from c9f4e2a7b815 under the name uq_professional_insurance_plans and
    # keeps working unchanged (catalog_id is now nullable, Postgres treats
    # each NULL as distinct, which is what independent-mode custom rows need).
    op.create_check_constraint(
        "ck_professional_insurance_plans_exactly_one_source",
        "professional_insurance_plans",
        _EXACTLY_ONE_OF_THREE,
    )

    op.add_column(
        "professionals",
        sa.Column(
            "insurance_plans_customized", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )

    # --- appointments: re-point insurance_plan_id, add the professional twin -
    _repoint_insurance_plan_id()
    op.add_column(
        "appointments",
        sa.Column(
            "insurance_professional_plan_id",
            sa.Uuid(),
            sa.ForeignKey(
                "professional_insurance_plans.id",
                ondelete="SET NULL",
                name="fk_appointments_insurance_professional_plan_id",
            ),
            nullable=True,
        ),
    )

    _backfill_unmatched_insurance_strings()


def _repoint_insurance_plan_id() -> None:
    """Re-target `appointments.insurance_plan_id` at `tenant_insurance_plans.id`.

    It used to point at `insurance_catalog.id`. Every existing non-NULL value
    is a catalog id for a plan the SAME tenant had enabled (that is the only
    way `resolve_tenant_plan_id` could ever have written one) — so the row it
    should now point at is `tenant_insurance_plans` WHERE (tenant_id, catalog_id)
    matches the appointment's own tenant + old value. A booking whose tenant no
    longer has that plan enabled (removed after the appointment was made) has
    no such row anymore; those go to NULL, matching what the deposit guard
    already treats as "no plan on file" (falls back to the tenant's own
    pix_deposit_* policy — never a behaviour change for a live appointment,
    since the plan being gone means the clinic already stopped offering it).
    """
    bind = op.get_bind()
    # Drop the OLD FK constraint first: a rename keeps the constraint (and its
    # name) attached to the renamed column, and the new column below reuses
    # that exact name — Postgres refuses two constraints with the same name on
    # one table.
    op.drop_constraint("fk_appointments_insurance_plan_id", "appointments", type_="foreignkey")
    op.alter_column(
        "appointments", "insurance_plan_id", new_column_name="_insurance_catalog_id_old"
    )
    op.add_column(
        "appointments",
        sa.Column(
            "insurance_plan_id",
            sa.Uuid(),
            sa.ForeignKey(
                "tenant_insurance_plans.id",
                ondelete="SET NULL",
                name="fk_appointments_insurance_plan_id",
            ),
            nullable=True,
        ),
    )
    bind.execute(
        sa.text(
            "UPDATE appointments AS a"
            " SET insurance_plan_id = tip.id"
            " FROM tenant_insurance_plans AS tip"
            " WHERE tip.tenant_id = a.tenant_id"
            "   AND tip.catalog_id = a._insurance_catalog_id_old"
            "   AND a._insurance_catalog_id_old IS NOT NULL"
        )
    )
    with op.batch_alter_table("appointments") as batch:
        batch.drop_column("_insurance_catalog_id_old")


def _backfill_unmatched_insurance_strings() -> None:
    bind = op.get_bind()
    catalog_rows = bind.execute(sa.text("SELECT name, aliases FROM insurance_catalog")).all()
    known: set[str] = set()
    for name, aliases in catalog_rows:
        known.add(_normalize(name))
        for alias in aliases or []:
            known.add(_normalize(str(alias)))

    tenants = bind.execute(
        sa.text("SELECT id, insurances FROM tenants WHERE insurances IS NOT NULL")
    ).all()
    for tenant_id, raw in tenants:
        values = raw if isinstance(raw, list) else []
        seen: set[str] = set()
        for value in values:
            raw_text = str(value).strip()
            key = _normalize(raw_text)
            if not key or key in known or key in seen:
                continue
            seen.add(key)
            bind.execute(
                sa.text(
                    "INSERT INTO insurance_catalog_unmatched"
                    " (id, tenant_id, raw_text, normalized_text)"
                    " VALUES (:id, :t, :raw, :norm)"
                    " ON CONFLICT (tenant_id, normalized_text) DO NOTHING"
                )
                if bind.dialect.name == "postgresql"
                else sa.text(
                    "INSERT OR IGNORE INTO insurance_catalog_unmatched"
                    " (id, tenant_id, raw_text, normalized_text)"
                    " VALUES (:id, :t, :raw, :norm)"
                ),
                {"id": uuid.uuid4(), "t": tenant_id, "raw": raw_text[:255], "norm": key[:255]},
            )


def downgrade() -> None:
    op.drop_column("appointments", "insurance_professional_plan_id")
    _unrepoint_insurance_plan_id()

    op.drop_column("professionals", "insurance_plans_customized")

    op.drop_constraint(
        "ck_professional_insurance_plans_exactly_one_source",
        "professional_insurance_plans",
        type_="check",
    )
    op.drop_index(
        "ix_professional_insurance_plans_tenant_plan_id", table_name="professional_insurance_plans"
    )
    with op.batch_alter_table("professional_insurance_plans") as batch:
        batch.drop_constraint(
            "uq_professional_insurance_plans_tenant_plan", type_="unique"
        )
        batch.drop_constraint("fk_professional_insurance_plans_tenant_id", type_="foreignkey")
        batch.drop_constraint("fk_professional_insurance_plans_catalog_id", type_="foreignkey")
        batch.drop_column("charge_deposit")
        batch.drop_column("custom_payment_note")
        batch.drop_column("custom_name")
        batch.drop_column("tenant_plan_id")
        # NOTE: rows created under independent/custom modes (catalog_id
        # pointing outside the clinic's own plans, or custom_name rows) make
        # the ORIGINAL composite FK impossible to re-add here without first
        # deleting them. This downgrade is a dev/test convenience, never run
        # against a database that has real independent-mode data — see
        # frozen-contract-migration skill: reverting past a narrowing step is
        # not always symmetric.
        batch.alter_column("catalog_id", existing_type=sa.Uuid(), nullable=False)
        batch.create_foreign_key(
            "fk_professional_insurance_plans_tenant_plan",
            "tenant_insurance_plans",
            ["tenant_id", "catalog_id"],
            ["tenant_id", "catalog_id"],
            ondelete="CASCADE",
        )

    op.drop_constraint(
        "ck_tenant_insurance_plans_exactly_one_source", "tenant_insurance_plans", type_="check"
    )
    with op.batch_alter_table("tenant_insurance_plans") as batch:
        batch.drop_column("custom_payment_note")
        batch.drop_column("custom_name")
        batch.alter_column("catalog_id", existing_type=sa.Uuid(), nullable=False)

    op.drop_index(
        "ix_insurance_catalog_unmatched_normalized_text", table_name="insurance_catalog_unmatched"
    )
    op.drop_index(
        "ix_insurance_catalog_unmatched_tenant_id", table_name="insurance_catalog_unmatched"
    )
    op.drop_table("insurance_catalog_unmatched")

    op.drop_column("insurance_catalog", "aliases")

    op.drop_constraint("ck_tenants_insurance_mode", "tenants", type_="check")
    op.drop_column("tenants", "insurance_mode")


def _unrepoint_insurance_plan_id() -> None:
    """Best-effort reverse of `_repoint_insurance_plan_id` for dev/test only.

    Maps each `tenant_insurance_plans.id` back to its `catalog_id`; a custom
    (catalog-less) plan has no catalog id to go back to and downgrades to NULL
    — an accepted, documented lossy step for a downgrade path that is never
    run against a database holding TASK-008 data for real.
    """
    bind = op.get_bind()
    op.drop_constraint("fk_appointments_insurance_plan_id", "appointments", type_="foreignkey")
    op.alter_column("appointments", "insurance_plan_id", new_column_name="_tenant_plan_id_old")
    op.add_column(
        "appointments",
        sa.Column(
            "insurance_plan_id",
            sa.Uuid(),
            sa.ForeignKey(
                "insurance_catalog.id", ondelete="SET NULL", name="fk_appointments_insurance_plan_id"
            ),
            nullable=True,
        ),
    )
    bind.execute(
        sa.text(
            "UPDATE appointments AS a"
            " SET insurance_plan_id = tip.catalog_id"
            " FROM tenant_insurance_plans AS tip"
            " WHERE tip.id = a._tenant_plan_id_old"
            "   AND a._tenant_plan_id_old IS NOT NULL"
        )
    )
    with op.batch_alter_table("appointments") as batch:
        batch.drop_column("_tenant_plan_id_old")
