"""conversation: add reactivation_origin

When a returning patient is offered a "quer continuar com a nossa última
conversa?" prompt, this column holds the FlowState value to resume to and marks
that the next inbound message is the Sim/Não answer. NULL whenever no prompt is
pending. Additive nullable column — safe on a populated table.

Revision ID: b1c2d3e4f5a6
Revises: c5d6e7f8a9b0
Create Date: 2026-06-10 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b1c2d3e4f5a6"
down_revision: str | None = "c5d6e7f8a9b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("reactivation_origin", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("conversations", "reactivation_origin")
