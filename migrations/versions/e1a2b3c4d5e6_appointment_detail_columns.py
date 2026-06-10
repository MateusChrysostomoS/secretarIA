"""appointment detail columns (conversation link + booked window)

Mirrors the booked window and type onto the appointments row so the bot path
can persist a complete record and appointments can be queried (reminders,
analytics) without round-tripping to Google Calendar.

Revision ID: e1a2b3c4d5e6
Revises: d5e9f3a2b7c8
Create Date: 2026-06-09 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e1a2b3c4d5e6"
down_revision: str | None = "d5e9f3a2b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "appointments", sa.Column("conversation_id", sa.Uuid(), nullable=True)
    )
    op.add_column(
        "appointments", sa.Column("google_event_link", sa.String(length=1024), nullable=True)
    )
    op.add_column(
        "appointments", sa.Column("appointment_type", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "appointments", sa.Column("start_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "appointments", sa.Column("end_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_appointments_conversation_id", "appointments", ["conversation_id"]
    )
    op.create_foreign_key(
        "fk_appointments_conversation_id",
        "appointments",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_appointments_conversation_id", "appointments", type_="foreignkey"
    )
    op.drop_index("ix_appointments_conversation_id", table_name="appointments")
    op.drop_column("appointments", "end_at")
    op.drop_column("appointments", "start_at")
    op.drop_column("appointments", "appointment_type")
    op.drop_column("appointments", "google_event_link")
    op.drop_column("appointments", "conversation_id")
