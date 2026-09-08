"""conversation_pii_token_maps: re-identification key per conversation

Backs services/pii_pseudonymization.py, the scrub/re-hydrate step that now sits
between this repo and OpenAI (ai/graph.py + ai/scoped_help.py). One row per
conversation, holding `{token: valor real}`; the map GROWS turn by turn as new
phones/CPFs/e-mails show up in free text.

DEPLOY ORDER - THE SAFE DIRECTION, UNLIKE e3b7c1d5a9f2 BELOW IT
---------------------------------------------------------------
This ADDS a table. A process running the pre-migration code never touches it,
so the migration is safe to run FIRST and the code can follow. That is the
inverse of the DROP in e3b7c1d5a9f2 - do not copy that revision's ordering.

The reverse is what needs care: code that reads this table deployed against a
database without it takes out every LLM turn. So: migration first, then BOTH
`secretaria_api` and `secretaria-worker` (they deploy independently and have
diverged in production before - 2026-08-16). The worker is the one that
actually answers patients, and ai/graph.py runs inside it.

`downgrade()` DROPS the table, and with it every token->value binding. Any
conversation mid-flight at that moment loses its key: history already masked in
a prior turn re-tokenizes from scratch, so a token the model still echoes back
can no longer be resolved. Schema-reversible, not data-reversible.

Revision ID: a1b2c3d4e5f6
Revises: e3b7c1d5a9f2
Create Date: 2026-09-07 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1b2c3d4e5f6"
down_revision: str | None = "e3b7c1d5a9f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "conversation_pii_token_maps",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("tokens", sa.JSON(), server_default=sa.text("'{}'"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        # CASCADE, so wiping a patient's context (/dangerously-remove-context
        # deletes the Patient, which cascades to conversations) takes the
        # re-identification key with it without any extra wiring.
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("conversation_id"),
    )


def downgrade() -> None:
    op.drop_table("conversation_pii_token_maps")
