"""tenants.clinic_facts: bounded facts about the clinic that the LLM may quote (TASK-025).

Additive and nullable (NULL = never filled in). Run this migration BEFORE the code that maps
the column: the API and the worker both select `tenants.clinic_facts` on every Tenant load, so
new code against an unmigrated database fails on every request and every turn. The reverse is
safe (code from before this revision ignores the extra nullable column), and once the
migration has run the API and the worker can be rolled in any order.

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
