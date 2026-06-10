"""tenant returning_greeting_message column

Greeting sent to a patient who has contacted the clinic before. Optional;
NULL keeps the legacy first-contact behaviour for returning patients.

Revision ID: f2b3c4d5e6a7
Revises: e1a2b3c4d5e6
Create Date: 2026-06-09 10:05:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b3c4d5e6a7"
down_revision: str | None = "e1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants", sa.Column("returning_greeting_message", sa.Text(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("tenants", "returning_greeting_message")
