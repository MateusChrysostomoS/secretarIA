"""R7: clinic actions on the agenda notify the patient - two additive columns.

TASK-032 R7 (docs/superpowers/specs/2026-10-09-acoes-clinica-avisos-paciente-design.md):

    ADD tenants.paid_notices_auto_approved     BOOLEAN NOT NULL DEFAULT false
    ADD appointments.post_consult_notified_at  TIMESTAMPTZ NULL

`paid_notices_auto_approved` is the clinic's standing "não perguntar novamente" for
billed WhatsApp notices outside the 24 h window (spec §2). `post_consult_notified_at`
marks that the post-consult message of THIS appointment was delivered after the
clinic marked "Compareceu", so it is never sent twice and the next-open follow-up
(services/patient_context.py::find_post_consult_followup) does not repeat it.

Both are metadata-only on Postgres 11+ (constant default / nullable). DEPLOY ORDER -
the database moves FIRST (the ORM names every mapped column on every read):

    1. `alembic upgrade head` from the NEW image (one-off), both services still old;
    2. deploy `secretaria_api` AND `secretaria-worker` together (`GET /build` parity).

Rollback: the OLD code on both services first, then `alembic downgrade`. Data lost:
the clinic's auto-approval choice and the post-consult markers (the follow-up then
behaves exactly as before R7).

Revision ID: d8e3a5c1f7b2
Revises: c2d5f8a1e4b6
Create Date: 2026-10-09
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d8e3a5c1f7b2"
down_revision: str | None = "c2d5f8a1e4b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tenants",
        sa.Column(
            "paid_notices_auto_approved",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "appointments",
        sa.Column("post_consult_notified_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appointments", "post_consult_notified_at")
    op.drop_column("tenants", "paid_notices_auto_approved")
