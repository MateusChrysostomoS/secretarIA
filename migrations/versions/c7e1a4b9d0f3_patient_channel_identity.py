"""Patient channel identity: channel + external_id, wa_id nullable.

Purely additive groundwork for the Brain-Message channel. Nothing reads the two
new columns yet - the pipeline that does is a separate, later change - so this
revision is a no-op for behaviour and a widening for the schema:

    ADD    patients.channel      NOT NULL DEFAULT 'whatsapp'
    ADD    patients.external_id  NULL, backfilled to wa_id
    ALTER  patients.wa_id        NOT NULL -> NULL
    ADD    uq_patients_tenant_channel_external_id

The order is "widen before use": the database moves first and the code that
reads the columns follows, because a column nobody reads is inert.

uq_patients_tenant_wa_id is deliberately NOT dropped here. After the backfill
the two constraints describe the same tuple for every existing row - proven by
query, not by argument - so the old one is redundant on paper. It is still
load-bearing in practice: the API and the arq worker deploy independently, and
a worker still running the pre-channel model inserts a patient with a NULL
external_id, which does not bind the new constraint (NULLs compare distinct).
Until both services are on the new code, the old constraint is the only thing
keeping one phone number to one row.

Rollback is symmetric only while no brain_message row exists. Once one does,
downgrade() cannot restore wa_id NOT NULL and will fail loudly rather than
invent a phone number - the honest rollback from that point is forward.

Revision ID: c7e1a4b9d0f3
Revises: a1b2c3d4e5f6
Create Date: 2026-09-08
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7e1a4b9d0f3"
down_revision: str | None = "a1b2c3d4e5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # NOT NULL + server_default in one shot: Postgres 11+ fills existing rows
    # without rewriting the table, so this IS the channel backfill - every row
    # that predates the column was WhatsApp, because nothing else existed.
    op.add_column(
        "patients",
        sa.Column("channel", sa.String(length=32), nullable=False, server_default="whatsapp"),
    )
    op.add_column("patients", sa.Column("external_id", sa.String(length=64), nullable=True))
    # WhatsApp's per-channel identifier IS the wa_id. Guarded by IS NULL so a
    # re-run cannot clobber a value some later code already chose.
    op.execute("UPDATE patients SET external_id = wa_id WHERE external_id IS NULL")
    op.alter_column(
        "patients",
        "wa_id",
        existing_type=sa.String(length=32),
        nullable=True,
    )
    op.create_unique_constraint(
        "uq_patients_tenant_channel_external_id",
        "patients",
        ["tenant_id", "channel", "external_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_patients_tenant_channel_external_id", "patients", type_="unique")
    # Fails loudly if a channel="brain_message" row exists - by design. There is
    # no wa_id to restore for a patient who never had a phone number.
    op.alter_column(
        "patients",
        "wa_id",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.drop_column("patients", "external_id")
    op.drop_column("patients", "channel")
