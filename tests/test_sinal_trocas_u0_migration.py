"""TASK-085 R / U0 - migration d5a8c3e1f7b9: where the OLD slot's event of a suspended change is.

Same layers as `tests/test_sinal_trocas_s0_migration.py`, cheapest first: the chain stays a
single head on top of c9e4a2f7b3d1; the offline SQL adds exactly two nullable columns to
`deposit_adjustments` and the downgrade drops exactly those; the model declares the same; and
(opt-in, a throwaway Postgres in `PAYMENTS_TEST_PG_URL`) up/down/up leaves no model drift.
"""

import asyncio
import contextlib
import io
import os
import pathlib
import re
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import create_async_engine

from secretaria.config import get_settings
from secretaria.core.database import Base

REPO = pathlib.Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "c9e4a2f7b3d1"
REVISION = "d5a8c3e1f7b9"


def _config() -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    return cfg


def _offline(direction: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        if direction == "up":
            command.upgrade(_config(), f"{PREVIOUS_HEAD}:{REVISION}", sql=True)
        else:
            command.downgrade(_config(), f"{REVISION}:{PREVIOUS_HEAD}", sql=True)
    return out.getvalue()


def test_the_revision_chains_after_the_sinal_trocas_state_and_is_the_single_head():
    script = ScriptDirectory.from_config(_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    heads = script.get_heads()
    assert REVISION in {r.revision for r in script.walk_revisions(head=heads[0])}


def test_upgrade_sql_adds_two_nullable_columns_and_nothing_else():
    sql = _offline("up")
    upper = sql.upper()
    assert "DROP" not in upper and "CREATE TABLE" not in upper
    assert "ALTER TABLE deposit_adjustments ADD COLUMN old_slot VARCHAR(16)" in sql
    assert "ALTER TABLE deposit_adjustments ADD COLUMN old_slot_at TIMESTAMP WITH TIME ZONE" in sql
    added = re.findall(r"ADD COLUMN (\w+)", sql)
    assert sorted(added) == ["old_slot", "old_slot_at"]
    # Nothing new is NOT NULL: the old code (API and worker) keeps working on the new schema.
    assert "NOT NULL" not in upper


def test_downgrade_sql_drops_exactly_the_two_columns():
    sql = _offline("down")
    dropped = re.findall(r"DROP COLUMN (\w+)", sql)
    assert sorted(dropped) == ["old_slot", "old_slot_at"]
    assert "DROP TABLE" not in sql.upper()


def test_the_model_declares_what_the_migration_adds():
    adjustments = Base.metadata.tables["deposit_adjustments"]
    assert adjustments.c.old_slot.nullable is True
    assert adjustments.c.old_slot.type.length == 16
    assert adjustments.c.old_slot_at.nullable is True
    assert adjustments.c.old_slot_at.type.timezone is True


PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to run on Postgres")


@needs_pg
def test_postgres_up_down_up_leaves_no_model_drift(monkeypatch):
    """Creates (and drops) its own scratch database next to `PAYMENTS_TEST_PG_URL`'s."""
    admin_url = PG_URL
    base, _, _ = admin_url.rpartition("/")
    name = f"t085u0_{uuid.uuid4().hex[:10]}"

    async def _admin(statement: str):
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.exec_driver_sql(statement)
        await engine.dispose()

    asyncio.run(_admin(f'CREATE DATABASE "{name}"'))
    url = f"{base}/{name}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()

    def _drift() -> list:
        async def _run():
            engine = create_async_engine(url)
            async with engine.connect() as conn:

                def _diff(sync_conn):
                    ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
                    return compare_metadata(ctx, Base.metadata)

                diffs = await conn.run_sync(_diff)
            await engine.dispose()
            return diffs

        def _flatten(items):
            for item in items:
                if isinstance(item, list | tuple) and item and isinstance(item[0], list | tuple):
                    yield from _flatten(item)
                else:
                    yield item

        mine = re.compile(r"deposit_adjustments.*old_slot")
        return [d for d in _flatten(asyncio.run(_run())) if mine.search(str(d))]

    async def _columns() -> set[str]:
        engine = create_async_engine(url)
        async with engine.connect() as conn:
            rows = await conn.exec_driver_sql(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'deposit_adjustments'"
            )
            found = {row[0] for row in rows}
        await engine.dispose()
        return found

    cfg = _config()
    try:
        command.upgrade(cfg, "head")
        assert {"old_slot", "old_slot_at"} <= asyncio.run(_columns())
        assert _drift() == []
        command.downgrade(cfg, PREVIOUS_HEAD)
        assert not {"old_slot", "old_slot_at"} & asyncio.run(_columns())
        command.upgrade(cfg, "head")
        assert _drift() == []
    finally:
        get_settings.cache_clear()
        asyncio.run(_admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
