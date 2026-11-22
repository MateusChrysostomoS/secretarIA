"""Structured address parts on units (TASK-090 M1).

Additive only: 7 nullable columns on `units` (cep, street, number, complement, neighborhood,
city, state). `units.address` stays and is now written from the parts by the API; legacy rows
keep their free text and have all parts NULL.

DEPLOY ORDER - the database moves FIRST (migração antes da API e do worker): `alembic upgrade`
from the new image, then secretarIA API and worker together (old code never reads the columns).

Rollback: old code on both services first, then `alembic downgrade` (drops the columns).

Revision ID: e1a7c3b9d5f2
Revises: f9a1d7c4e2b6
Create Date: 2026-11-22
"""

import sqlalchemy as sa
from alembic import op

revision: str = "e1a7c3b9d5f2"
down_revision: str = "f9a1d7c4e2b6"
branch_labels: str | None = None
depends_on: str | None = None

_COLUMNS: tuple[tuple[str, int], ...] = (
    ("cep", 8),
    ("street", 255),
    ("number", 30),
    ("complement", 255),
    ("neighborhood", 255),
    ("city", 255),
    ("state", 2),
)


def upgrade() -> None:
    for name, length in _COLUMNS:
        op.add_column("units", sa.Column(name, sa.String(length), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("units") as batch:
        for name, _ in reversed(_COLUMNS):
            batch.drop_column(name)
