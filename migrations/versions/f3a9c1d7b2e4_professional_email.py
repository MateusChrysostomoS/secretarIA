"""professionals: add email for the per-professional config-gap alert

Adds an optional email address to each professional row. When set, the worker
also mails THAT doctor - not only the clinic's `tenants.contact_email` - when a
patient reaches them and cannot book because their own configuration is
incomplete (no availability window, or no service). See
workers/tasks.py::_handle_professional_config_incomplete.

Nullable with no server_default and NO backfill, deliberately: the clinic is
the only party that knows a doctor's address, so existing rows stay NULL until
someone fills the field in on the hub's Configuracao screen. Every consumer
treats NULL as "nobody to mail here" and falls back to the clinic address.

Revision ID: f3a9c1d7b2e4
Revises: a7b8c9d0e1f2
Create Date: 2026-08-29 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f3a9c1d7b2e4"
down_revision: str | None = "a7b8c9d0e1f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "professionals",
        sa.Column("email", sa.String(length=254), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("professionals", "email")
