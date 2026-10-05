"""appointment reminders foundation: confirmation counter, schedule table, clinic config.

TASK-032 R1 (spec 4.1 / 4.5). Purely additive: three nullable/defaulted columns
on appointments, two on tenants, one new table. Nothing is dropped or renamed,
so an API/worker image from before this revision keeps working against the
migrated database (it simply never reads the new columns). Deploy order:
migration first, then API and worker together.

Revision ID: b8d3f1a6c2e5
Revises: c3a9e5f1d7b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d3f1a6c2e5"
down_revision: str | None = "c3a9e5f1d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_UNWARNED = "warned_at IS NULL AND warn_due_at IS NOT NULL"


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column(
            "confirmation_count", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
    )
    op.add_column(
        "appointments",
        sa.Column("first_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("last_confirmed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "reminders_v2_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("reminder_extra_lead_minutes", sa.Integer(), nullable=True),
    )

    op.create_table(
        "appointment_reminders",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "patient_id",
            sa.Uuid(),
            sa.ForeignKey("patients.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("appointment_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column(
            "channel", sa.String(length=16), nullable=False, server_default="whatsapp"
        ),
        sa.Column(
            "with_prompt", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("answer", sa.String(length=16), nullable=True),
        sa.Column("warn_due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("warn_kind", sa.String(length=24), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "uq_appointment_reminders_version",
        "appointment_reminders",
        ["appointment_id", "kind", "appointment_start_at"],
        unique=True,
        postgresql_where=sa.text("invalidated_at IS NULL"),
        sqlite_where=sa.text("invalidated_at IS NULL"),
    )
    op.create_index("ix_appointment_reminders_tenant_id", "appointment_reminders", ["tenant_id"])
    op.create_index(
        "ix_appointment_reminders_appointment_id", "appointment_reminders", ["appointment_id"]
    )
    op.create_index(
        "ix_appointment_reminders_status_due", "appointment_reminders", ["status", "due_at"]
    )
    op.create_index(
        "ix_appointment_reminders_warn_due",
        "appointment_reminders",
        ["warn_due_at"],
        postgresql_where=sa.text(_UNWARNED),
        sqlite_where=sa.text(_UNWARNED),
    )


def downgrade() -> None:
    # Local/dev only: never run against a database a newer image still uses.
    op.drop_table("appointment_reminders")
    op.drop_column("tenants", "reminder_extra_lead_minutes")
    op.drop_column("tenants", "reminders_v2_enabled")
    op.drop_column("appointments", "last_confirmed_at")
    op.drop_column("appointments", "first_confirmed_at")
    op.drop_column("appointments", "confirmation_count")
