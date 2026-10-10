"""R9: the clinic's extra reminder as "N days before, at HH:MM" - two additive columns.

TASK-048 R9 (docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md §6.2):

    ADD tenants.reminder_extra_days_before  INTEGER NULL     (2..14, validated by the API)
    ADD tenants.reminder_extra_send_time    VARCHAR(5) NULL  ("HH:MM", clinic time zone)

Data: every clinic with a positive legacy `reminder_extra_lead_minutes` gets
days = ceil(lead / 1440) clamped to 2..14, at 09:00 - the same rule as
core/extra_reminder.py::from_legacy_lead. The legacy column is NOT dropped (widen
before, narrow after): R9 code stops reading it and mirrors days x 1440 into it.
Pending reminder rows are not recomputed here; they move at the clinic's next save of
the reminder setting.

Metadata-only adds on Postgres 11+ plus one small UPDATE on `tenants`. DEPLOY ORDER -
the database moves FIRST (the ORM names every mapped column on every read):

    1. `alembic upgrade head` from the NEW image (one-off), both services still old;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity);
    3. then the Brain-Message front.

Rollback: OLD code on both services first, then `alembic downgrade`. The legacy column
still holds days x 1440, so old code plans the extra reminder on the same day; the
chosen hour is lost.

Revision ID: a4c7e2f9b1d3
Revises: d8e3a5c1f7b2
Create Date: 2026-10-10
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a4c7e2f9b1d3"
down_revision: str | None = "d8e3a5c1f7b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("reminder_extra_days_before", sa.Integer(), nullable=True))
    op.add_column(
        "tenants", sa.Column("reminder_extra_send_time", sa.String(length=5), nullable=True)
    )
    # Integer division on both Postgres and SQLite: (lead + 1439) / 1440 = ceil(lead / 1440).
    op.execute(
        "UPDATE tenants SET "
        "reminder_extra_days_before = CASE "
        "WHEN reminder_extra_lead_minutes <= 2880 THEN 2 "
        "WHEN reminder_extra_lead_minutes >= 20160 THEN 14 "
        "ELSE (reminder_extra_lead_minutes + 1439) / 1440 END, "
        "reminder_extra_send_time = '09:00' "
        "WHERE reminder_extra_lead_minutes > 0"
    )


def downgrade() -> None:
    op.drop_column("tenants", "reminder_extra_send_time")
    op.drop_column("tenants", "reminder_extra_days_before")
