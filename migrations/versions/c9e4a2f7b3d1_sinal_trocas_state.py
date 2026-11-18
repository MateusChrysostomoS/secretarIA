"""sinal_trocas_state: the state of a change on a consultation whose sinal is paid.

TASK-085 R / S0 (tasks/TASK-085/sinal-trocas: SPEC D-R1..D-R9, contrato.md). Three changes:

    ALTER  deposit_adjustments.mode           ADD VARCHAR(16) NULL
           NULL = a legacy row (today's semantics); "suspend" (D-R4, a clinic that books only
           after the sinal: the old consultation is suspended while the Pix of the difference
           is open); "applied" (D-R5: the change stood at once, the Pix only collects).
    ALTER  booking_holds.conversation_id      NOT NULL -> NULL
           the hold of the NEW window of a suspended change belongs to no conversation.
    ALTER  booking_holds.adjustment_id        ADD UUID NULL, indexed,
           FK -> deposit_adjustments.id ON DELETE CASCADE (a difference hold without its
           adjustment has nobody to apply or release it - the `conversation_id` rule).

The new statuses (`awaiting_choice`, `kept_old`) and the attention reason `service_diff_unpaid`
need no schema change (plain VARCHAR(24) / VARCHAR(32)).

ADDITIVE: nothing new is NOT NULL and no column is dropped, so the old code (API and worker)
runs unchanged on the new schema. Deploy order: `alembic upgrade head` from the NEW image (both
services still on the old code) -> `secretaria_api` AND `secretaria-worker` together (the
worker sends the choice buttons and runs the expiry sweep) -> the front.

Rollback: the OLD code on both services first, then `alembic downgrade` to b7d3f1a8c5e2. Do it
only when no change is in flight - `SELECT count(*) FROM deposit_adjustments WHERE mode IS NOT
NULL AND status IN ('awaiting_payment', 'awaiting_choice')` must be 0: the old code reads a
`mode` row as a legacy one and does not know `awaiting_choice`. The downgrade DELETES the holds
without a conversation (the difference holds: short-lived, and impossible under the old NOT
NULL) before restoring the constraint.

Revision ID: c9e4a2f7b3d1
Revises: b7d3f1a8c5e2
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c9e4a2f7b3d1"
down_revision: str | None = "b7d3f1a8c5e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FK_NAME = "fk_booking_holds_adjustment_id"
INDEX_NAME = "ix_booking_holds_adjustment_id"


def upgrade() -> None:
    op.add_column("deposit_adjustments", sa.Column("mode", sa.String(length=16), nullable=True))
    # batch_alter_table: plain ALTER TABLE statements on Postgres; a table rebuild on SQLite,
    # which cannot ALTER a column's nullability nor add a named foreign key (the test engine).
    with op.batch_alter_table("booking_holds") as batch:
        batch.alter_column("conversation_id", existing_type=sa.Uuid(), nullable=True)
        batch.add_column(sa.Column("adjustment_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            FK_NAME, "deposit_adjustments", ["adjustment_id"], ["id"], ondelete="CASCADE"
        )
    op.create_index(INDEX_NAME, "booking_holds", ["adjustment_id"])


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="booking_holds")
    op.execute("DELETE FROM booking_holds WHERE conversation_id IS NULL")
    with op.batch_alter_table("booking_holds") as batch:
        batch.drop_constraint(FK_NAME, type_="foreignkey")
        batch.drop_column("adjustment_id")
        batch.alter_column("conversation_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_column("deposit_adjustments", "mode")
