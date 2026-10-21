"""The `context_sources` table: registered on the metadata, defaults, cascade, one alembic head."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402
from sqlalchemy import event, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import ContextSource, Tenant  # noqa: E402


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_a_new_source_starts_pending_with_a_proposal_slot(db):
    tenant_id = uuid4()
    async with db() as session:
        session.add(Tenant(id=tenant_id, clinic_name="Clínica", phone_number_id=None))
        await session.flush()  # no relationship() here: the parent row must exist first
        session.add(ContextSource(tenant_id=tenant_id, kind="text", label="Texto colado"))
        await session.commit()
        row = (await session.execute(select(ContextSource))).scalar_one()
    assert row.status == "pending" and row.error_code is None
    assert row.proposal is None and row.source_text is None and row.url is None
    assert row.created_at is not None


async def test_deleting_the_clinic_deletes_its_sources(db):
    tenant_id = uuid4()
    async with db() as session:
        tenant = Tenant(id=tenant_id, clinic_name="Clínica", phone_number_id=None)
        session.add(tenant)
        await session.flush()
        session.add(ContextSource(tenant_id=tenant_id, kind="text", label="x"))
        await session.commit()
        await session.delete(tenant)
        await session.commit()
        assert (await session.execute(select(ContextSource))).first() is None


def test_extraction_has_its_own_token_budget_and_default_quotas():
    s = Settings(_env_file=None)
    # OPENAI_MAX_TOKENS (2500) also covers reasoning tokens and would cut an extraction to nothing.
    assert s.OPENAI_CONTEXT_EXTRACTION_MAX_TOKENS == 8000
    assert s.CONTEXT_SOURCES_PER_DAY == 10 and s.CONTEXT_SOURCES_IN_FLIGHT == 2


def test_the_migration_chain_has_a_single_head_and_it_is_ours():
    # TASK-067: the sources table kept its original parent (a4c7e2f9b1d3) and joins the Pix +
    # native agenda history through a merge revision; no existing revision was rewritten.
    scripts = ScriptDirectory.from_config(Config("alembic.ini"))
    heads = scripts.get_heads()
    assert len(heads) == 1
    chain = {r.revision for r in scripts.walk_revisions(head=heads[0])}
    assert {"a1c4e7b9d2f3", "d6f0a8c2e4b7"} <= chain
    assert scripts.get_revision("a1c4e7b9d2f3").down_revision == "a4c7e2f9b1d3"
    merge = scripts.get_revision("f3b9d1c7a5e2")
    assert set(merge.down_revision) == {"d6f0a8c2e4b7", "a1c4e7b9d2f3"}
