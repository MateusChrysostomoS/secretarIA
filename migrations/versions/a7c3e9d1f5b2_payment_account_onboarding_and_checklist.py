"""clinic_payment_accounts: Asaas onboarding states and the setup checklist (TASK-071, Plan 4)

Seven nullable columns, no backfill and no server default. The four `onboarding_*` hold fixed words
(PENDING | APPROVED | REJECTED | AWAITING_APPROVAL | UNKNOWN) read from GET /myAccount/status;
they stay NULL when the clinic's key cannot read that status. `setup_checklist` is a JSON object
whose keys are whitelisted in the code (the items only the manager can confirm).
`credentials_changed_at` is when the current API key was stored: a charge that worked with an older
key is not proof for the new one. Purely additive.

Deploy: migration first, then `secretaria_api` + `secretaria-worker` together.

Revision ID: a7c3e9d1f5b2
Revises: b9e4d2a6c8f1
Create Date: 2026-10-29 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a7c3e9d1f5b2"
down_revision: str | None = "b9e4d2a6c8f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE_COLUMNS = (
    "onboarding_general",
    "onboarding_docs",
    "onboarding_commercial",
    "onboarding_bank",
)


def upgrade() -> None:
    for name in _STATE_COLUMNS:
        op.add_column(
            "clinic_payment_accounts", sa.Column(name, sa.String(length=24), nullable=True)
        )
    op.add_column(
        "clinic_payment_accounts",
        sa.Column("onboarding_checked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "clinic_payment_accounts",
        sa.Column("credentials_changed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("clinic_payment_accounts", sa.Column("setup_checklist", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("clinic_payment_accounts", "setup_checklist")
    op.drop_column("clinic_payment_accounts", "credentials_changed_at")
    op.drop_column("clinic_payment_accounts", "onboarding_checked_at")
    for name in reversed(_STATE_COLUMNS):
        op.drop_column("clinic_payment_accounts", name)
