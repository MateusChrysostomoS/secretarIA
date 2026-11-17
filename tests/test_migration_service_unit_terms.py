"""TASK-085 T2: the terms table migration is additive, reversible and the single head."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
REVISION = "b7d3f1a8c5e2"
DOWN_REVISION = "a4e7c2f9d6b1"
TABLE = "professional_service_unit_terms"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_professional_service_unit_terms.py"
    spec = importlib.util.spec_from_file_location("service_unit_terms_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_head_is_this_migration_on_top_of_the_previous_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert REVISION in {rev.revision for rev in scripts.walk_revisions()}
    assert _migration().down_revision == DOWN_REVISION


def test_upgrade_creates_the_table_and_downgrade_drops_it(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'terms.db'}")
    with engine.begin() as conn:
        for parent in ("tenants", "professionals", "services", "units"):
            conn.execute(sa.text(f"CREATE TABLE {parent} (id CHAR(32) PRIMARY KEY)"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            inspector = sa.inspect(conn)
            assert TABLE in inspector.get_table_names()
            cols = {c["name"]: c for c in inspector.get_columns(TABLE)}
            assert cols["price"]["nullable"] is True
            assert cols["duration_min"]["nullable"] is True
            assert cols["tenant_id"]["nullable"] is False
            pk = inspector.get_pk_constraint(TABLE)["constrained_columns"]
            assert set(pk) == {"professional_id", "service_id", "unit_id"}
            migration.downgrade()
            assert TABLE not in sa.inspect(conn).get_table_names()
