"""attendee_name: booking for someone else ("Essa consulta é pra você?")

Adds one nullable VARCHAR(120) to each of the three places the booking carries
its data on the way to an appointment - the conversation's in-progress flow
(`conversations.flow_attendee_name`), the Brain-Message slot reservation
(`booking_holds.attendee_name`) and the appointment itself
(`appointments.attendee_name`). Same path the convênio already takes.

Widen-before-narrow (skill `frozen-contract-migration`): purely additive, NULL
means "the attendee is the patient who booked" - exactly what every existing
row already means. Old API/worker code does not map the columns and is not
affected, so this migration ships BEFORE the code that reads them, and the API
and the worker can then be deployed in either order.

Revision ID: b8e3c5a1d702
Revises: a7d2f4b9c013
Create Date: 2026-09-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8e3c5a1d702"
down_revision: str | None = "a7d2f4b9c013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    ("conversations", "flow_attendee_name"),
    ("booking_holds", "attendee_name"),
    ("appointments", "attendee_name"),
)


def upgrade() -> None:
    for table, column in _COLUMNS:
        op.add_column(table, sa.Column(column, sa.String(length=120), nullable=True))


def downgrade() -> None:
    # Only safe once NO deployed service maps these columns any more (see the
    # module docstring): drop the code first, then run this.
    for table, column in reversed(_COLUMNS):
        op.drop_column(table, column)
