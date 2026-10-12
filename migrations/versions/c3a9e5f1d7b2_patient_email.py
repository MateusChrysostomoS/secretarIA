"""patients.email: additive nullable booking contact copy.

Revision ID: c3a9e5f1d7b2
Revises: d4f8a2c6e913
"""

import sqlalchemy as sa
from alembic import op

revision = "c3a9e5f1d7b2"
down_revision = "d4f8a2c6e913"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("patients", sa.Column("email", sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column("patients", "email")
