"""tenants: clinic_description — the ONE clinic-authored slot in the greeting frame

The first-contact greeting stopped being clinic free text this round: it is now
a fixed product frame (services/greeting_template.py::GREETING_FRAME) carrying
the automated-assistant disclosure, the "no medical advice here" line, the
button guidance and the emergency escape — obligations a clinic that never
filled `greeting_message` (the column defaults to NULL, so most of them) was
shipping none of.

This column is the only part the clinic still writes: what it offers, its
values, a differentiator. It is deliberately a NEW column rather than a
reinterpretation of `greeting_message`, which is left in place but ORPHANED
(the same treatment `greeting_buttons` got in d5e9f3a2b7c8). Reusing it would
drop each clinic's existing WHOLE greeting — "Olá! Sou a secretária do Dr. X…"
— into the description slot of a frame that already opens with exactly that,
producing the duplicated, ambiguous opener this round exists to remove. No
backfill, for the same reason: an empty slot renders cleanly (render_greeting
collapses it), a wrong one does not.

DEPLOY ORDER — safe first, unlike the drop in b4c2e8f1a9d3 that precedes it.
Adding a NULLABLE column with no default is backward compatible: a process
still running the pre-column model simply never selects it. secretarIA runs the
API and the worker as two independently-deployed EasyPanel services, so run
this migration FIRST, then deploy either service in any order.

Revision ID: c1f4a8b6d2e9
Revises: b4c2e8f1a9d3
Create Date: 2026-08-31 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1f4a8b6d2e9"
down_revision: str | None = "b4c2e8f1a9d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tenants", sa.Column("clinic_description", sa.Text(), nullable=True))


def downgrade() -> None:
    # Genuinely reversible, unlike the revision below it: nothing else reads
    # this column, and the greeting degrades to the frame with an empty slot.
    # The clinic's typed description is lost, though — it lives nowhere else.
    op.drop_column("tenants", "clinic_description")
