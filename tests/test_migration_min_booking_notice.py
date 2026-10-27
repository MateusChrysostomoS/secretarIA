"""professionals.min_booking_notice_hours (2026-10-27): additive, nullable, one new head."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.models.professional import Professional

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c3d9e1f5a7b2"
DOWN_REVISION = "b7e2c4f9a1d6"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_professional_min_booking_notice.py"
    spec = importlib.util.spec_from_file_location("min_booking_notice_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_the_native_agenda():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_adds_the_nullable_column_the_model_maps_and_downgrade_drops_it(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'pro.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE professionals (id CHAR(32) PRIMARY KEY, name TEXT)"))
        conn.execute(sa.text("INSERT INTO professionals (id, name) VALUES ('a', 'Dr. A')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("professionals")}
            assert columns["min_booking_notice_hours"]["nullable"] is True
            assert Professional.__table__.c.min_booking_notice_hours.nullable is True
            # Existing doctors get no rule.
            value = conn.execute(sa.text("SELECT min_booking_notice_hours FROM professionals"))
            assert value.scalar() is None
            migration.downgrade()
            columns = {c["name"] for c in sa.inspect(conn).get_columns("professionals")}
            assert "min_booking_notice_hours" not in columns
