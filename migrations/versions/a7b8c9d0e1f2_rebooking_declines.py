"""rebooking_declines: why a patient did not rebook after a doctor cancelled

Additive and reversible: one new table, no change to any existing one, so the
downgrade is a plain drop and nothing else in the schema depends on it.

See models/rebooking_decline.py for why this is its own event table rather
than a column on `appointments` or a row in `analytics_events`.

Revision ID: a7b8c9d0e1f2
Revises: d1c2b3a4e5f6
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a7b8c9d0e1f2"
down_revision = "d1c2b3a4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rebooking_declines",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        # Patient content — see the model docstring's LGPD note.
        sa.Column("reason_text", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_rebooking_declines_tenant_id", "rebooking_declines", ["tenant_id"])
    op.create_index(
        "ix_rebooking_declines_appointment_id", "rebooking_declines", ["appointment_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_rebooking_declines_appointment_id", table_name="rebooking_declines")
    op.drop_index("ix_rebooking_declines_tenant_id", table_name="rebooking_declines")
    op.drop_table("rebooking_declines")
