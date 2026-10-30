"""Merge TASK-071 Plan 3 (deposit fallback) with TASK-072 (`patients.lgpd_accepted`); no schema
or data operation.

`a7c3e9f1d5b8` (`appointments.deposit_waived_reason`, after `d4f7a1c9e2b6` charge health on the
payment account) and `d4f8a2c6e1b9` (`patients.lgpd_accepted`, shipped on main) touch different
tables. Both parents are kept unchanged so a database on either history upgrades to this single
head. Deploy order stays: migration -> `secretaria_api` + `secretaria-worker` together ->
Brain-Message front.

Rollback: restore the database backup. `alembic downgrade` of a merge only reaches a branch tip.
"""

revision = "b9e4d2a6c8f1"
down_revision = ("a7c3e9f1d5b8", "d4f8a2c6e1b9")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
