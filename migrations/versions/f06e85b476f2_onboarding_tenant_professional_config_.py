"""Onboarding + multi-professional config (cross-service contract v1, secretaria §3).

Backs the brain-api-mediated onboarding flow and the per-professional config
round:

  - `tenants.phone_number_id` becomes NULLABLE: onboarding creates the tenant
    row (via the future internal bridge) BEFORE the WhatsApp number is
    connected. The plain unique constraint is replaced with a PARTIAL unique
    index (`postgresql_where phone_number_id IS NOT NULL`) so any number of
    not-yet-connected tenants can coexist, while a CONNECTED phone_number_id
    still cannot collide across tenants. See models/tenant.py's
    `__table_args__` for the SQLAlchemy-level definition (which additionally
    sets `sqlite_where` for the in-memory pytest engine - irrelevant to this
    migration, which only ever runs against Postgres, but kept identical so
    `Base.metadata` and the applied schema never drift apart).
  - `tenants` gains connection/mode/history-sync timestamps + address/
    insurance config columns (all additive, all nullable or defaulted -
    existing rows need no backfill for these).
  - `professionals` gains its own specialty/about/persona + business_hours/
    appointment_types columns (mirroring the tenant's legacy columns,
    NULL = "not configured, fall back to the tenant's" -
    services/tenant_config.py's `professional_business_hours` /
    `professional_appointment_types`).
  - New table `professional_credentials`: one encrypted Google refresh token
    per professional, additive to (never replacing) the tenant-level one in
    `tenant_credentials`.
  - DATA BACKFILL so every tenant keeps working unmodified through the
    professional-aware code paths added alongside this migration:
      1. Any tenant with zero professionals gets exactly one, named after
         `clinic_name` (the pre-existing single-professional/single-doctor
         product behaviour, now made explicit as a real row).
      2. Each tenant's single (or first-created, by `created_at` then `id`)
         professional has the tenant's CURRENT `business_hours` /
         `appointment_types` copied onto it wherever its own column is still
         NULL - a live materialization of the legacy fallback, not merely
         relying on the runtime helpers to paper over it.
    `tenants.business_hours` / `tenants.appointment_types` (and every other
    existing tenant column) are left untouched - they remain the legacy
    fallback for any professional whose own JSON is NULL.

Revision ID: f06e85b476f2
Revises: a4b5c6d7e8f9
Create Date: 2026-07-18 02:25:40.163571

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f06e85b476f2"
down_revision: str | None = "a4b5c6d7e8f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- tenants.phone_number_id: NOT NULL unique -> NULLABLE partial-unique ---
    # "tenants_phone_number_id_key" is Postgres's auto-generated name for the
    # unnamed `sa.UniqueConstraint('phone_number_id')` from the initial schema
    # (6c590bc4ee75) - confirmed against a fresh DB before writing this migration.
    op.drop_constraint("tenants_phone_number_id_key", "tenants", type_="unique")
    op.alter_column(
        "tenants",
        "phone_number_id",
        existing_type=sa.String(length=64),
        nullable=True,
    )
    op.create_index(
        "uq_tenants_phone_number_id_not_null",
        "tenants",
        ["phone_number_id"],
        unique=True,
        postgresql_where=sa.text("phone_number_id IS NOT NULL"),
    )

    # --- tenants: onboarding / connection / address+insurance columns ---
    op.add_column("tenants", sa.Column("connected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "tenants", sa.Column("mode_resolved_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tenants",
        sa.Column(
            "history_sync_status",
            sa.String(length=20),
            server_default="none",
            nullable=False,
        ),
    )
    op.add_column(
        "tenants", sa.Column("history_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("tenants", sa.Column("address", sa.JSON(), nullable=True))
    op.add_column("tenants", sa.Column("insurances", sa.JSON(), nullable=True))
    op.add_column(
        "tenants",
        sa.Column(
            "collect_insurance", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )

    # --- professionals: per-professional config columns ---
    op.add_column("professionals", sa.Column("specialty", sa.String(length=255), nullable=True))
    op.add_column("professionals", sa.Column("about", sa.Text(), nullable=True))
    op.add_column(
        "professionals", sa.Column("context_doctor_message", sa.Text(), nullable=True)
    )
    op.add_column("professionals", sa.Column("business_hours", sa.JSON(), nullable=True))
    op.add_column("professionals", sa.Column("appointment_types", sa.JSON(), nullable=True))

    # --- professional_credentials: encrypted Google refresh token, 1:1 ---
    op.create_table(
        "professional_credentials",
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("google_refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["professional_id"], ["professionals.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("professional_id"),
    )

    # --- data backfill ---
    conn = op.get_bind()

    # 1. Every tenant with zero professionals gets exactly one, named after
    #    the clinic - keeps the professional-aware code paths (which assume
    #    at least one row) working for tenants created before this feature.
    conn.execute(
        sa.text(
            "INSERT INTO professionals (id, tenant_id, name, is_active, created_at) "
            "SELECT gen_random_uuid(), t.id, t.clinic_name, true, now() "
            "FROM tenants t "
            "WHERE NOT EXISTS (SELECT 1 FROM professionals p WHERE p.tenant_id = t.id)"
        )
    )

    # 2. Materialize the tenant's current business_hours/appointment_types
    #    onto its single (or first-created) professional, wherever that
    #    professional's own column is still NULL. Targets exactly one
    #    professional per tenant (the earliest by created_at, tie-broken by
    #    id) - additional professionals stay NULL and rely on the live
    #    runtime fallback (services/tenant_config.py) until configured.
    conn.execute(
        sa.text(
            "UPDATE professionals p "
            "SET business_hours = COALESCE(p.business_hours, t.business_hours), "
            "    appointment_types = COALESCE(p.appointment_types, t.appointment_types) "
            "FROM tenants t "
            "WHERE p.tenant_id = t.id "
            "  AND p.id = ( "
            "      SELECT p2.id FROM professionals p2 "
            "      WHERE p2.tenant_id = t.id "
            "      ORDER BY p2.created_at ASC, p2.id ASC "
            "      LIMIT 1 "
            "  ) "
            "  AND (p.business_hours IS NULL OR p.appointment_types IS NULL)"
        )
    )


def downgrade() -> None:
    # Schema-only reversal: the professionals auto-created above (tenants
    # that had zero) are NOT deleted, and the business_hours/appointment_types
    # values materialized onto the first professional are lost along with the
    # columns themselves - same as any other column drop in this repo. This
    # mirrors the rest of the migration history (e.g. a4b5c6d7e8f9's downgrade
    # drops pix_key/ehr_provider outright); only d7e8f9a0b1c2 restores data on
    # downgrade, because that migration MOVED a still-live secret rather than
    # additively backfilling a default.
    op.drop_table("professional_credentials")

    op.drop_column("professionals", "appointment_types")
    op.drop_column("professionals", "business_hours")
    op.drop_column("professionals", "context_doctor_message")
    op.drop_column("professionals", "about")
    op.drop_column("professionals", "specialty")

    op.drop_column("tenants", "collect_insurance")
    op.drop_column("tenants", "insurances")
    op.drop_column("tenants", "address")
    op.drop_column("tenants", "history_synced_at")
    op.drop_column("tenants", "history_sync_status")
    op.drop_column("tenants", "mode_resolved_at")
    op.drop_column("tenants", "connected_at")

    op.drop_index("uq_tenants_phone_number_id_not_null", table_name="tenants")
    # Fails loudly (does not silently drop rows) if any tenant now holds a
    # NULL phone_number_id - e.g. one provisioned by onboarding and not yet
    # WhatsApp-connected. Resolve those (or delete them) before downgrading.
    op.alter_column(
        "tenants",
        "phone_number_id",
        existing_type=sa.String(length=64),
        nullable=False,
    )
    op.create_unique_constraint("tenants_phone_number_id_key", "tenants", ["phone_number_id"])
