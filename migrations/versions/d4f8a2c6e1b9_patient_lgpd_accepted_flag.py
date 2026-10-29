"""Patient LGPD acceptance as an explicit boolean: one NOT NULL column + backfill.

TASK-072 (owner, 2026-10-29): a Portal patient who tapped "Mudar e-mail" at the code
step was shown the LGPD notice again. The owner asked for an explicit, clearly named
flag the consent gate reads, so that once it is true the notice is never sent to that
patient again:

    ALTER TABLE patients ADD COLUMN lgpd_accepted BOOLEAN NOT NULL DEFAULT false
    UPDATE patients SET lgpd_accepted = true WHERE lgpd_accepted_at IS NOT NULL

`lgpd_accepted_at` is kept untouched as the audit "when" (and `consent_events` stays the
immutable record); `lgpd_accepted` becomes the single source of truth for "whether".

DEPLOY ORDER - the database moves FIRST (the new code reads the column on every turn):
    1. `alembic upgrade head` from the NEW image, both services still old (the old code
       never reads the column, and keeps writing `lgpd_accepted_at` only - so run the
       backfill statement above once more right after step 2 if any patient accepted
       consent between steps 1 and 2);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity).

Rollback: old code on both services first, then `alembic downgrade` (drops the column;
`lgpd_accepted_at` still holds every acceptance).

Revision ID: d4f8a2c6e1b9
Revises: e8a3c6f1b4d9
Create Date: 2026-10-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4f8a2c6e1b9"
down_revision: str | None = "e8a3c6f1b4d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column("lgpd_accepted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(
        sa.text("UPDATE patients SET lgpd_accepted = true WHERE lgpd_accepted_at IS NOT NULL")
    )


def downgrade() -> None:
    with op.batch_alter_table("patients") as batch:
        batch.drop_column("lgpd_accepted")
