"""Mark professionals who must configure themselves before patients can book them.

One boolean column, additive: `professionals.requires_self_setup` (server default false, so every
existing professional - the owner included - keeps today's behaviour; nothing is backfilled).

DEPLOY ORDER - the database moves FIRST (migração antes da API e do worker):
    1. `alembic upgrade head` from the NEW image, both services still old (the old code never
       reads the column);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity).

Rollback: old code on both services first, then `alembic downgrade` (drops the column).

Revision ID: a3c7e1f9b5d2
Revises: f9a1d7c4e2b6
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from alembic import op

revision: str = "a3c7e1f9b5d2"
down_revision: str = "f9a1d7c4e2b6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "professionals",
        sa.Column(
            "requires_self_setup", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
    )


def downgrade() -> None:
    with op.batch_alter_table("professionals") as batch:
        batch.drop_column("requires_self_setup")
