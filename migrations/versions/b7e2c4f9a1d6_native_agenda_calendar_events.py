"""Native agenda: the secretarIA's own `calendar_events` table - one new table, additive.

Founder's decision 2026-10-20: the agenda works without Google Calendar. An agenda no
Google credential covers keeps its events here (services/local_calendar.py); a
connected one keeps using Google exactly as before.

    CREATE TABLE calendar_events (
        id               VARCHAR(64) PRIMARY KEY       -- "loc_<uuid hex>"
        tenant_id        UUID NOT NULL -> tenants ON DELETE CASCADE
        professional_id  UUID NULL     -> professionals ON DELETE CASCADE  (NULL = clinic)
        summary, description TEXT NULL
        start_at, end_at TIMESTAMPTZ NOT NULL
        busy             BOOLEAN NOT NULL DEFAULT true
        status           VARCHAR(16) NOT NULL DEFAULT 'confirmed'
        google_event_id  VARCHAR(255) NULL, synced_at TIMESTAMPTZ NULL  -- pushed to Google
        created_at, updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    + ix_calendar_events_tenant_id, ix_calendar_events_agenda_window
      (tenant_id, professional_id, start_at)

No data is touched: every existing clinic with Google keeps reading and writing Google;
the table only fills for clinics without it. No extension and no exclusion constraint
(staff may overlap on purpose); patient writes are made exclusive under a per-agenda
lock in code (`pg_advisory_xact_lock`, available to any role).

DEPLOY ORDER - the database moves FIRST (the new code reads the table on every hub
agenda load and on every booking of a clinic without Google):

    1. `alembic upgrade head` from the NEW image (one-off), both services still old -
       the old code never reads the table, so this step is invisible to it;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity):
       the worker runs the bookings and the connect-time sync job, the API the hub;
    3. then the Brain-Message front.

Rollback: OLD code on both services first, then `alembic downgrade` (drops the table and
with it every booking made on the secretarIA agenda - export first if a clinic used it).

Revision ID: b7e2c4f9a1d6
Revises: a4c7e2f9b1d3
Create Date: 2026-10-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e2c4f9a1d6"
down_revision: str | None = "a4c7e2f9b1d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "calendar_events",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("professional_id", sa.Uuid(), nullable=True),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("busy", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="confirmed", nullable=False),
        sa.Column("google_event_id", sa.String(length=255), nullable=True),
        sa.Column("synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_calendar_events_tenant_id", "calendar_events", ["tenant_id"])
    op.create_index(
        "ix_calendar_events_agenda_window",
        "calendar_events",
        ["tenant_id", "professional_id", "start_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_calendar_events_agenda_window", table_name="calendar_events")
    op.drop_index("ix_calendar_events_tenant_id", table_name="calendar_events")
    op.drop_table("calendar_events")
