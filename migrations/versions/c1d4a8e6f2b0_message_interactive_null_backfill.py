"""messages.interactive: backfill the JSON literal 'null' to real SQL NULL

`interactive` (4c8e2a7f1b93) was mapped as plain `JSON` (`none_as_null=False`, the
SQLAlchemy default), and `services/channel_sender.py::BrainMessageSender._record` passes
`interactive=None` explicitly for every text message. That combination writes the JSON
literal `'null'` into the column, not SQL NULL - and `'null'` still satisfies `WHERE
interactive IS NOT NULL`. `workers/tasks.py::_validated_brain_message_reply_id` selects the
`BRAIN_MESSAGE_TAP_WINDOW` (10) most recent OUTBOUND rows `WHERE interactive IS NOT NULL`
to decide which cards a Brain-Message tap may reference - with every text message also
matching that predicate, a real card could be pushed out of its own window by text
messages alone, and a tap on it would be dropped and misrouted as typed text.

The model now maps the column `none_as_null=True` (models/message.py, same as `attachment`,
5e1f9a3c7d20), so every NEW row is correct from here on. This migration is the backfill for
rows already written under the old mapping:

    UPDATE messages SET interactive = NULL WHERE interactive holds the JSON literal null

No column, no index, no type change - `none_as_null` is a SQLAlchemy-side serialization
flag, not a distinct database column type, so there is nothing to ALTER. `CAST(interactive
AS TEXT) = 'null'` works identically on Postgres (JSON's text cast is its literal
representation) and on SQLite (the column is stored as TEXT already), so one statement
covers both engines without a dialect branch.

DEPLOY ORDER - unlike the two migrations before it, this one is NOT a prerequisite for the
new code: the app already writes and reads `interactive` today, and `none_as_null` changes
future writes, not the column shape. Safe to run before, with, or after the next
`secretaria-worker`/`secretaria_api` deploy; running it late only delays when old text rows
stop counting as cards.

Idempotent: a re-run finds nothing left with the literal `'null'` and updates zero rows.

Revision ID: c1d4a8e6f2b0
Revises: 8b4d2f6e1a37
Create Date: 2026-09-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1d4a8e6f2b0"
down_revision: str | None = "8b4d2f6e1a37"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_BACKFILL = "UPDATE messages SET interactive = NULL WHERE CAST(interactive AS TEXT) = 'null'"


def upgrade() -> None:
    op.execute(_BACKFILL)


def downgrade() -> None:
    # Not reversible in the meaningful sense: a real SQL NULL and the JSON literal
    # 'null' are indistinguishable once the app reads them back as `None` either way.
    # Nothing to undo - `none_as_null` only changes how future writes are stored.
    pass
