"""tenant greeting_buttons column

Adds optional quick-reply buttons for the first-contact greeting. List of short
labels (JSON), backfilled to an empty list for existing rows.

Revision ID: d5e9f3a2b7c8
Revises: c4d8e2f1a5b6
Create Date: 2026-06-08 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e9f3a2b7c8"
down_revision: str | None = "c4d8e2f1a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "greeting_buttons", sa.JSON(), server_default=sa.text("'[]'"), nullable=False
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "greeting_buttons")
