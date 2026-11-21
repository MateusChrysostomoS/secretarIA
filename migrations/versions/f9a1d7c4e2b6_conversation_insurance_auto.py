"""Record when the booking flow assigned Particular automatically.

One nullable column, additive: `conversations.flow_insurance_auto` (NULL = unknown, which is every
existing conversation - legacy choices are never guessed).

DEPLOY ORDER - the database moves FIRST (migração antes da API e do worker):
    1. `alembic upgrade head` from the NEW image, both services still old (the old code never
       reads the column);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity).

Rollback: old code on both services first, then `alembic downgrade` (drops the column).

Revision ID: f9a1d7c4e2b6
Revises: d4b7a9c2e6f1
Create Date: 2026-11-21
"""

import sqlalchemy as sa
from alembic import op

revision: str = "f9a1d7c4e2b6"
down_revision: str = "d4b7a9c2e6f1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("flow_insurance_auto", sa.Boolean(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch:
        batch.drop_column("flow_insurance_auto")
