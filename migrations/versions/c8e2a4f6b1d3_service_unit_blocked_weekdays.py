"""professional_service_unit_terms.blocked_weekdays: weekdays a service cannot be booked, per unit.

TASK-087 (weekdays per service). The professional's BASE list lives in the JSON entry of
`appointment_types` (no schema change, nothing to backfill); this adds the per-unit EXCEPTION on
the row that already carries the unit's price/duration exceptions (TASK-085):

    ALTER TABLE professional_service_unit_terms ADD COLUMN blocked_weekdays JSON NULL;
    -- NULL = inherit the base list, [] = own (nothing blocked), list = own
    -- ck_psut_price_or_duration: price IS NOT NULL OR duration_min IS NOT NULL
    --                            OR blocked_weekdays IS NOT NULL   (a days-only row is valid)

ADDITIVE for the old code: an old reader ignores the new column and never writes a days-only row,
so the database can move first (`alembic upgrade head` from the NEW image, both services still on
the old code) and then `secretaria_api` AND `secretaria-worker` are deployed together.

Rollback: the OLD code on both services first, then `alembic downgrade` to f1c4b8d2a6e3. Rows that
carry ONLY days are deleted (the old CHECK would refuse them); every blocked-weekday setting of the
unit exceptions is lost, rows with a price/duration keep those.

Revision ID: c8e2a4f6b1d3
Revises: f1c4b8d2a6e3
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c8e2a4f6b1d3"
down_revision: str | None = "f1c4b8d2a6e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "professional_service_unit_terms"
CHECK = "ck_psut_price_or_duration"


def upgrade() -> None:
    with op.batch_alter_table(TABLE) as batch:
        batch.add_column(sa.Column("blocked_weekdays", sa.JSON(), nullable=True))
        batch.drop_constraint(CHECK, type_="check")
        batch.create_check_constraint(
            CHECK,
            "price IS NOT NULL OR duration_min IS NOT NULL OR blocked_weekdays IS NOT NULL",
        )


def downgrade() -> None:
    # Rows with only days would violate the old CHECK: drop them before restoring it.
    op.execute(f"DELETE FROM {TABLE} WHERE price IS NULL AND duration_min IS NULL")
    with op.batch_alter_table(TABLE) as batch:
        batch.drop_constraint(CHECK, type_="check")
        batch.create_check_constraint(CHECK, "price IS NOT NULL OR duration_min IS NOT NULL")
        batch.drop_column("blocked_weekdays")
