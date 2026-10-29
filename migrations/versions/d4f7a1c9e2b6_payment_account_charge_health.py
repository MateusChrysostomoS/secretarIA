"""clinic_payment_accounts: remember the last charge failure and success (TASK-071, Plan 3)

Four nullable columns, no backfill and no server default: an account that never charged is
`untested`. `last_charge_failure_reason` holds a `FailureReason` value and
`last_charge_failure_stage` a short step name; neither ever holds provider text, a document,
an e-mail or a phone number. Purely additive.

Deploy: migration first, then `secretaria_api` + `secretaria-worker` together.

Revision ID: d4f7a1c9e2b6
Revises: e8a3c6f1b4d9
Create Date: 2026-10-28 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f7a1c9e2b6"
down_revision: str | None = "e8a3c6f1b4d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "clinic_payment_accounts",
        sa.Column("last_charge_failure_reason", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "clinic_payment_accounts",
        sa.Column("last_charge_failure_stage", sa.String(length=16), nullable=True),
    )
    op.add_column(
        "clinic_payment_accounts",
        sa.Column("last_charge_failure_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "clinic_payment_accounts",
        sa.Column("last_charge_ok_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("clinic_payment_accounts", "last_charge_ok_at")
    op.drop_column("clinic_payment_accounts", "last_charge_failure_at")
    op.drop_column("clinic_payment_accounts", "last_charge_failure_stage")
    op.drop_column("clinic_payment_accounts", "last_charge_failure_reason")
