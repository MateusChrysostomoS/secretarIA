"""Multi-doctor deterministic flow: conversation selection + appointment insurance.

Backs the deterministic doctor-selection branch in services/flow_router.py
(docs/CHECKPOINT_multi_doctor_flow.md):

  - `conversations.flow_selected_professional_id` — which professional the
    patient picked inside the SERVICE_CATALOG flow, parallel to the existing
    `flow_selected_type/day/slot` columns. FK -> professionals.id with
    ON DELETE SET NULL so removing a professional never breaks (or cascades
    into) an ongoing conversation row.
  - `conversations.flow_selected_insurance` — the convênio label the patient
    picked (or typed) at the STEP_AWAITING_INSURANCE step, held here between
    that step and the final booking (same lifecycle as the other
    flow_selected_* columns). Free text, never a FK: `tenants.insurances` is
    a JSON list of plan names, there is no Insurance table.
  - `appointments.insurance` — the same label recorded onto the booked
    appointment. Informational only, by design: convênio is a clinic-wide
    fact and never filters professionals or slots.

All additive + nullable; existing rows need no backfill.

Revision ID: b3d9f2a1c8e7
Revises: f06e85b476f2
Create Date: 2026-07-19 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3d9f2a1c8e7"
down_revision: str | None = "f06e85b476f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "flow_selected_professional_id",
            sa.Uuid(),
            sa.ForeignKey("professionals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.add_column(
        "conversations",
        sa.Column("flow_selected_insurance", sa.String(length=120), nullable=True),
    )
    op.add_column(
        "appointments",
        sa.Column("insurance", sa.String(length=120), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("appointments", "insurance")
    op.drop_column("conversations", "flow_selected_insurance")
    # Dropping the column also drops its unnamed FK constraint on Postgres.
    op.drop_column("conversations", "flow_selected_professional_id")
