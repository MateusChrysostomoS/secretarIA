"""Clinic-chosen order of its convênios: one nullable column, additive.

Instituto Gicovate (2026-11-09): Unimed is the doctor's main plan, yet the patient's
convênio list is alphabetical, so it sat behind "Ver mais convênios". The clinic now
chooses the order:

    ALTER TABLE tenant_insurance_plans ADD COLUMN sort_order INTEGER NULL

NULL = no position chosen (every existing row): those plans keep the alphabetical order,
AFTER every plan the clinic placed. Written only by `PUT /tenants/me/insurance-plans/order`.

DEPLOY ORDER - the database moves FIRST (the new code selects the column):
    1. `alembic upgrade head` from the NEW image, both services still old;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. then the Brain-Message front (the arrows in Contexto > Convênios).

Rollback: old code on both services first, then `alembic downgrade`.

Revision ID: d2f6b8a4c1e7
Revises: a7c3e9d1f5b2
Create Date: 2026-11-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2f6b8a4c1e7"
down_revision: str | None = "a7c3e9d1f5b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenant_insurance_plans", sa.Column("sort_order", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenant_insurance_plans", "sort_order")
