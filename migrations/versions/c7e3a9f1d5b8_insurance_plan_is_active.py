"""An ON/OFF switch for a convênio the clinic typed by hand: one NOT NULL column per plan table.

TASK-090 Adendo 1, contract C7 (owner, 2026-11-24): a clinic's (or, in the per-doctor mode, a
doctor's) own "Outro convênio" row can be switched OFF without losing it - the row, its name and
its payment rule stay; the convênio only stops being offered to patients and to the AI. Turning
it back ON restores it. Catalog rows never use the switch (OFF still deletes them), so they are
always `true`.

    ALTER TABLE tenant_insurance_plans       ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true
    ALTER TABLE professional_insurance_plans ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT true

Additive: every existing row reads `true` (= today's behaviour). Written only by
`PUT /tenants/me/insurance-plans/{plan_id}/active` and
`PUT /tenants/me/professionals/{professional_id}/insurance-plans/{plan_id}/active`.

DEPLOY ORDER - the database moves FIRST (the new code selects the column):
    1. `alembic upgrade head` from the NEW image, both services still old (the old code never
       reads the column, and the default keeps its inserts valid);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. then the Brain-Message front (the ON/OFF switch on the "Outro" convênio card).

Rollback: old code on both services first, then `alembic downgrade` (drops both columns; a
switched-OFF row comes back ON).

Revision ID: c7e3a9f1d5b8
Revises: a3c9e5d7f1b4
Create Date: 2026-11-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e3a9f1d5b8"
down_revision: str | None = "a3c9e5d7f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ("tenant_insurance_plans", "professional_insurance_plans")


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        )


def downgrade() -> None:
    for table in TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("is_active")
