"""flow engine: tenant.initial_flows + conversation flow state

Track 2 deterministic (zero-LLM) entry flows. Adds the per-tenant flow config
blob and the per-conversation flow state machine columns. flow_state mirrors
handover_state's VARCHAR-backed (non-native) enum style.

Revision ID: a3c4d5e6f7b8
Revises: f2b3c4d5e6a7
Create Date: 2026-06-09 10:10:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3c4d5e6f7b8"
down_revision: str | None = "f2b3c4d5e6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("initial_flows", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
    )
    op.add_column(
        "conversations",
        sa.Column(
            "flow_state",
            sa.Enum(
                "IDLE",
                "MENU",
                "SERVICE_CATALOG",
                "BUSINESS_HOURS",
                "LLM",
                name="flowstate",
                native_enum=False,
                length=32,
            ),
            server_default="IDLE",
            nullable=False,
        ),
    )
    op.add_column("conversations", sa.Column("flow_step", sa.String(length=48), nullable=True))
    op.add_column(
        "conversations", sa.Column("flow_selected_type", sa.String(length=120), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("flow_selected_day", sa.String(length=32), nullable=True)
    )
    op.add_column(
        "conversations", sa.Column("flow_selected_slot", sa.String(length=48), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("conversations", "flow_selected_slot")
    op.drop_column("conversations", "flow_selected_day")
    op.drop_column("conversations", "flow_selected_type")
    op.drop_column("conversations", "flow_step")
    op.drop_column("conversations", "flow_state")
    op.drop_column("tenants", "initial_flows")
