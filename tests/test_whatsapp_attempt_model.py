"""TASK-093 Plano 3, Task 1: the WhatsApp connection attempts table + its settings.

The secretarIA backend learns to connect a clinic's WhatsApp itself (stage a: built behind
`WHATSAPP_CONNECT_BACKEND_ENABLED`, off by default). Every attempt is one row keyed by the
client-supplied `attempt_id` (unique per clinic) so a replay never exchanges the Meta code
twice. The table holds NO secret: never the Meta `code`, never the access token (that one
stays Fernet ciphertext in `tenant_credentials`).
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

from secretaria.config import Settings
from secretaria.core.database import Base
from secretaria.models import Tenant, WhatsAppConnectionAttempt
from secretaria.models.whatsapp_connection_attempt import (
    ATTEMPT_CONNECTED,
    ATTEMPT_FAILED,
    ATTEMPT_PENDING,
    ATTEMPT_STATUSES,
)

ROOT = Path(__file__).resolve().parents[1]
REVISION = "e7b3d1f9a5c2"
DOWN_REVISION = "f9a1d7c4e2b6"
TABLE = "whatsapp_connection_attempts"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_whatsapp_connection_attempts.py"
    spec = importlib.util.spec_from_file_location("whatsapp_connection_attempts_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- settings ------------------------------------------------------------------------------


def test_new_settings_are_optional_and_the_backend_flag_is_off_by_default():
    settings = Settings(_env_file=None)
    assert settings.WHATSAPP_CONNECT_BACKEND_ENABLED is False
    assert settings.META_APP_ID == ""
    assert settings.META_ES_CONFIG_ID == ""
    # Already existed (webhook HMAC): the same Meta app's secret also exchanges the code.
    assert "META_APP_SECRET" in Settings.model_fields
    assert settings.META_GRAPH_API_VERSION


# --- model ---------------------------------------------------------------------------------


def test_statuses_are_the_three_the_plan_names():
    assert (ATTEMPT_PENDING, ATTEMPT_CONNECTED, ATTEMPT_FAILED) == (
        "pending",
        "connected",
        "failed",
    )
    assert ATTEMPT_STATUSES == ("pending", "connected", "failed")


def test_the_table_has_the_contract_columns_and_no_secret():
    columns = set(WhatsAppConnectionAttempt.__table__.c.keys())
    assert {
        "attempt_id",
        "tenant_id",
        "status",
        "phone_number_id",
        "waba_id",
        "failure_code",
        "created_at",
        "updated_at",
    } <= columns
    for name in columns:
        assert "token" not in name
        assert "secret" not in name
        assert name != "code"
    table = WhatsAppConnectionAttempt.__table__
    assert table.c.phone_number_id.nullable is True
    assert table.c.waba_id.nullable is True
    assert table.c.failure_code.nullable is True
    assert table.c.attempt_id.nullable is False
    assert table.c.tenant_id.nullable is False


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _tenant(session) -> Tenant:
    tenant = Tenant(id=uuid4(), clinic_name="Clínica")
    session.add(tenant)
    await session.flush()
    return tenant


async def test_new_attempt_starts_pending(db):
    async with db() as session:
        tenant = await _tenant(session)
        row = WhatsAppConnectionAttempt(tenant_id=tenant.id, attempt_id="a-1")
        session.add(row)
        await session.commit()
        await session.refresh(row)
        assert row.status == "pending"
        assert row.created_at is not None
        assert row.updated_at is not None


async def test_attempt_id_is_unique_within_a_clinic(db):
    async with db() as session:
        tenant = await _tenant(session)
        session.add(WhatsAppConnectionAttempt(tenant_id=tenant.id, attempt_id="dup"))
        await session.commit()
        session.add(WhatsAppConnectionAttempt(tenant_id=tenant.id, attempt_id="dup"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_another_clinic_may_reuse_the_same_attempt_id(db):
    # Per-clinic namespace: one clinic can never block (or read) another's attempt.
    async with db() as session:
        first = await _tenant(session)
        second = await _tenant(session)
        session.add(WhatsAppConnectionAttempt(tenant_id=first.id, attempt_id="same"))
        session.add(WhatsAppConnectionAttempt(tenant_id=second.id, attempt_id="same"))
        await session.commit()


# --- migration -----------------------------------------------------------------------------


def test_the_migration_is_the_single_head_on_top_of_insurance_auto():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in scripts.walk_revisions(base="base", head=heads[0])}
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_creates_the_table_and_downgrade_drops_it(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'attempts.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tenants (id CHAR(32) PRIMARY KEY, clinic_name TEXT)"))
        conn.execute(sa.text("INSERT INTO tenants (id, clinic_name) VALUES ('t1', 'A')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns(TABLE)}
            assert {
                "id",
                "attempt_id",
                "tenant_id",
                "status",
                "phone_number_id",
                "waba_id",
                "failure_code",
                "brain_notified_at",
                "created_at",
                "updated_at",
            } == set(columns)
            assert not any("token" in name or "secret" in name for name in columns)
            conn.execute(
                sa.text(f"INSERT INTO {TABLE} (id, attempt_id, tenant_id) VALUES ('r1', 'x', 't1')")
            )
            assert conn.execute(sa.text(f"SELECT status FROM {TABLE}")).scalar() == "pending"
            with pytest.raises(IntegrityError):
                conn.execute(
                    sa.text(
                        f"INSERT INTO {TABLE} (id, attempt_id, tenant_id) VALUES ('r2', 'x', 't1')"
                    )
                )
            migration.downgrade()
            assert TABLE not in sa.inspect(conn).get_table_names()
