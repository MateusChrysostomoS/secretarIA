"""Merge context sources (TASK-026) into the Pix + native agenda history (TASK-067); no schema
or data operation.

`a1c4e7b9d2f3` (context_sources) and the Pix chain both started from `a4c7e2f9b1d3`; the feature
later joined Pix and the native agenda in `d6f0a8c2e4b7`. Both parents are kept unchanged so a
database on either history upgrades to this single head. Deploy order stays: migration ->
`secretaria_api` + `secretaria-worker` together (new arq job) -> Brain-Message front.

Rollback of the sources table ONLY (old code on both services first), proven on a local Postgres:
    alembic downgrade d6f0a8c2e4b7          # undo this merge
    alembic downgrade a1c4e7b9d2f3@-1       # drop context_sources; Pix + agenda stay
NEVER `alembic downgrade a1c4e7b9d2f3-1`: it walks to the common ancestor a4c7e2f9b1d3 and also
drops the Pix and native agenda tables.
"""

revision = "f3b9d1c7a5e2"
down_revision = ("d6f0a8c2e4b7", "a1c4e7b9d2f3")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
