"""patients: lgpd_accepted_at — when this subject accepted the Terms/Privacy Policy

The operational half of the WhatsApp consent gate added with the greeting frame
(c1f4a8b6d2e9). The gate reads this column on EVERY inbound turn to decide
whether the patient still owes an acceptance, so it has to be a column on the
subject rather than something reconstructed from the audit trail.

WHY A TIMESTAMP AND NOT A BOOLEAN
---------------------------------
LGPD asks when consent was given, not merely whether. `is not None` is the truth
test everywhere, so the extra information costs nothing at the call sites. This
is deliberately MORE than PreCheck records: over there the same fact is a
transient `sessions.state` moving LGPD_PENDING -> ACTIVE (see the
`wf_condutor_generico_universal` n8n workflow), which cannot answer "when did
this person agree?" once the session moves on.

WHY NOT ONLY `consent_events`
-----------------------------
`ConsentEvent(kind="terms_accepted")` is still written and remains the immutable
audit record. The two have deliberately different lifetimes:
`/dangerously-remove-context` deletes the Patient row — and with it this column
— but does NOT delete consent_events. So wiping a patient's context replays a
genuine first contact, asking for consent again, while the legal record of what
they once accepted survives the wipe.

BACKFILL: none, and that is the point. Every existing patient reads as "never
accepted" and is asked once on their next message. Backfilling a timestamp would
be inventing a consent moment that never happened.

DEPLOY ORDER — safe first. A NULLABLE column with no default is backward
compatible: a process still running the pre-column model simply never selects
it. secretarIA runs the API and the worker as two independently-deployed
EasyPanel services, so run this migration FIRST, then deploy either in any
order.

Revision ID: d2a5b9c7e3f1
Revises: c1f4a8b6d2e9
Create Date: 2026-08-31 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2a5b9c7e3f1"
down_revision: str | None = "c1f4a8b6d2e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patients",
        sa.Column("lgpd_accepted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    # Reversible in schema, NOT in meaning: dropping this re-asks every patient
    # who had already accepted. The acceptances themselves are not lost —
    # consent_events still holds one row per acceptance — but nothing reads
    # those at runtime, so the gate would re-prompt until they tap again.
    op.drop_column("patients", "lgpd_accepted_at")
