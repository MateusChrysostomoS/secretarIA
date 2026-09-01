"""tenants: drop greeting_message — the first-contact greeting is product copy now

Finishes what c1f4a8b6d2e9 started. That revision introduced the fixed greeting
frame (services/greeting_template.py) and left this column in place but
ORPHANED; nothing has read or written it since, and the hub PUT stopped
accepting it. This removes it.

WHAT WENT WITH IT
-----------------
  * `Tenant.greeting_message` (models/tenant.py)
  * `TenantRuntimeConfig.greeting_message` — written by
    services/tenant_config.py and ai/graph.py, read by NOBODY. Verified by grep
    before deleting, not assumed.
  * the reactivation fallback in workers/tasks.py::_reactivation_offer, which
    reused the clinic's "welcome pitch" for an opted-in tenant that never wrote
    a `returning_greeting_message`. That FEATURE is kept — its source simply
    follows the pitch to where the pitch now lives, `clinic_description`. It is
    also strictly lighter there: that slot is capped near 180 chars, where this
    column could hold a full 1024-char greeting.
  * `scripts/apply_config.py`'s settable-field allowlist, and the one seed
    config that set it (scripts/configs/clinica-psi-infantil.json), whose
    416-char greeting was reduced to the ~80-char pitch that is all the frame
    has room for.

DEPLOY ORDER — THE INVERSE OF THE TWO REVISIONS BELOW IT, AND IT MATTERS
------------------------------------------------------------------------
Adding a nullable column is safe to run FIRST (a column nobody reads is inert).
Dropping one is the opposite: **the code moves first, the DROP last.**

SQLAlchemy does not `SELECT *` — it emits an explicit column list built from the
mapped class. So a process still running the pre-drop model does not merely lose
this field: EVERY read of `tenants` raises `column tenants.greeting_message does
not exist`. The blast radius is the whole model, not the greeting.

secretarIA runs `secretaria_api` and `secretaria-worker` as two independently
deployed EasyPanel services with no auto-deploy, and they HAVE diverged in
production before (2026-08-16). Both map `Tenant`. So:

  1. Deploy the code (without the attribute) to BOTH services.
  2. PROVE both are on it — compare `source_fingerprint` on each service's
     `/build`. "I pushed the commit" is not evidence.
  3. Only then run this migration.

ROLLBACK IS NOT SYMMETRIC. Before the DROP runs, reverting the code is a
complete rollback — the column is still there, merely unread. AFTER it, a code
rollback reintroduces reads of a column that no longer exists and takes out every
tenant read. The honest rollback after this point is FORWARD, to code that does
not need the column.

`downgrade()` recreates the column EMPTY. The greetings it held are not
recoverable from this side.

Revision ID: e3b7c1d5a9f2
Revises: d2a5b9c7e3f1
Create Date: 2026-08-31 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3b7c1d5a9f2"
down_revision: str | None = "d2a5b9c7e3f1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("tenants", "greeting_message")


def downgrade() -> None:
    # Schema-reversible, NOT data-reversible: the column comes back empty and
    # the clinic-authored greetings it held are gone. Kept nullable so the
    # recreated column is immediately valid for every existing row.
    op.add_column("tenants", sa.Column("greeting_message", sa.Text(), nullable=True))
