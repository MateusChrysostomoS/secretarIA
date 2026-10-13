"""Migration e2b7c4a9d1f5 - the `before` timing columns (TASK-017, spec sections 6.1 and 10A.4).

Same layers as `tests/test_payment_migration.py`, cheapest first: the offline SQL is additive
and matches the models; the chain stays a single head; and (opt-in, a throwaway Postgres) the
upgrade/downgrade round trip leaves no model drift.
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
PREVIOUS_HEAD = "c8a5d3f1b7e2"
REVISION = "e2b7c4a9d1f5"
NEW_PIX_COLUMNS = ("conversation_id", "expired_notice_sent_at")


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


def test_the_revision_chains_after_the_provider_core_and_is_the_single_head():
    script = ScriptDirectory.from_config(_config())
    # TASK-052 chains `b6d2e8f4a1c3` after this one: the newest migration pins the head.
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD


def test_upgrade_sql_is_purely_additive():
    sql = _offline("up")
    assert "DROP " not in sql.upper()
    assert "NOT NULL" not in sql.upper()  # every new column is nullable: old code ignores it
    for column in NEW_PIX_COLUMNS:
        assert f"ALTER TABLE pix_deposits ADD COLUMN {column} " in sql
    assert "ALTER TABLE payment_attention_items ADD COLUMN alerted_at " in sql
    assert "ON DELETE SET NULL" in sql


def test_the_models_declare_exactly_what_the_migration_adds():
    pix = Base.metadata.tables["pix_deposits"]
    for column in NEW_PIX_COLUMNS:
        assert pix.c[column].nullable is True
    fk = next(iter(pix.c.conversation_id.foreign_keys))
    assert fk.column.table.name == "conversations" and fk.ondelete == "SET NULL"
    assert Base.metadata.tables["payment_attention_items"].c.alerted_at.nullable is True


def test_downgrade_sql_drops_only_what_this_revision_added():
    sql = _offline("down")
    dropped = re.findall(r"DROP COLUMN (\w+)", sql)
    assert sorted(dropped) == sorted([*NEW_PIX_COLUMNS, "alerted_at"])
    assert "DROP TABLE" not in sql.upper()


PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to run on Postgres")


@needs_pg
def test_postgres_round_trip_leaves_no_model_drift(monkeypatch):
    admin_url = PG_URL
    base, _, _ = admin_url.rpartition("/")
    name = f"t017_{uuid.uuid4().hex[:10]}"

    async def _create():
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        await engine.dispose()

    asyncio.run(_create())
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
                if isinstance(item, list):
                    yield from _flatten(item)
                else:
                    yield item

        mine = re.compile("|".join([*NEW_PIX_COLUMNS, "alerted_at"]))
        return [d for d in _flatten(asyncio.run(_run())) if mine.search(str(d))]

    cfg = _config()
    command.upgrade(cfg, "head")
    assert _drift() == []
    command.downgrade(cfg, PREVIOUS_HEAD)
    command.upgrade(cfg, "head")
    assert _drift() == []
