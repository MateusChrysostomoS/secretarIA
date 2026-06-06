"""tenant config columns + tenant_credentials table

Adds the non-sensitive tenant configuration columns the doctor hub edits, and a
separate encrypted-at-rest table for the Google Calendar refresh token.

Revision ID: b2f1a7c9d3e4
Revises: 6c590bc4ee75
Create Date: 2026-06-02 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2f1a7c9d3e4"
down_revision: str | None = "6c590bc4ee75"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # --- tenant config columns (server_default backfills existing rows) ---
    op.add_column("tenants", sa.Column("greeting_message", sa.Text(), nullable=True))
    op.add_column("tenants", sa.Column("persona_notes", sa.Text(), nullable=True))
    op.add_column(
        "tenants",
        sa.Column("language", sa.String(length=8), server_default="pt-BR", nullable=False),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "timezone",
            sa.String(length=64),
            server_default="America/Sao_Paulo",
            nullable=False,
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "appointment_duration_min", sa.Integer(), server_default="30", nullable=False
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("business_hours", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "appointment_types", sa.JSON(), server_default=sa.text("'[]'"), nullable=False
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "google_calendar_id",
            sa.String(length=255),
            server_default="primary",
            nullable=False,
        ),
    )
    op.add_column(
        "tenants",
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )

    # --- encrypted credentials table ---
    op.create_table(
        "tenant_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("google_refresh_token_encrypted", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", name="uq_tenant_credentials_tenant_id"),
    )
    op.create_index(
        op.f("ix_tenant_credentials_tenant_id"),
        "tenant_credentials",
        ["tenant_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_tenant_credentials_tenant_id"), table_name="tenant_credentials")
    op.drop_table("tenant_credentials")
    op.drop_column("tenants", "is_active")
    op.drop_column("tenants", "google_calendar_id")
    op.drop_column("tenants", "appointment_types")
    op.drop_column("tenants", "business_hours")
    op.drop_column("tenants", "appointment_duration_min")
    op.drop_column("tenants", "timezone")
    op.drop_column("tenants", "language")
    op.drop_column("tenants", "persona_notes")
    op.drop_column("tenants", "greeting_message")
