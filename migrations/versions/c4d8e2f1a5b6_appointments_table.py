"""appointments table - event ↔ patient ↔ phone link

Revision ID: c4d8e2f1a5b6
Revises: b2f1a7c9d3e4
Create Date: 2026-06-02 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c4d8e2f1a5b6"
down_revision: str | None = "b2f1a7c9d3e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# create_type=False is essential: we create the type explicitly in upgrade()
# with checkfirst=True (idempotent). Without this flag, op.create_table() would
# ALSO auto-emit `CREATE TYPE appointment_status` (no checkfirst) and collide
# with the explicit create -> DuplicateObjectError.
appointment_status = postgresql.ENUM(
    "scheduled", "cancelled", "rescheduled", "confirmed", "attended", "no_show",
    name="appointment_status",
    create_type=False,
)


def upgrade() -> None:
    appointment_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "appointments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("patient_id", sa.Uuid(), nullable=True),
        sa.Column("google_event_id", sa.String(length=255), nullable=False),
        sa.Column("phone", sa.String(length=32), nullable=True),
        sa.Column(
            "status",
            appointment_status,
            server_default="scheduled",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_appointments_tenant_id", "appointments", ["tenant_id"])
    op.create_index("ix_appointments_patient_id", "appointments", ["patient_id"])
    op.create_index("ix_appointments_google_event_id", "appointments", ["google_event_id"])


def downgrade() -> None:
    op.drop_index("ix_appointments_google_event_id", table_name="appointments")
    op.drop_index("ix_appointments_patient_id", table_name="appointments")
    op.drop_index("ix_appointments_tenant_id", table_name="appointments")
    op.drop_table("appointments")
    appointment_status.drop(op.get_bind(), checkfirst=True)
