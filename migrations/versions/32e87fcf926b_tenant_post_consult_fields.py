"""tenant: add post-consult config fields (message + knowledge)

Adds two optional tenant-level text fields for the post-consult follow-up
feature:

  - `post_consult_message` - copy the secretary will send after a consult.
    Sending itself is wired by a later feature; this column is only
    persisted + surfaced (doctor hub) for now.
  - `post_consult_knowledge` - free-text reference material the LLM may
    consult when answering post-consult questions (recovery care, return-
    visit norms, how exam results are delivered). Injected into the system
    prompt only on qualifying turns - see ai/prompts.py.

Both nullable, no backfill.

Revision ID: 32e87fcf926b
Revises: b3d9f2a1c8e7
Create Date: 2026-07-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "32e87fcf926b"
down_revision: str | None = "b3d9f2a1c8e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column("post_consult_message", sa.Text(), nullable=True),
    )
    op.add_column(
        "tenants",
        sa.Column("post_consult_knowledge", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("tenants", "post_consult_knowledge")
    op.drop_column("tenants", "post_consult_message")
