"""messages.interactive_reply_id: a tap's id, stored apart from its title

Until this revision `Message.body` carried BOTH halves of a tapped list row in
one string - "<title> (<payload>)", built by `schemas/webhook.py::
extract_inbound_body` so the flow router (and the agent's history) could read
the payload back out of the text. That same string is what the staff console
and the patient portal render, which is how a doctor tap showed up there as
"Dr. Fulano (8faa12e1-…)" (found live, 2026-09-10).

The split: `body` keeps the title the patient saw, and this column keeps the
raw id of the control they tapped (`interactive.button_reply.id` /
`list_reply.id`). Machine readers recompose the old string with
`schemas/webhook.py::inbound_routing_text`; nothing displays this column.

    ADD  messages.interactive_reply_id  TEXT NULL

No backfill, on purpose. Rows written before this revision still carry their
payload inside `body` and a NULL here, and `inbound_routing_text` passes a NULL
id through untouched, so the agent reads them back exactly as before. Rewriting
those bodies would edit conversation records (LGPD) to change what old console
pages show - out of scope for a routing fix.

DEPLOY ORDER - the database moves FIRST, and here that is not a nicety. The ORM
never `SELECT *`s and never leaves an unset nullable column out of an INSERT:
SQLAlchemy names every mapped column on every `messages` read AND write. So a
process running the new model against a schema WITHOUT this column fails every
read and write of `messages` - on the worker that is every inbound turn, every
outbound record and every agent history load. The column itself is inert to
the old code (it maps nothing it does not know), so the safe sequence is:

    1. `alembic upgrade head` from the NEW image (one-off command), while both
       services still run the old code;
    2. deploy `secretaria_api` and `secretaria-worker`.

If a one-off is not available: API first, migrate immediately (until then only
the console/portal message lists and the staff send fail), worker LAST. Never
the worker before the column exists.

Rollback is symmetric in schema and slightly lossy in data: downgrade() drops
the column, and rows written after the upgrade keep a title-only `body`, so the
old code's agent history reads those taps without their payload (e.g. a slot
tap without its ISO). Nothing is misrouted by that - the router only reads the
CURRENT turn, which the old code composes itself.

Revision ID: 9d3b7e1f5a2c
Revises: aeeeb64360f5
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d3b7e1f5a2c"
down_revision: str | None = "aeeeb64360f5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: a metadata-only change on Postgres (no table
    # rewrite), and NULL is the true value for every row that exists today.
    op.add_column("messages", sa.Column("interactive_reply_id", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "interactive_reply_id")
