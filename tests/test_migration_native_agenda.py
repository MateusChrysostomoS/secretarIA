"""calendar_events - the native agenda's table (2026-10-20): additive, one new head."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.models.calendar_event import CalendarEvent

ROOT = Path(__file__).resolve().parents[1]
REVISION = "b7e2c4f9a1d6"
DOWN_REVISION = "a4c7e2f9b1d3"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_native_agenda_calendar_events.py"
    spec = importlib.util.spec_from_file_location("native_agenda_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_r9():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_creates_the_table_the_model_maps_and_downgrade_drops_it(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'agenda.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE tenants (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE professionals (id CHAR(32) PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            inspector = sa.inspect(conn)
            columns = {c["name"]: c for c in inspector.get_columns("calendar_events")}
            assert set(columns) == {c.name for c in CalendarEvent.__table__.columns}
            for name, column in CalendarEvent.__table__.columns.items():
                assert columns[name]["nullable"] is column.nullable, name
            indexes = {
                i["name"]: i["column_names"] for i in inspector.get_indexes("calendar_events")
            }
            assert indexes["ix_calendar_events_agenda_window"] == [
                "tenant_id",
                "professional_id",
                "start_at",
            ]
            assert "ix_calendar_events_tenant_id" in indexes
            fks = {fk["referred_table"]: fk["options"].get("ondelete") for fk in
                   inspector.get_foreign_keys("calendar_events")}
            assert fks == {"tenants": "CASCADE", "professionals": "CASCADE"}
            migration.downgrade()
            assert "calendar_events" not in sa.inspect(conn).get_table_names()
    engine.dispose()
