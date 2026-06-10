"""tenant: add contact_email for operational alerts

Adds an optional email address to each tenant row. When set, the worker
sends an alert email when Google Calendar becomes unreachable (token revoked,
Google outage, etc.) instead of silently degrading.

Revision ID: c5d6e7f8a9b0
Revises: a3c4d5e6f7b8
Create Date: 2026-06-10 09:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5d6e7f8a9b0"
down_revision: str | None = "a3c4d5e6f7b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("contact_email", sa.String(length=254), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "contact_email")
