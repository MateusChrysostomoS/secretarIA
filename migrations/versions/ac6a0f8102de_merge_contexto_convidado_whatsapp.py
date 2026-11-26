"""Merge TASK-090 (Contexto: unit address, convênio payment rule / "Outro" ON-OFF / order),
TASK-091 (`professionals.requires_self_setup`) and TASK-093 Plano 3
(`whatsapp_connection_attempts`); no schema or data operation.

The three parents all start from `f9a1d7c4e2b6` and touch different tables:
`c7e3a9f1d5b8` (end of the TASK-090 chain `e1a7c3b9d5f2` -> `f2b8d4c6e0a3` -> `a3c9e5d7f1b4` ->
`c7e3a9f1d5b8`), `a3c7e1f9b5d2` (TASK-091) and `e7b3d1f9a5c2` (TASK-093 Plano 3). They are kept
unchanged so a database on any of those histories upgrades to this single head. Deploy order:
`alembic upgrade head` BY HAND, before `secretaria_api` + `secretaria-worker` (together).

Rollback: restore the database backup. `alembic downgrade` of a merge only reaches a branch tip.
"""

revision = "ac6a0f8102de"
down_revision = ("c7e3a9f1d5b8", "a3c7e1f9b5d2", "e7b3d1f9a5c2")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
