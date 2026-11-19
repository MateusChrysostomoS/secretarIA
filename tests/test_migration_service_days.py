"""TASK-087 T1: the blocked-weekdays migration is additive-ish, reversible and the single head."""

import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]
REVISION = "c8e2a4f6b1d3"
DOWN_REVISION = "f1c4b8d2a6e3"
TABLE = "professional_service_unit_terms"
PARENT = "b7d3f1a8c5e2"  # creates the table (the old CHECK lives there)


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "migrations" / "versions" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration():
    return _load("service_days_migration", f"{REVISION}_service_unit_blocked_weekdays.py")


def _base_migration():
    return _load("service_unit_terms_base", f"{PARENT}_professional_service_unit_terms.py")


def test_single_head_is_this_migration_on_top_of_the_previous_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert scripts.get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def _engine_at_previous_state(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'days.db'}")
    with engine.begin() as conn:
        for parent in ("tenants", "professionals", "services", "units"):
            conn.execute(sa.text(f"CREATE TABLE {parent} (id CHAR(32) PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(conn)):
            _base_migration().upgrade()
    return engine


def _insert(conn, *, unit, price=None, duration=None, days=None, with_days_column=True):
    cols = "professional_id, service_id, unit_id, tenant_id, price, duration_min"
    vals = ":p, :s, :u, :t, :price, :dur"
    params = {"p": "p", "s": "s", "u": unit, "t": "t", "price": price, "dur": duration}
    if with_days_column:
        cols += ", blocked_weekdays"
        vals += ", :days"
        params["days"] = days
    conn.execute(sa.text(f"INSERT INTO {TABLE} ({cols}) VALUES ({vals})"), params)


def test_upgrade_adds_column_and_accepts_a_days_only_row(tmp_path):
    migration = _migration()
    engine = _engine_at_previous_state(tmp_path)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            cols = {c["name"]: c for c in sa.inspect(conn).get_columns(TABLE)}
            assert cols["blocked_weekdays"]["nullable"] is True
            _insert(conn, unit="u1", days='["friday"]')  # only days: valid now
            _insert(conn, unit="u2", price="R$ 10,00")  # old shape still valid


def test_upgrade_still_refuses_a_row_with_all_three_null(tmp_path):
    migration = _migration()
    engine = _engine_at_previous_state(tmp_path)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            _insert(conn, unit="u1")


def test_upgrade_keeps_existing_rows(tmp_path):
    migration = _migration()
    engine = _engine_at_previous_state(tmp_path)
    with engine.begin() as conn:
        _insert(conn, unit="u1", price="R$ 250,00", duration=45, with_days_column=False)
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        row = conn.execute(
            sa.text(f"SELECT price, duration_min, blocked_weekdays FROM {TABLE}")
        ).one()
    assert tuple(row) == ("R$ 250,00", 45, None)


def test_downgrade_drops_days_only_rows_keeps_priced_ones_and_restores_the_old_check(tmp_path):
    migration = _migration()
    engine = _engine_at_previous_state(tmp_path)
    with engine.begin() as conn:
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            _insert(conn, unit="u1", days='["friday"]')
            _insert(conn, unit="u2", price="R$ 10,00", days='["monday"]')
            migration.downgrade()
            rows = conn.execute(sa.text(f"SELECT unit_id, price FROM {TABLE}")).all()
            assert [tuple(r) for r in rows] == [("u2", "R$ 10,00")]
            assert "blocked_weekdays" not in {
                c["name"] for c in sa.inspect(conn).get_columns(TABLE)
            }
    with pytest.raises(IntegrityError), engine.begin() as conn:
        _insert(conn, unit="u3", with_days_column=False)  # old CHECK is back: nothing set refused
