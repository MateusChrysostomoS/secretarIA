"""tenants.paid_notices_auto_approved + appointments.post_consult_notified_at (TASK-032 R7)."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.models import Appointment, Tenant
from tests._reminder_fixtures import db  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
REVISION = "d8e3a5c1f7b2"
DOWN_REVISION = "c2d5f8a1e4b6"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_clinic_action_notices.py"
    spec = importlib.util.spec_from_file_location("clinic_action_notices_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_sits_on_top_of_the_edit_outbox():
    # The head itself is pinned by the newest migration's test (R9: a4c7e2f9b1d3).
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_revision(REVISION) is not None
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_adds_both_columns_with_safe_values_and_downgrade_removes_them(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'r7.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tenants (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE appointments (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO tenants (id) VALUES ('t')"))
        conn.execute(sa.text("INSERT INTO appointments (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            tenants = {c["name"]: c for c in sa.inspect(conn).get_columns("tenants")}
            appointments = {c["name"]: c for c in sa.inspect(conn).get_columns("appointments")}
            assert tenants["paid_notices_auto_approved"]["nullable"] is False
            assert appointments["post_consult_notified_at"]["nullable"] is True
            assert appointments["google_calendar_source"]["nullable"] is True
            source = conn.execute(
                sa.text("SELECT google_calendar_source FROM appointments")
            ).scalar()
            assert source is None
            flag = conn.execute(sa.text("SELECT paid_notices_auto_approved FROM tenants")).scalar()
            assert flag in (0, False)
            marker = conn.execute(
                sa.text("SELECT post_consult_notified_at FROM appointments")
            ).scalar()
            assert marker is None
            migration.downgrade()
            assert "google_calendar_source" not in {
                c["name"] for c in sa.inspect(conn).get_columns("appointments")
            }
            assert "paid_notices_auto_approved" not in {
                c["name"] for c in sa.inspect(conn).get_columns("tenants")
            }
            assert "post_consult_notified_at" not in {
                c["name"] for c in sa.inspect(conn).get_columns("appointments")
            }
    engine.dispose()


async def test_the_models_map_the_columns_with_their_defaults(db):  # noqa: F811
    assert Tenant.__table__.c.paid_notices_auto_approved.nullable is False
    assert Appointment.__table__.c.post_consult_notified_at.nullable is True
    assert Appointment.__table__.c.google_calendar_source.nullable is True
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica", phone_number_id=None)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        assert tenant.paid_notices_auto_approved is False
