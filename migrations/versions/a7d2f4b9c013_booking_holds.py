"""booking_holds: the slot a Brain-Message visitor holds while proving their address

Creates the table behind `models/booking_hold.py`. Additive and reversible: no
existing table, column or row is touched, and nothing reads this table until
`services/booking_hold.py` is deployed with it, so the migration can ship
ahead of the code without changing any behaviour.

Revision ID: a7d2f4b9c013
Revises: c1d4a8e6f2b0
Create Date: 2026-09-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7d2f4b9c013"
down_revision: str | None = "c1d4a8e6f2b0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "booking_holds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("professional_id", sa.Uuid(), nullable=True),
        sa.Column("appointment_type", sa.String(length=120), nullable=True),
        sa.Column("insurance", sa.String(length=120), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_booking_holds_tenant_id", "booking_holds", ["tenant_id"])
    op.create_index("ix_booking_holds_conversation_id", "booking_holds", ["conversation_id"])
    op.create_index("ix_booking_holds_patient_id", "booking_holds", ["patient_id"])
    op.create_index("ix_booking_holds_professional_id", "booking_holds", ["professional_id"])
    op.create_index("ix_booking_holds_expires_at", "booking_holds", ["expires_at"])
    op.create_index(
        "ix_booking_holds_agenda_window",
        "booking_holds",
        ["tenant_id", "professional_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_booking_holds_agenda_window", table_name="booking_holds")
    op.drop_index("ix_booking_holds_expires_at", table_name="booking_holds")
    op.drop_index("ix_booking_holds_professional_id", table_name="booking_holds")
    op.drop_index("ix_booking_holds_patient_id", table_name="booking_holds")
    op.drop_index("ix_booking_holds_conversation_id", table_name="booking_holds")
    op.drop_index("ix_booking_holds_tenant_id", table_name="booking_holds")
    op.drop_table("booking_holds")
