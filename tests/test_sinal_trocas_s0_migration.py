"""TASK-085 R S0 - migration c9e4a2f7b3d1: the state of a change on a sinal.

Same layers as `tests/test_deposit_before_migration.py`, cheapest first: the chain stays a
single head on top of b7d3f1a8c5e2; the offline SQL is additive (one nullable column per table,
one NOT NULL relaxed) and the downgrade undoes exactly that; the models declare the same; and
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
PREVIOUS_HEAD = "b7d3f1a8c5e2"
REVISION = "c9e4a2f7b3d1"


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


def test_the_revision_chains_after_the_unit_terms_and_is_the_single_head():
    script = ScriptDirectory.from_config(_config())
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD


def test_upgrade_sql_is_additive():
    sql = _offline("up")
    upper = sql.upper()
    assert "DROP TABLE" not in upper and "DROP COLUMN" not in upper
    assert "ALTER TABLE deposit_adjustments ADD COLUMN mode VARCHAR(16)" in sql
    assert "ALTER TABLE booking_holds ALTER COLUMN conversation_id DROP NOT NULL" in sql
    assert "ALTER TABLE booking_holds ADD COLUMN adjustment_id UUID" in sql
    assert re.search(
        r"FOREIGN KEY\s*\(adjustment_id\) REFERENCES deposit_adjustments \(id\) ON DELETE CASCADE",
        sql,
    )
    assert "CREATE INDEX ix_booking_holds_adjustment_id ON booking_holds (adjustment_id)" in sql
    # Nothing new is NOT NULL: the old code (API and worker) keeps working on the new schema.
    assert "SET NOT NULL" not in upper


def test_downgrade_sql_undoes_exactly_this_revision():
    sql = _offline("down")
    # Holds without a conversation (the difference holds) cannot exist under the old NOT NULL.
    assert "DELETE FROM booking_holds WHERE conversation_id IS NULL" in sql
    dropped = re.findall(r"DROP COLUMN (\w+)", sql)
    assert sorted(dropped) == ["adjustment_id", "mode"]
    assert "ALTER TABLE booking_holds ALTER COLUMN conversation_id SET NOT NULL" in sql
    assert "DROP INDEX ix_booking_holds_adjustment_id" in sql
    assert "DROP TABLE" not in sql.upper()


def test_the_models_declare_what_the_migration_adds():
    adjustments = Base.metadata.tables["deposit_adjustments"]
    assert adjustments.c.mode.nullable is True and adjustments.c.mode.type.length == 16
    holds = Base.metadata.tables["booking_holds"]
    assert holds.c.conversation_id.nullable is True
    assert holds.c.adjustment_id.nullable is True
    (fk,) = holds.c.adjustment_id.foreign_keys
    assert fk.column.table.name == "deposit_adjustments" and fk.ondelete == "CASCADE"
    assert any(
        index.name == "ix_booking_holds_adjustment_id" for index in holds.indexes
    )


PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to run on Postgres")


@needs_pg
def test_postgres_up_down_up_leaves_no_model_drift(monkeypatch):
    admin_url = PG_URL
    base, _, _ = admin_url.rpartition("/")
    name = f"t085r_{uuid.uuid4().hex[:10]}"

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
                if isinstance(item, list | tuple) and item and isinstance(item[0], list | tuple):
                    yield from _flatten(item)
                else:
                    yield item

        mine = re.compile(
            r"deposit_adjustments.*mode|booking_holds.*(conversation_id|adjustment_id)"
        )
        return [d for d in _flatten(asyncio.run(_run())) if mine.search(str(d))]

    async def _seed_a_difference_hold():
        """A hold without a conversation, so the downgrade has to clear it to restore NOT NULL."""
        from datetime import UTC, datetime, timedelta

        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

        from secretaria.models import BookingHold, Tenant

        engine = create_async_engine(url)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        now = datetime.now(UTC)
        async with maker() as session:
            tenant = Tenant(id=uuid.uuid4(), clinic_name="Clinic", phone_number_id="t085r-pg")
            session.add(tenant)
            await session.flush()
            session.add(
                BookingHold(
                    tenant_id=tenant.id,
                    conversation_id=None,
                    start_at=now,
                    end_at=now + timedelta(minutes=30),
                    expires_at=now + timedelta(hours=1),
                    phase="payment",
                )
            )
            await session.commit()
        await engine.dispose()

    cfg = _config()
    try:
        command.upgrade(cfg, "head")
        assert _drift() == []
        asyncio.run(_seed_a_difference_hold())
        command.downgrade(cfg, PREVIOUS_HEAD)
        command.upgrade(cfg, "head")
        assert _drift() == []
    finally:
        get_settings.cache_clear()
