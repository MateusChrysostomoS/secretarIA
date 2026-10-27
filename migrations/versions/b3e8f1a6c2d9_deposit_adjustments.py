"""deposit_adjustments (service change with a paid deposit)

Docs: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md. Purely additive:
one new table, child of `pix_deposits` (whose `appointment_id` stays unique). The new attention
reason `service_diff_refund_due` needs no schema change (`payment_attention_items.reason` is a
plain VARCHAR(32)). API and worker can deploy in either order after this revision.

Revision ID: b3e8f1a6c2d9
Revises: a7d2e9c4b1f6
"""

import sqlalchemy as sa
from alembic import op

revision = "b3e8f1a6c2d9"
down_revision = "a7d2e9c4b1f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "deposit_adjustments",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "tenant_id", sa.Uuid(), sa.ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "deposit_id",
            sa.Uuid(),
            sa.ForeignKey("pix_deposits.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("old_service", sa.String(120), nullable=False),
        sa.Column("new_service", sa.String(120), nullable=False),
        sa.Column("pending_edit", sa.JSON(), nullable=True),
        sa.Column("diff_cents", sa.Integer(), nullable=False),
        sa.Column("provider_charge_id", sa.String(96), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("asked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("provider_charge_id", name="uq_deposit_adjustments_provider_charge"),
    )
    op.create_index("ix_deposit_adjustments_tenant_id", "deposit_adjustments", ["tenant_id"])
    op.create_index("ix_deposit_adjustments_deposit_id", "deposit_adjustments", ["deposit_id"])
    op.create_index(
        "ix_deposit_adjustments_appointment_id", "deposit_adjustments", ["appointment_id"]
    )
    op.create_index(
        "ix_deposit_adjustments_tenant_status",
        "deposit_adjustments",
        ["tenant_id", "status", "expires_at"],
    )


def downgrade() -> None:
    op.drop_table("deposit_adjustments")
