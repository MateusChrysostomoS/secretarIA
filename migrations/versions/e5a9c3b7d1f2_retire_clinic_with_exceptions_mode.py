"""retire the 3rd insurance mode: clinic_with_exceptions -> shared

TASK-086 T5. Owner's decision (2026-11-16): no clinic is in production, so any tenant still in
`clinic_with_exceptions` becomes `shared` (the clinic's list). The CHECK constraint keeps the old
value legal on purpose (read code stays; only the API stops accepting it).

Downgrade is a documented no-op: the original per-tenant mode is not recorded, and the old value
is no longer selectable, so there is nothing safe to restore.

Revision ID: e5a9c3b7d1f2
Revises: d7f2b8a4c1e9
Create Date: 2026-11-17
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5a9c3b7d1f2"
down_revision: str | None = "d7f2b8a4c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "UPDATE tenants SET insurance_mode = 'shared' "
        "WHERE insurance_mode = 'clinic_with_exceptions'"
    )


def downgrade() -> None:
    # Intentional no-op (see module docstring).
    pass
