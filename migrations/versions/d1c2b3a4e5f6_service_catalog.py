"""canonical service catalog (services table)

The clinic's services stop being free strings scattered across
`professionals.appointment_types` (and the legacy `tenants.appointment_types`)
and gain a stable identity — see models/service.py and
docs/CHECKPOINT_service_catalog.md.

This migration is DELIVERY 1 of three and is strictly ADDITIVE:

  - it creates ONE new table and touches nothing else;
  - it writes no rows and rewrites no JSON. Every existing reader keeps
    reading exactly what it reads today, because a tenant with an empty
    catalog resolves to its raw stored entries
    (services/service_catalog.py::resolve_entries);
  - populating the catalog and stamping `service_id` onto the existing JSON
    entries is a SEPARATE, opt-in, per-tenant step
    (scripts/backfill_service_catalog.py), never something a deploy does;
  - dropping the now-duplicated descriptive fields from the JSON is delivery 3.

`downgrade()` therefore loses nothing that existed before `upgrade()`: the
per-professional lists are still the source of truth at this stage. (It does
drop catalog rows created after the fact — which is why the backfill is a
separate step you can decide to run, or not, per tenant.)

`UNIQUE (tenant_id, normalized_name)` is the point of the whole feature: two
spellings of one service cannot coexist in a clinic, and no clinic can ever
see another's catalog.

Revision ID: d1c2b3a4e5f6
Revises: c9a1e2f4b6d8
Create Date: 2026-08-17 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d1c2b3a4e5f6"
down_revision: str | None = "c9a1e2f4b6d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "services",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=120), nullable=False),
        # Derived from `name` by services/service_catalog.py::normalize (trim,
        # collapse whitespace, strip accents, casefold). Stored rather than
        # expressed as a functional index so the identity rule is visible in
        # the schema and behaves identically on SQLite in tests.
        sa.Column("normalized_name", sa.String(length=160), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("long_description", sa.Text(), nullable=True),
        sa.Column("requirements", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("tenant_id", "normalized_name", name="uq_services_tenant_normalized"),
    )
    op.create_index("ix_services_tenant_id", "services", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_services_tenant_id", table_name="services")
    op.drop_table("services")
