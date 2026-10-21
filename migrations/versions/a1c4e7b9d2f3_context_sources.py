"""context_sources: material submitted for context extraction (TASK-026).

New table, nothing else touched. DEPLOY ORDER - the database moves FIRST:

    1. `alembic upgrade head` from the NEW image (one-off), both services still old;
    2. deploy `secretaria_api` AND `secretaria-worker` together (new arq job);
    3. then the Brain-Message front.

Rollback: OLD code on both services first, then `alembic downgrade -1`.

Revision ID: a1c4e7b9d2f3
Revises: a4c7e2f9b1d3
Create Date: 2026-10-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c4e7b9d2f3"
down_revision: str | None = "a4c7e2f9b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "context_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("label", sa.String(255), nullable=False),
        sa.Column("status", sa.String(12), server_default="pending", nullable=False),
        sa.Column("error_code", sa.String(40), nullable=True),
        sa.Column("url", sa.String(2048), nullable=True),
        sa.Column("source_text", sa.Text(), nullable=True),
        sa.Column("proposal", sa.JSON(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_context_sources_tenant_id", "context_sources", ["tenant_id"])
    op.create_index(
        "ix_context_sources_tenant_created", "context_sources", ["tenant_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_context_sources_tenant_created", table_name="context_sources")
    op.drop_index("ix_context_sources_tenant_id", table_name="context_sources")
    op.drop_table("context_sources")
