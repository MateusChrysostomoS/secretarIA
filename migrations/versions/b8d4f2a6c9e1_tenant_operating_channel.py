"""Where the secretarIA serves each clinic's patients: one NOT NULL column, additive.

TASK-084 (owner, 2026-11-14): the clinic chooses its secretarIA's operating channel -
the Portal Brain chat or WhatsApp. Today only the Portal is released, so every clinic,
existing and new, reads `brain_message`:

    ALTER TABLE tenants ADD COLUMN operating_channel VARCHAR(16) NOT NULL DEFAULT 'brain_message'
    ALTER TABLE tenants ADD CONSTRAINT ck_tenants_operating_channel
        CHECK (operating_channel IN ('brain_message', 'whatsapp'))

Written only by `PATCH /tenants/me/operating-channel`; nothing on the message path reads it
(conversations keep answering on the channel they arrived on).

Batch mode so the same script runs on the test suite's SQLite (which cannot ALTER a
constraint); on Postgres `batch_alter_table` issues the plain ALTERs above, no table copy.

DEPLOY ORDER - the database moves FIRST (the new code selects the column):
    1. `alembic upgrade head` from the NEW image, both services still old;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. then the Brain-Message front (the "Onde a secretarIA atende" block in Contexto).

Rollback: old code on both services first, then `alembic downgrade`.

OTHER BRANCHES: TASK-083 (`e4a8c2f6b1d9`) also sits on `d2f6b8a4c1e7`, and TASK-074
(`c4e8a1d6b3f9`) on its own base. Whichever lands after this one adds an `alembic merge`
revision (or re-parents) so the chain keeps a single head.

Revision ID: b8d4f2a6c9e1
Revises: d2f6b8a4c1e7
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f2a6c9e1"
down_revision: str | None = "d2f6b8a4c1e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.add_column(
            sa.Column(
                "operating_channel",
                sa.String(length=16),
                nullable=False,
                server_default="brain_message",
            )
        )
        batch.create_check_constraint(
            "ck_tenants_operating_channel",
            "operating_channel IN ('brain_message', 'whatsapp')",
        )


def downgrade() -> None:
    with op.batch_alter_table("tenants") as batch:
        batch.drop_constraint("ck_tenants_operating_channel", type_="check")
        batch.drop_column("operating_channel")
