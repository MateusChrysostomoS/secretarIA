"""widen consent_events.wa_id to match patients.external_id

`ConsentEvent.wa_id` (models/consent_event.py) predates channel identity
(docs/CHECKPOINT_patient_channel_identity.md): it was sized `String(32)` back
when the only subject identifier in this repo was a WhatsApp phone number.
Despite the legacy column name, the brain-message pipeline
(docs/CHECKPOINT_brain_message_pipeline.md, workers/tasks.py's LGPD-consent
branch) already writes `patient_ref` into it for a `channel="brain_message"`
patient — i.e. `Patient.external_id`, not a real `wa_id`.

`Patient.external_id` is `String(64)` (migration c7e1a4b9d0f3). Nothing today
mints an `external_id` anywhere near that width, so no row has failed yet —
but `PROMPT_BRAIN_MESSAGE_OTP_SWITCHBOARD.md` (not yet built) plans to mint a
UUID (36 chars, with hyphens) as the patient identifier for the OTP-access
channel. A 36-char value into a `String(32)` column raises a Postgres
truncation error on INSERT, which would break `POST /internal/brain-message/
inbound`'s consent-recording branch for every OTP-access patient's very
first message. Additive, symmetric with `Patient.external_id`'s width so the
same value always fits regardless of which table it lands in.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "aeeeb64360f5"
down_revision: str | None = "c7e1a4b9d0f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "consent_events",
        "wa_id",
        existing_type=sa.String(length=32),
        type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "consent_events",
        "wa_id",
        existing_type=sa.String(length=64),
        type_=sa.String(length=32),
        existing_nullable=False,
    )
