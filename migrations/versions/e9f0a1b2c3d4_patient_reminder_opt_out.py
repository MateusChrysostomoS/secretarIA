"""Add patients.reminder_opt_out for the reminders plugin.

Reminders ride on the booking relationship itself (a patient who booked has a
legitimate expectation of a reminder about their own appointment) — no
separate opt-in flow gates sending them. An explicit opt-out is always
honored, though: this column is that one override. A full LGPD consent /
preference registry (marketing opt-in, channel prefs, etc.) is a separate
round — TODO(lgpd-consent-registry).

Revision ID: e9f0a1b2c3d4
Revises: d7e8f9a0b1c2
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | None = "d7e8f9a0b1c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column(
            "reminder_opt_out",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("patients", "reminder_opt_out")
