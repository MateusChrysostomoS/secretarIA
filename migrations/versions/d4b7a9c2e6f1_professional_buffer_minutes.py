"""Professional buffer between consultations: one nullable column, additive.

TASK-088: the doctor decides how many minutes must stay free between two consultations
("intervalo entre consultas"), so it lives on the professional:

    ALTER TABLE professionals ADD COLUMN buffer_minutes INTEGER NULL

NULL (or 0) = no buffer (every existing professional, unchanged). N = a time the patient is
offered, or asked to confirm, through the secretarIA leaves N free minutes before and after
the doctor's other appointments. The clinic's own agenda is never limited, and neither the
service duration nor the event written to the agenda changes.

DEPLOY ORDER - the database moves FIRST (the new code reads the column on every roster load):
    1. `alembic upgrade head` from the NEW image, both services still old (the old code
       never reads the column);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. then the Brain-Message front (the field in Contexto > Profissionais).

Rollback: old code on both services first, then `alembic downgrade` (drops the column and
every value a clinic typed into it).

Revision ID: d4b7a9c2e6f1
Revises: c8e2a4f6b1d3
Create Date: 2026-11-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4b7a9c2e6f1"
down_revision: str | None = "c8e2a4f6b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("professionals", sa.Column("buffer_minutes", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("professionals") as batch:
        batch.drop_column("buffer_minutes")
