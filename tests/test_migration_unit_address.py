"""TASK-090 B2: the unit address-parts migration is additive, reversible and chains on one head."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
REVISION = "e1a7c3b9d5f2"
DOWN_REVISION = "f9a1d7c4e2b6"
PARTS = ("cep", "street", "number", "complement", "neighborhood", "city", "state")


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_unit_address_parts.py"
    spec = importlib.util.spec_from_file_location("unit_address_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_head_and_chains_on_previous_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in scripts.walk_revisions(base="base", head=heads[0])}
    assert _migration().down_revision == DOWN_REVISION


def _units_engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'units.db'}")
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE units "
                "(id CHAR(32) PRIMARY KEY, name VARCHAR(255), address VARCHAR(512))"
            )
        )
        conn.execute(sa.text("INSERT INTO units VALUES ('u1', 'Legacy', 'Rua A, 1')"))
    return engine


def test_upgrade_adds_nullable_parts_and_keeps_legacy_rows(tmp_path):
    migration = _migration()
    engine = _units_engine(tmp_path)
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.upgrade()
        cols = {c["name"]: c for c in sa.inspect(conn).get_columns("units")}
        for name in PARTS:
            assert cols[name]["nullable"] is True
        row = conn.execute(sa.text("SELECT address, cep, state FROM units")).one()
        assert row == ("Rua A, 1", None, None)


def test_downgrade_drops_the_parts_and_keeps_the_text(tmp_path):
    migration = _migration()
    engine = _units_engine(tmp_path)
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.upgrade()
        migration.downgrade()
        names = {c["name"] for c in sa.inspect(conn).get_columns("units")}
        assert names == {"id", "name", "address"}
        assert conn.execute(sa.text("SELECT address FROM units")).scalar() == "Rua A, 1"
