"""The clinic's one convênio order in the per-doctor mode: one new table, additive (TASK-090).

In `independent` mode there is no clinic-level plan row, so the order the patient sees (and the
doctor matrix's columns follow) lives in its own table:

    CREATE TABLE insurance_plan_order (
        tenant_id UUID REFERENCES tenants(id) ON DELETE CASCADE,
        plan_key  VARCHAR(80),   -- "catalog:<catalog id>" | "custom:<professional plan row id>"
        position  INTEGER NOT NULL,
        PRIMARY KEY (tenant_id, plan_key)
    )

No backfill: an empty table = every plan never placed = today's alphabetical list.
Written only by `PUT /tenants/me/professionals/{id}/insurance-acceptance/plan-order`.

DEPLOY ORDER - the database moves FIRST (the new code reads the table):
    1. `alembic upgrade` to this revision from the NEW image, both services still old (the old
       code never reads the table);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. then the Brain-Message front (dragging the matrix's columns).

Rollback: old code on both services first, then `alembic downgrade` (drops the table).

Revision ID: a3c9e5d7f1b4
Revises: f2b8d4c6e0a3
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a3c9e5d7f1b4"
down_revision: str | None = "f2b8d4c6e0a3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "insurance_plan_order",
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("plan_key", sa.String(length=80), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("tenant_id", "plan_key"),
    )


def downgrade() -> None:
    op.drop_table("insurance_plan_order")
