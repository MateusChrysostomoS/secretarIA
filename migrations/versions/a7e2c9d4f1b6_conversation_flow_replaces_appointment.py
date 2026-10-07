"""conversations.flow_replaces_appointment_id: the booking "Marcar outra consulta" replaces.

TASK-032 R3 (docs/superpowers/specs/2026-10-03-lembretes-e-confirmacao-design.md §4.3).
A patient who taps "Cancelar" on a reminder and then "Marcar outra" books a NEW
appointment while the original stays live. The original may be cancelled only
when the new booking is confirmed (owner's decision 7), so the conversation has
to remember which appointment the booking in progress replaces:

    ADD  conversations.flow_replaces_appointment_id  UUID NULL
         FK -> appointments.id  ON DELETE SET NULL

Same shape as flow_managing_appointment_id (e51cd84e1959). NULL whenever no
replacement is in progress; every flow result that does not carry it clears it
(services/flow_router.py::_carry_replacement), and the silence floors clear it.

SQLite (local tests) cannot ALTER a constraint in, so there the column is added
bare; Postgres gets the FK.

DEPLOY ORDER - the database moves FIRST: the ORM names every mapped column on
every read and write of `conversations`, so a process running the new model
against a schema WITHOUT this column fails every turn. The column is inert to
the old code, so:

    1. `alembic upgrade head` from the NEW image (one-off), both services still
       on the old code;
    2. deploy `secretaria_api` AND `secretaria-worker` together (README: "Deploy
       both services, or neither"; `GET /build` must report parity `match`).

Rollback narrows, so it goes the other way round: the OLD code on both services
first, then `alembic downgrade` to the previous revision. The only data lost is
a replacement in progress; that patient's new booking then simply does not
cancel the original (the safe direction).

Revision ID: a7e2c9d4f1b6
Revises: e7d3c1a9b5f2
Create Date: 2026-10-03
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7e2c9d4f1b6"
down_revision: str | None = "e7d3c1a9b5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable, no default: metadata-only on Postgres (no table rewrite), and NULL
    # is the true value for every row that exists today.
    fk_args: list = []
    if op.get_bind().dialect.name != "sqlite":
        fk_args.append(sa.ForeignKey("appointments.id", ondelete="SET NULL"))
    op.add_column(
        "conversations",
        sa.Column("flow_replaces_appointment_id", sa.Uuid(), *fk_args, nullable=True),
    )


def downgrade() -> None:
    # Dropping the column also drops its unnamed FK constraint on Postgres.
    op.drop_column("conversations", "flow_replaces_appointment_id")
