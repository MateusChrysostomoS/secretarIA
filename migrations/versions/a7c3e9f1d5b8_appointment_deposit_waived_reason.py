"""appointments: remember why a booking was confirmed without the deposit (TASK-071, Plan 3)

One nullable column, no backfill and no server default: every existing booking is an ordinary
or paid one. `deposit_waived_reason` holds a `FailureReason` value (a closed vocabulary, never
provider text, a document, an e-mail or a phone number); the agenda derives the plain sentence
from it. Purely additive.

Deploy: migration first, then `secretaria_api` + `secretaria-worker` together.

Revision ID: a7c3e9f1d5b8
Revises: d4f7a1c9e2b6
Create Date: 2026-10-29 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9f1d5b8"
down_revision: str | None = "d4f7a1c9e2b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "appointments",
        sa.Column("deposit_waived_reason", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appointments", "deposit_waived_reason")
