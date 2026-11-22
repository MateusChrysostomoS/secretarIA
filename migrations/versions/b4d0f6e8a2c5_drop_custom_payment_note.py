"""Drop `custom_payment_note` from both convênio plan tables (TASK-090) - LAST revision, run it LAST.

The owner removed the payment text of the "Outro" convênio completely, database included
(SPEC §3 item 2). The code no longer maps, reads, writes or returns the column (task B6), but the
code that is still deployed in production DOES read it. So this DROP is its own revision and is
never run in the same `alembic upgrade` as the additive TASK-090 revisions.

DEPLOY ORDER (documented, not executed here):
    1. `alembic upgrade a3c9e5d7f1b4` from the NEW image - the additive revisions only (unit
       address parts, "Sede" backfill + paid-notice reset, `insurance_plan_order`). The old code
       keeps working because nothing it reads has been removed.
    2. Deploy `secretaria_api`, then `secretaria-worker` (`GET /build` parity), then the
       Brain-Message front.
    3. ONLY THEN `alembic upgrade head` - this DROP. Running it earlier breaks the old code, which
       still selects `custom_payment_note`.

Rollback: `alembic downgrade -1` recreates both columns as `Text NULL` - EMPTY (the dropped notes
are not recoverable; no data is restored).

Revision ID: b4d0f6e8a2c5
Revises: a3c9e5d7f1b4
Create Date: 2026-11-22
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4d0f6e8a2c5"
down_revision: str | None = "a3c9e5d7f1b4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLES = ("tenant_insurance_plans", "professional_insurance_plans")


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.drop_column("custom_payment_note")


def downgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("custom_payment_note", sa.Text(), nullable=True))
