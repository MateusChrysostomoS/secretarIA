"""Record when the booking flow assigned Particular automatically.

Revision ID: f9a1d7c4e2b6
Revises: d4b7a9c2e6f1
"""

import sqlalchemy as sa
from alembic import op

revision: str = "f9a1d7c4e2b6"
down_revision: str = "d4b7a9c2e6f1"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("flow_insurance_auto", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "flow_insurance_auto")
