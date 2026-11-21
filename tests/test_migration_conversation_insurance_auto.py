"""The booking provenance migration adds a nullable column without guessing legacy choices."""

from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from secretaria.models import Conversation

ROOT = Path(__file__).resolve().parents[1]
REVISION = "f9a1d7c4e2b6"


def test_upgrade_adds_nullable_boolean_and_downgrade_preserves_existing_row(tmp_path):
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    scripts = ScriptDirectory.from_config(config)
    assert len(scripts.get_heads()) == 1
    assert REVISION in {script.revision for script in scripts.walk_revisions()}
    migration = scripts.get_revision(REVISION).module
    assert migration.down_revision == "d4b7a9c2e6f1"

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'conversation.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE conversations (id TEXT PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO conversations (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {col["name"]: col for col in sa.inspect(conn).get_columns("conversations")}
            assert columns["flow_insurance_auto"]["nullable"] is True
            assert columns["flow_insurance_auto"]["default"] is None
            assert Conversation.__table__.c.flow_insurance_auto.nullable is True
            query = sa.text("SELECT flow_insurance_auto FROM conversations")
            assert conn.execute(query).scalar_one() is None
            migration.downgrade()
        assert [col["name"] for col in sa.inspect(conn).get_columns("conversations")] == ["id"]
        assert conn.execute(sa.text("SELECT id FROM conversations")).scalar_one() == "a"
