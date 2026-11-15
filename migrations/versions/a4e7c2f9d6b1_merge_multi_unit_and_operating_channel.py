"""Merge TASK-083 (multi-units: `professional_service_units`) with TASK-084
(`tenants.operating_channel`, shipped on main); no schema or data operation.

`c6e1a9f4d2b7` (service x unit marks, after the multi-unit agenda chain `e4a8c2f6b1d9` ->
`b5d9e3f7a1c4`) and `b8d4f2a6c9e1` (the clinic's operating channel, after `d2f6b8a4c1e7`)
touch different tables. Both parents are kept unchanged so a database on either history
upgrades to this single head. Deploy order stays: migration -> `secretaria_api` +
`secretaria-worker` together -> fronts.

Rollback: restore the database backup. `alembic downgrade` of a merge only reaches a branch tip.
"""

revision = "a4e7c2f9d6b1"
down_revision = ("c6e1a9f4d2b7", "b8d4f2a6c9e1")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
