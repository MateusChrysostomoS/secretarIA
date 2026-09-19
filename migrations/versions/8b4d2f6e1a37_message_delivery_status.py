"""messages: delivery state (delivered / read / failed) + updated_at poll cursor

The console and the Portal draw WhatsApp-style ticks (enviado / entregue / lido /
falhou), but no message ever moved past "enviado": nothing on the row said otherwise.
This adds the FACTS the status is derived from (models/message.py::status_of), never a
status column of its own:

    ADD messages.delivered_at    TIMESTAMPTZ NULL
    ADD messages.read_at         TIMESTAMPTZ NULL
    ADD messages.failed_at       TIMESTAMPTZ NULL
    ADD messages.failure_reason  VARCHAR(255) NULL   "<Meta code>: <title>", no PII
    ADD messages.updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
    CREATE INDEX ix_messages_conversation_updated_at ON messages (conversation_id, updated_at)

`updated_at` becomes the Brain-Message transcript's poll cursor (`since`), so a message
already fetched comes back when its ticks move. See
docs/CHECKPOINT_brain_message_status_entrega.md.

BACKFILL - both in this migration, because a wrong default is visible:
  * `updated_at = created_at` for every existing row. Left at the migration's now(),
    every old row would look "just changed": the first poll after the deploy, with a
    cursor older than the migration, would receive up to a page of the whole history
    again, and today's Portal appends what it receives.
  * `delivered_at = created_at` for rows of `brain_message` patients - the rule new rows
    follow (delivery on that channel IS persistence). WhatsApp rows stay NULL: no
    receipt was ever kept for them, and "enviado" is the honest answer.

DEPLOY ORDER - the database moves FIRST, same reason as 5e1f9a3c7d20: the ORM names every
mapped column on every read and write of `messages`. The columns are inert to old code
(`updated_at` has a server default, so old INSERTs still satisfy NOT NULL):

    1. `alembic upgrade head` from the NEW image (one-off), both services on old code;
    2. deploy `secretaria-worker` AND `secretaria_api` (the worker writes WhatsApp
       statuses and Brain-Message rows, the API serves them and takes the read marks).

Rollback: the OLD code on both services first, then `alembic downgrade 5e1f9a3c7d20`.
What is lost is the delivery history; nothing else reads these columns.

Revision ID: 8b4d2f6e1a37
Revises: 5e1f9a3c7d20
Create Date: 2026-09-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8b4d2f6e1a37"
down_revision: str | None = "5e1f9a3c7d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_messages_conversation_updated_at"


def upgrade() -> None:
    op.add_column("messages", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("read_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("messages", sa.Column("failure_reason", sa.String(255), nullable=True))
    # Nullable first so the backfill can run, then NOT NULL. The server default stays:
    # it is what keeps an INSERT from the OLD code valid during the rollout window.
    op.add_column(
        "messages",
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
    )
    op.execute("UPDATE messages SET updated_at = created_at")
    op.execute(
        "UPDATE messages SET delivered_at = created_at "
        "WHERE conversation_id IN ("
        "  SELECT c.id FROM conversations c JOIN patients p ON p.id = c.patient_id"
        "  WHERE p.channel = 'brain_message'"
        ")"
    )
    with op.batch_alter_table("messages") as batch:
        batch.alter_column("updated_at", existing_type=sa.DateTime(timezone=True), nullable=False)
    op.create_index(_INDEX, "messages", ["conversation_id", "updated_at"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="messages")
    for column in ("updated_at", "failure_reason", "failed_at", "read_at", "delivered_at"):
        op.drop_column("messages", column)
