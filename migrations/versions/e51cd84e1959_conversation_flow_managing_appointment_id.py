"""conversations: add flow_managing_appointment_id (manage sub-flow target)

Backs the manage-flow rework in services/flow_router.py. Cancel/reschedule
used to smuggle the appointment being acted on through the SERVICE_CATALOG
`flow_selected_type` column (a string meant for the appointment-type name) -
an overload that made the two flows impossible to tell apart from the
conversation row alone. This column replaces it with a proper typed FK:

  - `conversations.flow_managing_appointment_id` - the appointment currently
    being cancelled/rescheduled inside the MANAGE_BOOKING sub-flow. FK ->
    appointments.id with ON DELETE SET NULL so deleting the appointment row
    never breaks an ongoing conversation row (same pattern as
    `flow_selected_professional_id` -> professionals.id, migration
    b3d9f2a1c8e7). The manage flow no longer writes `flow_selected_type` at
    all, so it always reads back None on conversations mid-MANAGE_BOOKING.

Additive + nullable; existing rows need no backfill.

Revision ID: e51cd84e1959
Revises: 32e87fcf926b
Create Date: 2026-07-21 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e51cd84e1959"
down_revision: str | None = "32e87fcf926b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "flow_managing_appointment_id",
            sa.Uuid(),
            sa.ForeignKey("appointments.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    # Dropping the column also drops its unnamed FK constraint on Postgres.
    op.drop_column("conversations", "flow_managing_appointment_id")
