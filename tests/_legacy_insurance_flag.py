"""The retired per-convênio "não cobra sinal" flag, as a test can still plant it (TASK-085 R U1b).

The ORM no longer maps `charge_deposit` on `tenant_insurance_plans` / `professional_insurance_plans`
(models/insurance.py, LEGACY COLUMNS), but production still has both columns, `NOT NULL DEFAULT
true`, and old rows may hold `false`. A test that wants to prove such a row is IGNORED adds the
column to its SQLite schema exactly as production has it, then writes the legacy value with SQL.
"""

from uuid import UUID

from sqlalchemy import Uuid, bindparam, text

_TABLES = frozenset({"tenant_insurance_plans", "professional_insurance_plans"})


async def ensure_legacy_column(session, table: str) -> None:
    """Give `table` the production column (`NOT NULL DEFAULT true`) if the test schema lacks it."""
    assert table in _TABLES, table
    columns = (await session.execute(text(f"PRAGMA table_info({table})"))).all()
    if not any(column[1] == "charge_deposit" for column in columns):
        await session.execute(
            text(f"ALTER TABLE {table} ADD COLUMN charge_deposit BOOLEAN NOT NULL DEFAULT 1")
        )


async def set_legacy_flag(session, table: str, row_id: UUID, value: bool) -> None:
    """Write the legacy value onto one row (the row must already be flushed)."""
    await ensure_legacy_column(session, table)
    statement = text(f"UPDATE {table} SET charge_deposit = :value WHERE id = :id").bindparams(
        bindparam("id", type_=Uuid())
    )
    await session.execute(statement, {"value": value, "id": row_id})


async def legacy_flags(session, table: str) -> list[bool]:
    """Every row's legacy value, sorted (False first)."""
    await ensure_legacy_column(session, table)
    rows = (await session.execute(text(f"SELECT charge_deposit FROM {table}"))).scalars().all()
    return sorted(bool(value) for value in rows)
