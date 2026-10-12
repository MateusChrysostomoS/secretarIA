"""conversations.flow_draft: the AI booking draft parked while pra-quem is answered.

TASK-030 P2 (docs/superpowers/specs/2026-10-02-ia-entra-em-qualquer-etapa-design.md §4.3).
When the AI hands a booking back and "Essa consulta é pra você?" is still unanswered, the
workflow asks it first. What the AI had already resolved (service, doctor, convênio, day,
time) had nowhere to wait: during those steps `flow_selected_type` holds the pra-quem entry
marker. The draft waits here:

    ADD  conversations.flow_draft  JSON NULL
         {"t", "p", "i", "w", "d", "h", "saved_at"}   (services/booking_draft.py)

No third party's name ever lands here - "w" is only "self"/"other". Every flow result that
does not name it clears it, and the two silence floors (workers/shared/state_expiry.py)
clear it too.

DEPLOY ORDER - the database moves FIRST (same reason as 4c8e2a7f1b93): the ORM names every
mapped column on every read and write of `conversations`, so a process running the new
model against a schema WITHOUT this column fails every turn. The column is inert to the
old code, so:

    1. `alembic upgrade head` from the NEW image (one-off), both services still on the old
       code;
    2. deploy `secretaria_api` AND `secretaria-worker` together (README: "Deploy both
       services, or neither"; `GET /build` must report parity `match`).

Rollback narrows, so it goes the other way round: the OLD code on both services first,
then `alembic downgrade b8d3f1a6c2e5`. The only data lost is a draft parked in between; that
patient's pra-quem answer then continues the plain button flow.

Revision ID: e7d3c1a9b5f2
Revises: b8d3f1a6c2e5 (appointment_reminders_foundation, main at P2a integration)
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e7d3c1a9b5f2"
down_revision: str | None = "b8d3f1a6c2e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: metadata-only on Postgres (no table rewrite), and NULL is the
    # true value for every row that exists today.
    op.add_column("conversations", sa.Column("flow_draft", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "flow_draft")
