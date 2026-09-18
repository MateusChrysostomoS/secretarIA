"""messages.attachment: the one file a Brain-Message message carries

The Portal (Brain-Message channel) lets a patient send a file and the clinic's staff
send one back - an image (JPEG/PNG/WEBP/GIF) or a PDF, up to 20 MiB, one per message,
like WhatsApp. The bytes live in secretarIA's own R2 bucket (services/media_storage.py);
the row keeps where they are and what they are:

    ADD    messages.attachment  JSON NULL
           {"r2_object_key", "content_type", "size_bytes", "filename"}
           (core/attachments.py::StoredAttachment)
    CREATE INDEX ix_messages_attachment_created_at ON messages (created_at)
           WHERE attachment IS NOT NULL

One JSON column, not an `attachments` table - a message has at most one file, so a
table would be a 1:1 join with nothing to gain, and it keeps the parallel with
`messages.interactive` (4c8e2a7f1b93) that the next reader will look for.

The partial index serves the persisted daily byte quota
(api/internal.py::_enforce_attachment_quota), which sums the attachment rows of the
last 24h per patient and per clinic. Only rows WITH a file enter it, so it stays tiny,
and the clinic-wide sum never scans every message the clinic ever exchanged. The ORM
maps the column with `none_as_null=True`, so a message without a file is SQL NULL,
never a JSON 'null' that would satisfy the predicate. A plain (not CONCURRENT) build:
every existing row is NULL and the table is pre-launch sized; on a large table, build
it CONCURRENTLY by hand before upgrading.

No backfill: every existing message has no file.

DEPLOY ORDER - the database moves FIRST, same reason as 4c8e2a7f1b93: the ORM names
every mapped column on every read and write of `messages`, so a process running the
new model against a schema without this column fails every inbound turn and every
console thread. The column and the index are inert to the old code:

    1. `alembic upgrade head` from the NEW image (one-off), while both services still
       run the old code;
    2. deploy `secretaria-worker` AND `secretaria_api` (the worker writes the column
       for a patient's file, the API writes it for staff and serves it).

Rollback narrows, so it goes the other way round: the OLD code on both services
first, then `alembic downgrade 4c8e2a7f1b93`. What is lost is the link from those
messages to their files (their `body` keeps the caption or "[anexo: <name>]"); the
objects stay in the bucket, unreferenced, until a lifecycle rule or a manual sweep.

Revision ID: 5e1f9a3c7d20
Revises: 4c8e2a7f1b93
Create Date: 2026-09-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "5e1f9a3c7d20"
down_revision: str | None = "4c8e2a7f1b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_messages_attachment_created_at"
_HAS_FILE = sa.text("attachment IS NOT NULL")


def upgrade() -> None:
    # Nullable, no default: metadata-only on Postgres, NULL for every row that exists.
    op.add_column("messages", sa.Column("attachment", sa.JSON(), nullable=True))
    op.create_index(
        _INDEX,
        "messages",
        ["created_at"],
        postgresql_where=_HAS_FILE,
        sqlite_where=_HAS_FILE,
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="messages")
    op.drop_column("messages", "attachment")
