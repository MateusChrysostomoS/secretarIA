"""tenant google_calendar_mode column

Per-tenant Google Calendar integration mode: "per_professional" (default —
existing behaviour unchanged, each professional may connect their own Google
account, falling back to the clinic's) or "shared_account" (the clinic
connects ONE Google account and secretarIA creates a secondary Google
Calendar per professional inside it) — see
docs/CHECKPOINT_google_calendar_modes.md.

server_default guarantees every existing tenant reads as "per_professional"
without a backfill step: nobody's calendar wiring or behaviour changes.

Revision ID: c9a1e2f4b6d8
Revises: b06ff85998bf
Create Date: 2026-08-01 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c9a1e2f4b6d8"
down_revision: str | None = "b06ff85998bf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "google_calendar_mode",
            sa.String(length=32),
            server_default="per_professional",
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "google_calendar_mode")
