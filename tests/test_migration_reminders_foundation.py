"""The R1 migration: additive, reversible, in sync with the model (TASK-032).

The full Alembic chain cannot run on SQLite (older revisions are Postgres
specific), so the migration module is loaded directly and its upgrade() /
downgrade() run against a stub schema holding just the tables it touches. The
Postgres run is the manual step in the plan (Task 2, Step 6).
"""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from secretaria.models import AppointmentReminder

ROOT = Path(__file__).resolve().parent.parent
MIGRATION = ROOT / "migrations" / "versions" / "b8d3f1a6c2e5_appointment_reminders_foundation.py"


def _load():
    spec = importlib.util.spec_from_file_location("mig_b8d3f1a6c2e5", MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stub_engine() -> sa.Engine:
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        for name in ("tenants", "patients", "appointments"):
            conn.exec_driver_sql(f"CREATE TABLE {name} (id CHAR(32) PRIMARY KEY)")
    return engine


def _run(engine: sa.Engine, fn_name: str) -> None:
    module = _load()
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            getattr(module, fn_name)()


def test_there_is_exactly_one_head_and_it_is_ours():
    script = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini")))
    assert script.get_heads() == ["b8d3f1a6c2e5"]
    assert _load().down_revision == "c3a9e5f1d7b2"


def test_upgrade_adds_columns_and_table_then_downgrade_removes_them():
    engine = _stub_engine()
    _run(engine, "upgrade")
    insp = sa.inspect(engine)

    appt_cols = {c["name"]: c for c in insp.get_columns("appointments")}
    assert {"confirmation_count", "first_confirmed_at", "last_confirmed_at"} <= set(appt_cols)
    assert appt_cols["confirmation_count"]["nullable"] is False
    assert appt_cols["first_confirmed_at"]["nullable"] is True

    tenant_cols = {c["name"]: c for c in insp.get_columns("tenants")}
    assert tenant_cols["reminders_v2_enabled"]["nullable"] is False
    assert tenant_cols["reminder_extra_lead_minutes"]["nullable"] is True

    index_names = {i["name"] for i in insp.get_indexes("appointment_reminders")}
    assert {
        "ix_appointment_reminders_status_due",
        "ix_appointment_reminders_warn_due",
        "ix_appointment_reminders_tenant_id",
        "ix_appointment_reminders_appointment_id",
    } <= index_names
    version_index = next(
        i
        for i in insp.get_indexes("appointment_reminders")
        if i["name"] == "uq_appointment_reminders_version"
    )
    assert version_index["unique"]
    assert str(version_index["dialect_options"]["sqlite_where"]) == "invalidated_at IS NULL"

    # Existing rows get the safe defaults (additive: old rows stay valid).
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO appointments (id) VALUES ('a1')")
        conn.exec_driver_sql("INSERT INTO tenants (id) VALUES ('t1')")
        assert conn.exec_driver_sql("SELECT confirmation_count FROM appointments").scalar() == 0
        assert not conn.exec_driver_sql("SELECT reminders_v2_enabled FROM tenants").scalar()

    _run(engine, "downgrade")
    insp = sa.inspect(engine)
    assert "appointment_reminders" not in insp.get_table_names()
    assert "confirmation_count" not in {c["name"] for c in insp.get_columns("appointments")}
    assert "reminders_v2_enabled" not in {c["name"] for c in insp.get_columns("tenants")}


def test_migrated_table_has_exactly_the_model_columns():
    engine = _stub_engine()
    _run(engine, "upgrade")
    migrated = {c["name"] for c in sa.inspect(engine).get_columns("appointment_reminders")}
    modelled = {c.name for c in AppointmentReminder.__table__.columns}
    assert migrated == modelled
