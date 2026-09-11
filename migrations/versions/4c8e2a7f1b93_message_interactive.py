"""messages.interactive: what an interactive outbound card offered

Until this revision an outbound interactive message (reply buttons, a list)
survived only as `Message.body`, flattened for the agent's history by
`workers/tasks.py::_bubble_history_body`: "<body>\n(opções: A, B, C)" for a list
or a menu, the bare body for the greeting, the LGPD notice and a
confirm/cancel card. That string is also what the staff console renders, so a
list of doctors showed up there as a line of text and the LGPD button did not
show up at all (found live, 2026-09-10).

    ADD  messages.interactive  JSON NULL

`body` is untouched - it stays the agent's history contract. This column
carries the structure for human screens: {"kind": "buttons" | "list", "body",
"options": [{"id", "title", "description"}], "button_label", "section_title"},
recorded from the same arguments the Graph API payload is built from
(services/whatsapp.py::interactive_buttons_record / interactive_list_record).
Written only where the channel really delivered a control (WhatsApp); a
Brain-Message patient receives the options as text, so those rows stay NULL.

No backfill, on purpose. Old rows keep NULL and render as text, exactly as
today. Rebuilding structure by parsing "(opções: ...)" back out of `body`
would be a guess (a label can contain ", ", the ids are not there at all) and
would edit conversation records (LGPD) to change what old console pages show.

DEPLOY ORDER - the database moves FIRST, for the same reason as 9d3b7e1f5a2c:
the ORM names every mapped column on every read and write of `messages`, so a
process running the new model against a schema WITHOUT this column fails every
inbound turn, every outbound record and every console thread. The column is
inert to the old code, so:

    1. `alembic upgrade head` from the NEW image (one-off), while both services
       still run the old code - this also applies 9d3b7e1f5a2c if that one is
       not applied yet;
    2. deploy `secretaria_api` AND `secretaria-worker` (the worker writes the
       column, the API serves it).

Rollback narrows, so it goes the other way round: the OLD code on both services
first, then `alembic downgrade 9d3b7e1f5a2c` - dropping the column while a
service still maps it fails every read of `messages`. The only data lost is the
structure recorded in between; those rows fall back to their text `body`.

Revision ID: 4c8e2a7f1b93
Revises: 9d3b7e1f5a2c
Create Date: 2026-09-11
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "4c8e2a7f1b93"
down_revision: str | None = "9d3b7e1f5a2c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: a metadata-only change on Postgres (no table
    # rewrite), and NULL is the true value for every row that exists today.
    op.add_column("messages", sa.Column("interactive", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("messages", "interactive")
