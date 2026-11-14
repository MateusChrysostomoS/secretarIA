"""tenants.operating_channel (TASK-084): where the secretarIA serves this clinic's patients.

Additive, NOT NULL with a server default, so every existing clinic reads the Portal
(`brain_message`) after the upgrade; a CHECK keeps the column to the two channels the
product knows. WhatsApp is a VALUE here, not a released option - that gate lives in
services/operating_channel.py.
"""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.core.database import Base
from secretaria.models import Tenant
from secretaria.models.tenant import (
    OPERATING_CHANNEL_PORTAL,
    OPERATING_CHANNEL_WHATSAPP,
    OPERATING_CHANNELS,
)
from secretaria.services import channel_sender

ROOT = Path(__file__).resolve().parents[1]
REVISION = "b8d4f2a6c9e1"
DOWN_REVISION = "d2f6b8a4c1e7"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_tenant_operating_channel.py"
    spec = importlib.util.spec_from_file_location("tenant_operating_channel_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_insurance_order():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in scripts.walk_revisions(base="base", head=heads[0])}
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_backfills_brain_message_and_downgrade_drops_it(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'tenants.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tenants (id CHAR(32) PRIMARY KEY, clinic_name TEXT)"))
        conn.execute(sa.text("INSERT INTO tenants (id, clinic_name) VALUES ('a', 'Clínica A')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("tenants")}
            assert columns["operating_channel"]["nullable"] is False
            stored = conn.execute(sa.text("SELECT operating_channel FROM tenants")).scalar()
            assert stored == "brain_message"
            with pytest.raises(IntegrityError):
                conn.execute(
                    sa.text(
                        "INSERT INTO tenants (id, clinic_name, operating_channel) "
                        "VALUES ('b', 'Clínica B', 'sms')"
                    )
                )
            migration.downgrade()
            columns = {c["name"] for c in sa.inspect(conn).get_columns("tenants")}
            assert "operating_channel" not in columns


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def test_new_tenant_defaults_to_the_portal(db):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica Nova")
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        assert tenant.operating_channel == "brain_message"


async def test_the_check_refuses_another_channel(db):
    async with db() as session:
        session.add(Tenant(id=uuid4(), clinic_name="Clínica X", operating_channel="sms"))
        with pytest.raises(IntegrityError):
            await session.commit()


def test_values_are_the_patient_channel_vocabulary():
    assert OPERATING_CHANNEL_PORTAL == channel_sender.CHANNEL_BRAIN_MESSAGE == "brain_message"
    assert OPERATING_CHANNEL_WHATSAPP == channel_sender.CHANNEL_WHATSAPP == "whatsapp"
    assert OPERATING_CHANNELS == ("brain_message", "whatsapp")
    assert Tenant.__table__.c.operating_channel.nullable is False
