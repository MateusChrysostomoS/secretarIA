"""Durable professional edit notice intents, additive and references-only.

Revision ID: c2d5f8a1e4b6
Revises: b1c4e7a2d9f3
"""

import sqlalchemy as sa
from alembic import op

revision = "c2d5f8a1e4b6"
down_revision = "b1c4e7a2d9f3"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "professional_edit_notices",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("version", sa.String(64), nullable=False),
        sa.Column("changed_fields", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("appointment_id", "revision", name="uq_profedit_appointment_revision"),
    )
    op.create_index(
        "ix_profedit_pending_due", "professional_edit_notices", ["status", "next_attempt_at"]
    )


def downgrade():
    op.drop_index("ix_profedit_pending_due", table_name="professional_edit_notices")
    op.drop_table("professional_edit_notices")
