"""conversations.flow_edit_draft: the in-progress "Alterar Dados" draft.

TASK-032 R6 (docs/superpowers/specs/2026-10-07-lembretes-alterar-dados-design.md §5.2).
A patient who taps "Alterar Dados" on a reminder changes date, time, service,
doctor, convênio or patient through several steps; nothing touches the real
appointment until the final Confirmar. The draft (current vs original values) is
parked on the conversation:

    ADD  conversations.flow_edit_draft  JSON NULL

NULL whenever no edit is in progress. Written unconditionally by
`_apply_flow_result` like every flow field and carried by
`flow_router._carry_edit_draft` only while the conversation stays in
`EDIT_BOOKING`. `FlowState.EDIT_BOOKING` itself needs no migration (flow_state is a
VARCHAR(32) with no CHECK).

DEPLOY ORDER - the database moves FIRST: the ORM names every mapped column on every
read and write of `conversations`, so new code against a schema WITHOUT this column
fails every turn. The column is inert to the old code:

    1. `alembic upgrade head` from the NEW image (one-off), both services on the old code;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity `match`).

Rollback narrows: the OLD code on both services first, then `alembic downgrade`.
The only data lost is an edit in progress (the appointment itself is never touched
by a draft).

Revision ID: b1c4e7a2d9f3
Revises: a7e2c9d4f1b6
Create Date: 2026-10-07
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c4e7a2d9f3"
down_revision: str | None = "a7e2c9d4f1b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: metadata-only on Postgres, and NULL is the true value
    # for every row that exists today.
    op.add_column("conversations", sa.Column("flow_edit_draft", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("conversations", "flow_edit_draft")
