"""TASK-083 multi-unit + shared agenda: additive, one new head on top of d2f6b8a4c1e7."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
REVISION = "e4a8c2f6b1d9"
DOWN_REVISION = "d2f6b8a4c1e7"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_multi_unit_shared_agenda.py"
    spec = importlib.util.spec_from_file_location("multi_unit_agenda_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_there_is_exactly_one_head_and_it_is_this_migration_on_top_of_the_previous_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert heads == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_the_docstring_carries_the_deploy_order():
    assert "DEPLOY ORDER" in (_migration().__doc__ or "")


def _legacy_schema(conn):
    for stmt in (
        "CREATE TABLE tenants (id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE units (id CHAR(32) PRIMARY KEY)",
        "CREATE TABLE professionals (id CHAR(32) PRIMARY KEY, name TEXT)",
        "CREATE TABLE calendar_events (id VARCHAR(64) PRIMARY KEY)",
        "CREATE TABLE booking_holds (id CHAR(32) PRIMARY KEY)",
        "INSERT INTO professionals (id, name) VALUES ('a', 'Dr. A')",
    ):
        conn.execute(sa.text(stmt))


def test_upgrade_adds_the_tables_and_nullable_columns_and_downgrade_removes_them(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'agenda.db'}")
    with engine.begin() as conn:
        _legacy_schema(conn)
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            inspector = sa.inspect(conn)
            assert {"agendas", "professional_units"} <= set(inspector.get_table_names())
            agenda_cols = {c["name"]: c for c in inspector.get_columns("agendas")}
            assert set(agenda_cols) == {"id", "person_key", "created_at"}
            assert agenda_cols["person_key"]["nullable"] is False
            unit_cols = {c["name"]: c for c in inspector.get_columns("professional_units")}
            assert unit_cols["business_hours"]["nullable"] is True
            assert {"professional_id", "unit_id", "is_active", "sort_order"} <= set(unit_cols)
            for table, column in (
                ("professionals", "agenda_id"),
                ("calendar_events", "unit_id"),
                ("booking_holds", "unit_id"),
            ):
                cols = {c["name"]: c for c in inspector.get_columns(table)}
                assert cols[column]["nullable"] is True
            # Existing rows are untouched: nobody is linked to an agenda.
            assert conn.execute(sa.text("SELECT agenda_id FROM professionals")).scalar() is None
            uniques = inspector.get_unique_constraints("agendas")
            indexes = inspector.get_indexes("agendas")
            assert any(u["column_names"] == ["person_key"] for u in uniques) or any(
                i["unique"] and i["column_names"] == ["person_key"] for i in indexes
            )

            migration.downgrade()
            inspector = sa.inspect(conn)
            assert not {"agendas", "professional_units"} & set(inspector.get_table_names())
            assert "agenda_id" not in {c["name"] for c in inspector.get_columns("professionals")}
            assert "unit_id" not in {c["name"] for c in inspector.get_columns("calendar_events")}
            assert "unit_id" not in {c["name"] for c in inspector.get_columns("booking_holds")}
