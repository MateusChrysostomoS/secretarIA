"""professionals.buffer_minutes (TASK-088): additive, nullable, no default, one new head."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.models.professional import Professional

ROOT = Path(__file__).resolve().parents[1]
REVISION = "d4b7a9c2e6f1"
DOWN_REVISION = "c8e2a4f6b1d3"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_professional_buffer_minutes.py"
    spec = importlib.util.spec_from_file_location("buffer_minutes_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_adds_buffer_minutes_and_single_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    heads = scripts.get_heads()
    assert len(heads) == 1
    assert REVISION in {r.revision for r in scripts.walk_revisions(base="base", head=heads[0])}
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
            assert columns["buffer_minutes"]["nullable"] is True
            assert columns["buffer_minutes"]["default"] is None
            assert Professional.__table__.c.buffer_minutes.nullable is True
            # Existing doctors get no buffer.
            value = conn.execute(sa.text("SELECT buffer_minutes FROM professionals"))
            assert value.scalar() is None
            migration.downgrade()
            columns = {c["name"] for c in sa.inspect(conn).get_columns("professionals")}
            assert "buffer_minutes" not in columns
