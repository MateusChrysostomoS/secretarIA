"""professional_service_units: at which units a professional does each service.

TASK-083 follow-up (service-per-unit). One row = (professional, service, unit). The rule:

    no row for (professional, service)  -> the service is done at EVERY unit the doctor attends
    one or more rows                    -> the service is done ONLY at those units

so every existing clinic keeps today's behaviour with no backfill. The empty set is never stored.

    CREATE TABLE professional_service_units (
        professional_id UUID  FK -> professionals.id ON DELETE CASCADE,
        service_id      UUID  FK -> services.id      ON DELETE CASCADE,
        unit_id         UUID  FK -> units.id         ON DELETE CASCADE,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (professional_id, service_id, unit_id)
    )
    CREATE INDEX ix_professional_service_units_service_id ON (service_id)

ADDITIVE: a new table nothing reads until the new code ships, so the database can move first
(`alembic upgrade head` from the NEW image, both services still on the old code) and then
`secretaria_api` AND `secretaria-worker` are deployed together.

Rollback: the OLD code on both services first, then `alembic downgrade` to b5d9e3f7a1c4. Only the
per-unit service marks are lost (every service goes back to "every unit").

Revision ID: c6e1a9f4d2b7
Revises: b5d9e3f7a1c4
Create Date: 2026-11-14
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c6e1a9f4d2b7"
down_revision: str | None = "b5d9e3f7a1c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "professional_service_units",
        sa.Column("professional_id", sa.Uuid(), nullable=False),
        sa.Column("service_id", sa.Uuid(), nullable=False),
        sa.Column("unit_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["professional_id"], ["professionals.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["service_id"], ["services.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["unit_id"], ["units.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("professional_id", "service_id", "unit_id"),
    )
    op.create_index(
        "ix_professional_service_units_service_id", "professional_service_units", ["service_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_professional_service_units_service_id", table_name="professional_service_units"
    )
    op.drop_table("professional_service_units")
