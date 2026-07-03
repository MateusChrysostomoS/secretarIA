"""Add professionals + units tables and appointment.professional_id/unit_id.

Backs the multi_professional and multi_unit addon plugins
(plugins/multi_professional.py, plugins/multi_unit.py). A professional
optionally carries its own Google Calendar id (falls back to the tenant's own
when NULL); a unit only carries a display address. Both FKs on `appointments`
are ON DELETE SET NULL so removing a professional/unit never deletes booking
history.

Revision ID: f1a2b3c4d5e6
Revises: e9f0a1b2c3d4
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: str | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "professionals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("google_calendar_id", sa.String(length=255), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_professionals_tenant_id"), "professionals", ["tenant_id"], unique=False
    )

    op.create_table(
        "units",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("address", sa.String(length=512), nullable=True),
        sa.Column(
            "is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_units_tenant_id"), "units", ["tenant_id"], unique=False)

    op.add_column("appointments", sa.Column("professional_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_appointments_professional_id"), "appointments", ["professional_id"], unique=False
    )
    op.create_foreign_key(
        "fk_appointments_professional_id_professionals",
        "appointments",
        "professionals",
        ["professional_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.add_column("appointments", sa.Column("unit_id", sa.Uuid(), nullable=True))
    op.create_index(op.f("ix_appointments_unit_id"), "appointments", ["unit_id"], unique=False)
    op.create_foreign_key(
        "fk_appointments_unit_id_units",
        "appointments",
        "units",
        ["unit_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_appointments_unit_id_units", "appointments", type_="foreignkey")
    op.drop_index(op.f("ix_appointments_unit_id"), table_name="appointments")
    op.drop_column("appointments", "unit_id")

    op.drop_constraint(
        "fk_appointments_professional_id_professionals", "appointments", type_="foreignkey"
    )
    op.drop_index(op.f("ix_appointments_professional_id"), table_name="appointments")
    op.drop_column("appointments", "professional_id")

    op.drop_index(op.f("ix_units_tenant_id"), table_name="units")
    op.drop_table("units")

    op.drop_index(op.f("ix_professionals_tenant_id"), table_name="professionals")
    op.drop_table("professionals")
