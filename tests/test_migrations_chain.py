"""TASK-090 B9: the `custom_payment_note` DROP is the last revision; nothing maps the column."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

ROOT = Path(__file__).resolve().parents[1]
REVISION = "b4d0f6e8a2c5"
LAST_ADDITIVE = "a3c9e5d7f1b4"
TABLES = ("tenant_insurance_plans", "professional_insurance_plans")


def _scripts() -> ScriptDirectory:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(config)


def _migration():
    path = ROOT / "migrations" / "versions" / f"{REVISION}_drop_custom_payment_note.py"
    spec = importlib.util.spec_from_file_location("drop_custom_payment_note_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_alembic_head():
    assert _scripts().get_heads() == [REVISION]


def test_drop_revision_is_the_head_and_parent_is_last_additive():
    migration = _migration()
    assert migration.revision == REVISION
    assert migration.down_revision == LAST_ADDITIVE


def test_drop_is_its_own_revision_not_mixed_with_additive_ones():
    """No earlier revision of this task may drop the column (the old code still reads it)."""
    versions = ROOT / "migrations" / "versions"
    droppers = [
        p.name
        for p in versions.glob("*.py")
        if 'drop_column("custom_payment_note")' in p.read_text(encoding="utf-8")
        and not p.name.startswith(("d4f8a2c6e913", REVISION))
    ]
    assert droppers == []


def test_models_do_not_reference_dropped_column():
    offenders = [
        str(p.relative_to(ROOT))
        for p in (ROOT / "src").rglob("*.py")
        if "custom_payment_note" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def _engine_with_column(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'plans.db'}")
    with engine.begin() as conn:
        for table in TABLES:
            conn.execute(
                sa.text(
                    f"CREATE TABLE {table} "
                    "(id CHAR(32) PRIMARY KEY, custom_name VARCHAR(120), custom_payment_note TEXT)"
                )
            )
            conn.execute(sa.text(f"INSERT INTO {table} VALUES ('p1', 'Outro', 'old note')"))
    return engine


def test_upgrade_drops_the_column_and_downgrade_recreates_it_empty(tmp_path):
    migration = _migration()
    engine = _engine_with_column(tmp_path)
    with engine.begin() as conn, Operations.context(MigrationContext.configure(conn)):
        migration.upgrade()
        for table in TABLES:
            names = {c["name"] for c in sa.inspect(conn).get_columns(table)}
            assert "custom_payment_note" not in names
            assert conn.execute(sa.text(f"SELECT custom_name FROM {table}")).scalar() == "Outro"
        migration.downgrade()
        for table in TABLES:
            cols = {c["name"]: c for c in sa.inspect(conn).get_columns(table)}
            assert cols["custom_payment_note"]["nullable"] is True
            query = sa.text(f"SELECT custom_payment_note FROM {table}")
            assert conn.execute(query).scalar() is None
