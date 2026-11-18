"""Merge TASK-085 (unit prices/durations and the sinal re-quote: `d5a8c3e1f7b9`, the old-slot
state) with TASK-086 (convenio acceptance matrix: `e5a9c3b7d1f2`, the 3rd insurance mode
retired); no schema or data operation.

The two chains touch different tables (`professional_service_unit_terms` and the sinal
re-quote columns vs. `insurance_acceptance_exceptions` and the `insurance_mode` data move).
Both parents are kept unchanged so a database on either history upgrades to this single head.
Deploy order stays: migration -> `secretaria_api` + `secretaria-worker` together -> fronts.

Rollback: restore the database backup. `alembic downgrade` of a merge only reaches a branch tip.
"""

revision = "f1c4b8d2a6e3"
down_revision = ("d5a8c3e1f7b9", "e5a9c3b7d1f2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
