"""conversations.flow_selected_unit_id (TASK-083 spec §8): the unit a booking in progress uses."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.models import Conversation

ROOT = Path(__file__).resolve().parents[1]
REVISION = "b5d9e3f7a1c4"
DOWN_REVISION = "e4a8c2f6b1d9"


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_conversation_flow_selected_unit.py"
    spec = importlib.util.spec_from_file_location("flow_selected_unit_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_sits_on_the_multi_unit_migration_and_is_the_single_head():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert len(script.get_heads()) == 1
    assert REVISION in {rev.revision for rev in script.walk_revisions()}
    assert _migration().down_revision == DOWN_REVISION
    assert "DEPLOY ORDER" in (_migration().__doc__ or "")


def test_the_migration_adds_and_drops_a_nullable_column_on_sqlite(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'unit.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE units (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE conversations (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO conversations (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("conversations")}
            assert columns["flow_selected_unit_id"]["nullable"] is True
            value = conn.execute(sa.text("SELECT flow_selected_unit_id FROM conversations"))
            assert value.scalar() is None
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("conversations")}
            assert "flow_selected_unit_id" not in names
    engine.dispose()


def test_the_model_maps_a_nullable_fk_that_survives_the_unit():
    column = Conversation.__table__.c.flow_selected_unit_id
    assert column.nullable is True
    [fk] = column.foreign_keys
    assert fk.target_fullname == "units.id"
    assert fk.ondelete == "SET NULL"
