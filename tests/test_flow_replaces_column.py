"""conversations.flow_replaces_appointment_id (TASK-032 R3, spec §4.3 "Marcar outra consulta")."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import importlib.util  # noqa: E402
from pathlib import Path  # noqa: E402

import sqlalchemy as sa  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from alembic.operations import Operations  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

from secretaria.models import Conversation  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a7e2c9d4f1b6"
# The single head before this plan (Task 1 Step 1): TASK-030 P2a's revision,
# which sits on top of R1's b8d3f1a6c2e5.
DOWN_REVISION = "e7d3c1a9b5f2"


def _migration():
    path = (
        ROOT / "migrations" / "versions" / f"{REVISION}_conversation_flow_replaces_appointment.py"
    )
    spec = importlib.util.spec_from_file_location("flow_replaces_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_migration_is_the_single_head_on_top_of_the_previous_one():
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    assert ScriptDirectory.from_config(config).get_heads() == [REVISION]
    assert _migration().down_revision == DOWN_REVISION


def test_the_migration_adds_and_drops_a_nullable_column_on_sqlite(tmp_path):
    migration = _migration()
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'replaces.db'}")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE appointments (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("CREATE TABLE conversations (id CHAR(32) PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO conversations (id) VALUES ('a')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
            columns = {c["name"]: c for c in sa.inspect(conn).get_columns("conversations")}
            assert columns["flow_replaces_appointment_id"]["nullable"] is True
            value = conn.execute(
                sa.text("SELECT flow_replaces_appointment_id FROM conversations")
            ).scalar()
            assert value is None
            migration.downgrade()
            names = {c["name"] for c in sa.inspect(conn).get_columns("conversations")}
            assert "flow_replaces_appointment_id" not in names
    engine.dispose()


def test_the_model_maps_a_nullable_fk_that_survives_the_appointment():
    column = Conversation.__table__.c.flow_replaces_appointment_id
    assert column.nullable is True
    [fk] = column.foreign_keys
    assert fk.target_fullname == "appointments.id"
    assert fk.ondelete == "SET NULL"
