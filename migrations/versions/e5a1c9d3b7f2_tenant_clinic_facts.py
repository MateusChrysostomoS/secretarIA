"""tenants.clinic_facts: bounded facts about the clinic that the LLM may quote (TASK-025).

Additive and nullable (NULL = never filled in), so the order of deploy does not matter for
correctness: run this migration first, then roll the API and the worker in any order. The
worker reads the model, so it must not run a version that expects the column before this runs.

Revision ID: e5a1c9d3b7f2
Revises: c3a9e5f1d7b2
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5a1c9d3b7f2"
down_revision: str | None = "c3a9e5f1d7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("clinic_facts", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("tenants", "clinic_facts")
