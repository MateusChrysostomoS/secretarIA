"""conversations.flow_selected_unit_id: the unit a booking in progress happens at.

TASK-083 spec §8. A professional who attends at two or more units of the clinic makes the
patient pick one (`flow_router` step "awaiting_unit"); the day and time lists that follow use
that unit's hours, and the booking (appointment, local event, hold) records it. Between the
pick and the "Confirmar" the choice lives on the conversation, like the doctor and the convênio:

    ADD  conversations.flow_selected_unit_id  UUID NULL
         FK -> units.id  ON DELETE SET NULL

NULL whenever no unit was chosen (a doctor without units, which is every clinic today) and on
every flow result that leaves the booking (services/flow_router.py::_carry_unit). SQLite (local
tests) cannot ALTER a constraint in, so there the column is added bare; Postgres gets the FK.

DEPLOY ORDER - the database moves FIRST: the ORM names every mapped column on every read and
write of `conversations`, so a process running the new model against a schema WITHOUT this
column fails every turn. The column is inert to the old code, so:

    1. `alembic upgrade head` from the NEW image (one-off), both services still on the old code
       (together with e4a8c2f6b1d9 when both ship in the same deploy);
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity `match`).

Rollback: the OLD code on both services first, then `alembic downgrade` to e4a8c2f6b1d9. Only a
unit picked in a booking still in progress is lost; that patient is asked for it again.

Revision ID: b5d9e3f7a1c4
Revises: e4a8c2f6b1d9
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b5d9e3f7a1c4"
down_revision: str | None = "e4a8c2f6b1d9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: metadata-only on Postgres (no table rewrite).
    fk_args: list = []
    if op.get_bind().dialect.name != "sqlite":
        fk_args.append(sa.ForeignKey("units.id", ondelete="SET NULL"))
    op.add_column(
        "conversations",
        sa.Column("flow_selected_unit_id", sa.Uuid(), *fk_args, nullable=True),
    )


def downgrade() -> None:
    # Dropping the column also drops its unnamed FK constraint on Postgres.
    op.drop_column("conversations", "flow_selected_unit_id")
