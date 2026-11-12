"""Multi-unit professionals + one shared agenda per person - two new tables, three nullable columns.

TASK-083. A doctor who works at two clinics that both use the secretarIA has two `professionals`
rows (one per tenant) and, until now, two agendas that could not see each other. An `agendas`
row is the identity those rows share (`person_key` = the brain-api account id); and a
professional can now be linked to the units of their own clinic, with an optional own schedule
per unit. Everything is additive and inert: no row is linked to anything until the code that
ties them (services/agenda_linking.py, the hub) runs, and a professional with no `agenda_id`
behaves exactly as before.

    CREATE TABLE agendas (
        id          UUID PRIMARY KEY
        person_key  VARCHAR(64) NOT NULL UNIQUE         -- str(StaffAccount.id)
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    CREATE TABLE professional_units (
        professional_id UUID NOT NULL -> professionals ON DELETE CASCADE
        unit_id         UUID NOT NULL -> units         ON DELETE CASCADE
        business_hours  JSON NULL            -- NULL = inherit the professional's hours
        is_active       BOOLEAN NOT NULL DEFAULT true
        sort_order      INTEGER NOT NULL DEFAULT 0
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
        PRIMARY KEY (professional_id, unit_id)
    )
    ALTER TABLE professionals    ADD COLUMN agenda_id UUID NULL -> agendas ON DELETE SET NULL
    + ix_professionals_agenda_id
    ALTER TABLE calendar_events  ADD COLUMN unit_id UUID NULL   -- historical label, no FK
    ALTER TABLE booking_holds    ADD COLUMN unit_id UUID NULL   -- historical label, no FK

No data is touched or backfilled here: linking existing professionals is a separate, idempotent
step (`scripts/reconcile_agendas.py`, after the deploy) - it needs brain-api.

DEPLOY ORDER - the database moves FIRST (the new code selects the columns):
    1. `alembic upgrade head` from the NEW image (one-off), both services still old - the old
       code never reads the new tables/columns, so this step is invisible to it;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. brain-api (`professional-accounts` route) BEFORE running the reconcile script, then
       `scripts/reconcile_agendas.py`; then the Brain-Message front.

Rollback: OLD code on both services first, then `alembic downgrade` (drops the two tables and
the three columns - the agenda links and the per-unit schedules are lost; the appointments,
events and holds themselves are untouched).

Revision ID: e4a8c2f6b1d9
Revises: d2f6b8a4c1e7
Create Date: 2026-11-12
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4a8c2f6b1d9"
down_revision: str | None = "d2f6b8a4c1e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "agendas",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("person_key", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("person_key", name="uq_agendas_person_key"),
    )
    op.create_table(
        "professional_units",
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column("business_hours", sa.JSON(), nullable=True),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["unit_id"], ["units.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("professional_id", "unit_id"),
    )
    # batch_alter_table: plain ALTER TABLE statements on Postgres; a table rebuild on SQLite,
    # which cannot ALTER in a named foreign key (the test engine).
    with op.batch_alter_table("professionals") as batch:
        batch.add_column(sa.Column("agenda_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_professionals_agenda_id", "agendas", ["agenda_id"], ["id"], ondelete="SET NULL"
        )
    op.create_index("ix_professionals_agenda_id", "professionals", ["agenda_id"])
    op.add_column("calendar_events", sa.Column("unit_id", sa.Uuid(), nullable=True))
    op.add_column("booking_holds", sa.Column("unit_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    op.drop_column("booking_holds", "unit_id")
    op.drop_column("calendar_events", "unit_id")
    op.drop_index("ix_professionals_agenda_id", table_name="professionals")
    # batch_alter_table: see upgrade().
    with op.batch_alter_table("professionals") as batch:
        batch.drop_constraint("fk_professionals_agenda_id", type_="foreignkey")
        batch.drop_column("agenda_id")
    op.drop_table("professional_units")
    op.drop_table("agendas")
