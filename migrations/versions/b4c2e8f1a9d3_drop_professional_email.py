"""professionals: drop email — brain-api owns a professional's address

Reverts f3a9c1d7b2e4, which added a SECOND copy of a value brain-api already
owns and that this service already reads. brain-api is the single writer of
identity: a professional's address lives on its `users.email` (linked by
`users.professional_id`) and is read per use through
services/brain_professionals.py::fetch_professional_emails — the client that
plugins/professional_notification.py has used since FEAT 33, and that
workers/tasks.py::_handle_professional_config_incomplete now uses too.

The column could not be kept honest: it had no propagation path (a doctor
changing their address in brain-api would leave this copy mailing the old one
forever) and no backfill, so it was NULL for every pre-existing clinic — the
config-gap alert reached the clinic's `tenants.contact_email` and never the
doctor, even though brain-api knew the address the whole time.

DEPLOY ORDER — the inverse of the migration it reverts, and it matters. Adding
a nullable column is safe to run first; DROPPING one is not. Every service that
maps `Professional` emits an explicit column list, so any process still running
the pre-drop model raises `column professionals.email does not exist` on EVERY
professional read — not just the alert path. secretarIA runs the API and the
worker as two independently-deployed EasyPanel services, so BOTH must be live
on the code without the attribute before this runs. Confirm with the
`source_fingerprint` on each service's /build, then upgrade.

Irreversible in practice, and `downgrade` says so rather than pretending: the
column comes back empty, and the addresses it once held are not recoverable
from this side. The real rollback is code — go back to reading brain-api.

Revision ID: b4c2e8f1a9d3
Revises: f3a9c1d7b2e4
Create Date: 2026-08-29 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4c2e8f1a9d3"
down_revision: str | None = "f3a9c1d7b2e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("professionals", "email")


def downgrade() -> None:
    # Structural only: the addresses are gone, and nothing on this side can
    # refill them. Present so the chain stays walkable, not as a data recovery.
    op.add_column(
        "professionals",
        sa.Column("email", sa.String(length=254), nullable=True),
    )
