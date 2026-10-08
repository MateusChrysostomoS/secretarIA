"""Additive outbox migration and revision uniqueness on a disposable database."""

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from secretaria.models import ProfessionalEditNotice

ROOT = Path(__file__).resolve().parent.parent


def test_upgrade_defaults_unique_revision_and_downgrade():
    file = ROOT / "migrations/versions/c2d5f8a1e4b6_professional_edit_notices.py"
    spec = importlib.util.spec_from_file_location("edit_notice_migration", file)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    scripts = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert scripts.get_heads() == [migration.revision]
    assert migration.down_revision == "b1c4e7a2d9f3"
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE tenants (id CHAR(32) PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE appointments (id CHAR(32) PRIMARY KEY)")
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        values = dict(
            id=uuid4(),
            tenant_id=uuid4(),
            appointment_id=uuid4(),
            revision=1,
            version="version",
            changed_fields=["data"],
        )
        table = ProfessionalEditNotice.__table__
        conn.execute(table.insert().values(**values))
        row = conn.execute(sa.select(table)).first()
        assert row.status == "pending" and row.dispatch_attempts == row.attempts == 0
        with pytest.raises(sa.exc.IntegrityError):
            conn.execute(table.insert().values(**{**values, "id": uuid4()}))
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert "professional_edit_notices" not in sa.inspect(conn).get_table_names()
    engine.dispose()
