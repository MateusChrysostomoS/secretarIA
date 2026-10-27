"""appointments.card_confirmation_count (entry reminder card stops at 3 confirmations)

Owner, 2026-10-27: the reminder card a patient gets when entering the conversation stops after
the patient confirmed the appointment 3 times; `confirmation_count` stays capped at 2 (agenda and
scheduled reminders), so the card needs its own uncapped counter. Additive: one NOT NULL column
with a server default (0), so API and worker can deploy in either order.

Revision ID: c5a9e2d7f4b1
Revises: b3e8f1a6c2d9
"""

import sqlalchemy as sa
from alembic import op

revision = "c5a9e2d7f4b1"
down_revision = "b3e8f1a6c2d9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column(
            "card_confirmation_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )


def downgrade() -> None:
    op.drop_column("appointments", "card_confirmation_count")
