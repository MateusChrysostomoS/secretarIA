"""tenants.reminder_extra_days_before + reminder_extra_send_time (TASK-048 R9, spec §6.2)."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.core.extra_reminder import from_legacy_lead
from secretaria.models import Tenant
from tests._reminder_fixtures import db  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a4c7e2f9b1d3"
DOWN_REVISION = "d8e3a5c1f7b2"
# (row id, legacy minutes) - the same table test_extra_reminder.py checks in Python.
LEGACY = [
    ("off", None),
    ("zero", 0),
    ("short", 720),
    ("min", 1500),
    ("two", 2880),
    ("three", 2881),
    ("five", 7200),
    ("max", 20160),
    ("long", 30000),
]


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_extra_reminder_day_time.py"
    spec = importlib.util.spec_from_file_location("extra_reminder_day_time_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_r7():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_converts_legacy_minutes_like_the_python_rule_and_downgrade_removes(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'r9.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE tenants (id VARCHAR(16) PRIMARY KEY, "
                "reminder_extra_lead_minutes INTEGER)"
            )
        )
        for row_id, lead in LEGACY:
            conn.execute(
                sa.text("INSERT INTO tenants (id, reminder_extra_lead_minutes) VALUES (:i, :l)"),
                {"i": row_id, "l": lead},
            )
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            cols = {c["name"]: c for c in sa.inspect(conn).get_columns("tenants")}
            assert cols["reminder_extra_days_before"]["nullable"] is True
            assert cols["reminder_extra_send_time"]["nullable"] is True
            rows = {
                r[0]: (r[1], r[2], r[3])
                for r in conn.execute(
                    sa.text(
                        "SELECT id, reminder_extra_days_before, reminder_extra_send_time, "
                        "reminder_extra_lead_minutes FROM tenants"
                    )
                )
            }
            for row_id, lead in LEGACY:
                assert rows[row_id][:2] == from_legacy_lead(lead, current_send_time=None), row_id
                assert rows[row_id][2] == lead, "the legacy column is left as it was"
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("tenants")}
            assert "reminder_extra_days_before" not in names
            assert "reminder_extra_send_time" not in names
            assert "reminder_extra_lead_minutes" in names
    engine.dispose()


async def test_the_model_maps_both_columns_as_nullable(db):  # noqa: F811
    assert Tenant.__table__.c.reminder_extra_days_before.nullable is True
    assert Tenant.__table__.c.reminder_extra_send_time.nullable is True
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica", phone_number_id=None)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        assert (tenant.reminder_extra_days_before, tenant.reminder_extra_send_time) == (None, None)
