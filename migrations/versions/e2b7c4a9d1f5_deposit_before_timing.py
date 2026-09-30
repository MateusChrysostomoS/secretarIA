"""deposit before timing: the conversation a charge waits for, notice stamps, alert stamp

TASK-017 (F2 of TASK F, docs/CHECKPOINT_pix_before_timing.md). Purely ADDITIVE, widen-before-
narrow (skill frozen-contract-migration): three nullable columns, no default needed, nothing
dropped, nothing rewritten. Code that predates this revision (the API or the worker, whichever
is deployed first) never reads them and keeps working untouched.

- `pix_deposits.conversation_id` - FK `conversations.id` ON DELETE SET NULL, indexed. In `before`
  timing the charge exists before any appointment: this is how the webhook-driven side finds the
  chat to notify and to move out of `AWAITING_DEPOSIT`.
- `pix_deposits.expired_notice_sent_at` - stamped AFTER the "Pix expired" notice was delivered, so a
  replay never sends it twice and a failed send is retried (`paid_notice_sent_at`, its twin, is
  already in `c8a5d3f1b7e2`).
- `payment_attention_items.alerted_at` - stamped when the clinic was e-mailed about the item, so
  an item is announced once (the sweep only reads rows where it is NULL).

DEPLOY ORDER (both services map `PixDeposit` and `PaymentAttentionItem`): this migration ->
API + worker BOTH on the new code (deploy_parity=match) -> Brain-Message-Frontend last (F4).

Revision ID: e2b7c4a9d1f5
Revises: c8a5d3f1b7e2
Create Date: 2026-09-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2b7c4a9d1f5"
down_revision: str | None = "c8a5d3f1b7e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("pix_deposits", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        "fk_pix_deposits_conversation_id",
        "pix_deposits",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_pix_deposits_conversation_id", "pix_deposits", ["conversation_id"])
    op.add_column(
        "pix_deposits",
        sa.Column("expired_notice_sent_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payment_attention_items",
        sa.Column("alerted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("payment_attention_items", "alerted_at")
    op.drop_column("pix_deposits", "expired_notice_sent_at")
    op.drop_index("ix_pix_deposits_conversation_id", table_name="pix_deposits")
    op.drop_constraint("fk_pix_deposits_conversation_id", "pix_deposits", type_="foreignkey")
    op.drop_column("pix_deposits", "conversation_id")
