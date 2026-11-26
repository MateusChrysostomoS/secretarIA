"""WhatsApp connection attempts made by the secretarIA backend (TASK-093 Plano 3, stage a).

One new table, additive: `whatsapp_connection_attempts` (one row per clinic attempt, unique on
(tenant_id, attempt_id)). It holds NO secret - neither the Meta code nor the token. Nothing
existing is touched, and nothing about brain-api's onboarding state changes: brain-api keeps
owning it (the secretarIA only notifies it after a connection).

DEPLOY ORDER - the database moves FIRST (migração antes da API e do worker):
    0. brain-api with `POST /internal/tenants/{id}/whatsapp-connected` (only needed before the
       flag is turned on);
    1. `alembic upgrade head` from the NEW image, both services still old (the old code never
       reads the table);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity).
The routes stay 404 until WHATSAPP_CONNECT_BACKEND_ENABLED is turned on by the owner.

Rollback: old code on both services first, then `alembic downgrade` (drops the table).

Revision ID: e7b3d1f9a5c2
Revises: f9a1d7c4e2b6
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op

revision: str = "e7b3d1f9a5c2"
down_revision: str = "f9a1d7c4e2b6"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_connection_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.Uuid(),
            sa.ForeignKey("tenants.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("attempt_id", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("phone_number_id", sa.String(64), nullable=True),
        sa.Column("waba_id", sa.String(64), nullable=True),
        sa.Column("failure_code", sa.String(48), nullable=True),
        sa.Column("brain_notified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("tenant_id", "attempt_id", name="uq_whatsapp_connection_attempt"),
    )
    op.create_index(
        "ix_whatsapp_connection_attempts_tenant_id",
        "whatsapp_connection_attempts",
        ["tenant_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_whatsapp_connection_attempts_tenant_id", table_name="whatsapp_connection_attempts"
    )
    op.drop_table("whatsapp_connection_attempts")
