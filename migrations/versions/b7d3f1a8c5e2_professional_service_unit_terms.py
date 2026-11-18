"""professional_service_unit_terms: price/duration exceptions per professional x service x unit.

TASK-085 (price and duration per service x professional x unit). Only EXCEPTIONS are stored: no
row = the professional's base price/duration (today's behaviour, nothing to backfill); a row with
only a price (or only a duration) inherits the other half from the base.

    CREATE TABLE professional_service_unit_terms (
        professional_id UUID  FK -> professionals.id ON DELETE CASCADE,
        service_id      UUID  FK -> services.id      ON DELETE CASCADE,
        unit_id         UUID  FK -> units.id         ON DELETE CASCADE,
        tenant_id       UUID  FK -> tenants.id       ON DELETE CASCADE,
        price           VARCHAR(40) NULL,
        duration_min    INTEGER NULL,
        created_at / updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (professional_id, service_id, unit_id),
        CHECK (price IS NOT NULL OR duration_min IS NOT NULL),
        CHECK (duration_min IS NULL OR (duration_min > 0 AND duration_min <= 600))
    )

ADDITIVE: a new table nothing reads until the new code ships, so the database can move first
(`alembic upgrade head` from the NEW image, both services still on the old code) and then
`secretaria_api` AND `secretaria-worker` are deployed together.

Rollback: the OLD code on both services first, then `alembic downgrade` to a4e7c2f9d6b1. Only the
per-unit prices/durations are lost (every unit goes back to the professional's base value).

Revision ID: b7d3f1a8c5e2
Revises: a4e7c2f9d6b1
Create Date: 2026-11-16
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7d3f1a8c5e2"
down_revision: str | None = "a4e7c2f9d6b1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "professional_service_unit_terms"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("price", sa.String(length=40), nullable=True),
        sa.Column("duration_min", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["unit_id"], ["units.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("professional_id", "service_id", "unit_id"),
        sa.CheckConstraint(
            "price IS NOT NULL OR duration_min IS NOT NULL", name="ck_psut_price_or_duration"
        ),
        sa.CheckConstraint(
            "duration_min IS NULL OR (duration_min > 0 AND duration_min <= 600)",
            name="ck_psut_duration_range",
        ),
    )
    op.create_index(f"ix_{TABLE}_service_id", TABLE, ["service_id"])
    op.create_index(f"ix_{TABLE}_unit_id", TABLE, ["unit_id"])
    op.create_index(f"ix_{TABLE}_tenant_id", TABLE, ["tenant_id"])


def downgrade() -> None:
    op.drop_index(f"ix_{TABLE}_tenant_id", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_unit_id", table_name=TABLE)
    op.drop_index(f"ix_{TABLE}_service_id", table_name=TABLE)
    op.drop_table(TABLE)
