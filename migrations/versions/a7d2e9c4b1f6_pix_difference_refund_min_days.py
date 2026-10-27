"""tenants.pix_difference_refund_min_days (service change with a paid deposit)

Docs: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md. Additive: one
NOT NULL column with a server default (2), so API and worker can deploy in either order.

Revision ID: a7d2e9c4b1f6
Revises: f3b9d1c7a5e2
"""

import sqlalchemy as sa
from alembic import op

revision = "a7d2e9c4b1f6"
down_revision = "f3b9d1c7a5e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "pix_difference_refund_min_days", sa.Integer(), nullable=False, server_default="2"
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "pix_difference_refund_min_days")
