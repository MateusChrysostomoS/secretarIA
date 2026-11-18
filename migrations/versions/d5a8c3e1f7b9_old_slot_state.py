"""old_slot_state: where the OLD slot's event of a suspended change is (TASK-085 R, 3rd round).

TASK-085 R / U0 (tasks/TASK-085/sinal-trocas: SPEC D-R10..D-R14, mapa-u.md §1.2 / §4.1). D-R10:
while the Pix of the difference of a dearer change is open in a clinic that books only after the
sinal, the old consultation LEAVES the doctor's agenda (its event is deleted, the slot is free for
other patients) and its row stays intact so it can come back. Two columns:

    ALTER  deposit_adjustments.old_slot      ADD VARCHAR(16) NULL
           NULL (does not apply / not claimed yet) -> releasing (claimed; the event may still
           exist) -> released (deleted; slot free) -> restored (re-created on the old slot) |
           rebooked (the row moved via "Outro horário"/the hub) | replaced (paid: an event was
           created on the new slot/agenda).
    ALTER  deposit_adjustments.old_slot_at   ADD TIMESTAMP WITH TIME ZONE NULL
           the last `old_slot` transition (paces the retries).

The new statuses (`restoring`, `old_slot_lost`, `rebooked`), the attention reasons
(`service_diff_rebook_due`, `price_change_no_email`) and the reminder kind `price_change` need no
schema change (plain VARCHAR(24) / VARCHAR(32) / VARCHAR(16), no CHECK).

ADDITIVE: both columns are nullable and nothing is dropped, so the old code (API and worker)
runs unchanged on the new schema. Deploy order: `alembic upgrade head` from the NEW image (both
services still on the old code) -> `secretaria_api` AND `secretaria-worker` together -> the front.

Rollback: the OLD code on both services first, then `alembic downgrade` to c9e4a2f7b3d1. Do it
only with zero suspended changes in flight - `SELECT count(*) FROM deposit_adjustments WHERE
old_slot IS NOT NULL AND status IN ('awaiting_payment', 'awaiting_choice', 'restoring',
'old_slot_lost')` must be 0: the old code does not know those states, and a consultation whose
event was deleted would stay out of the agenda with nobody to bring it back.

Revision ID: d5a8c3e1f7b9
Revises: c9e4a2f7b3d1
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5a8c3e1f7b9"
down_revision: str | None = "c9e4a2f7b3d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deposit_adjustments", sa.Column("old_slot", sa.String(length=16), nullable=True)
    )
    op.add_column(
        "deposit_adjustments",
        sa.Column("old_slot_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deposit_adjustments", "old_slot_at")
    op.drop_column("deposit_adjustments", "old_slot")
