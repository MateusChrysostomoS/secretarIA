"""Merge native agenda and Pix histories (TASK-066); no schema or data operation.

Both parents are retained unchanged so databases on either history can upgrade.
This revision is local only; no database migration was executed by TASK-066.
"""

revision = "d6f0a8c2e4b7"
down_revision = ("b6d2e8f4a1c3", "b7e2c4f9a1d6")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
