"""Professional minimum booking notice: one nullable column, additive.

Instituto Gicovate (2026-10-27): "agendamento com no mínimo 24h de antecedência". The
doctor decides it, so it lives on the professional:

    ALTER TABLE professionals ADD COLUMN min_booking_notice_hours INTEGER NULL

NULL = no rule (every existing professional, unchanged). N = a patient booking a NEW
appointment through the secretarIA is never offered, nor allowed to confirm, a slot
starting less than N hours from now. Cancelling and rescheduling are not limited, and the
clinic's own agenda ("Nova consulta" in the console) is not either.

DEPLOY ORDER - the database moves FIRST (the new code reads the column on every flow turn):
    1. `alembic upgrade head` from the NEW image, both services still old (the old code
       never reads the column);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. then the Brain-Message front (the field in Contexto > Profissionais).

Rollback: old code on both services first, then `alembic downgrade` (drops the column and
every value a clinic typed into it).

Revision ID: c3d9e1f5a7b2
Revises: b7e2c4f9a1d6
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d9e1f5a7b2"
down_revision: str | None = "b7e2c4f9a1d6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "professionals", sa.Column("min_booking_notice_hours", sa.Integer(), nullable=True)
    )


def downgrade() -> None:
    with op.batch_alter_table("professionals") as batch:
        batch.drop_column("min_booking_notice_hours")
