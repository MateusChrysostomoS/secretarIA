"""Add ehr/pix_whatsapp/analytics_bi plugin columns + tables, and consent_events.

Backs the final plugin-seam wave:
  - `tenants.ehr_provider` / `tenants.pix_key` — per-tenant config read by
    plugins/ehr.py and plugins/pix_whatsapp.py's post_booking hooks.
  - `analytics_events` — minimal, non-personal booking events recorded by
    plugins/analytics_bi.py (models/analytics_event.py).
  - `consent_events` — LGPD consent/legal-basis audit trail groundwork
    (models/consent_event.py), read/deleted by the new
    api/internal_privacy.py export/erase endpoints.

Revision ID: a4b5c6d7e8f9
Revises: f1a2b3c4d5e6
Create Date: 2026-07-02 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4b5c6d7e8f9"
down_revision: str | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("ehr_provider", sa.String(length=32), nullable=True))
    op.add_column("tenants", sa.Column("pix_key", sa.String(length=140), nullable=True))

    op.create_table(
        "analytics_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=48), nullable=False),
        sa.Column("payload", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_analytics_events_tenant_id"), "analytics_events", ["tenant_id"], unique=False
    )

    op.create_table(
        "consent_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("wa_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=48), nullable=False),
        sa.Column("legal_basis", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_consent_events_tenant_id"), "consent_events", ["tenant_id"], unique=False
    )
    op.create_index(op.f("ix_consent_events_wa_id"), "consent_events", ["wa_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_consent_events_wa_id"), table_name="consent_events")
    op.drop_index(op.f("ix_consent_events_tenant_id"), table_name="consent_events")
    op.drop_table("consent_events")

    op.drop_index(op.f("ix_analytics_events_tenant_id"), table_name="analytics_events")
    op.drop_table("analytics_events")

    op.drop_column("tenants", "pix_key")
    op.drop_column("tenants", "ehr_provider")
