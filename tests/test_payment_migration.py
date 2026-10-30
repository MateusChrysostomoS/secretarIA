"""Migration c8a5d3f1b7e2 - payment provider core (TASK-016, spec section 6).

Three layers, cheapest first:
  1. OFFLINE: the generated SQL is additive (no DROP), creates the four tables and adds the
     columns the models declare. Needs no database.
  2. BACKFILL: `_backfill` (the data-copy half) runs on SQLite against legacy rows.
  3. POSTGRES (opt-in, `PAYMENTS_TEST_PG_URL=postgresql+asyncpg://user@host:port/postgres`):
     the real upgrade/downgrade/upgrade on a throwaway database, plus a drift check between
     the models and the migrated schema for every table this migration touches.
"""

import asyncio
import contextlib
import importlib.util
import io
import os
import pathlib
import re
import uuid

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from secretaria.config import get_settings
from secretaria.core.database import Base
from secretaria.models import (
    Appointment,
    ClinicPaymentAccount,
    Patient,
    PatientPaymentCustomer,
    PixDeposit,
    Tenant,
    TenantCredentials,
)

REPO = pathlib.Path(__file__).resolve().parent.parent
PREVIOUS_HEAD = "a4c7e2f9b1d3"  # re-parented onto main's head (TASK-050)
REVISION = "c8a5d3f1b7e2"
NEW_TABLES = (
    "clinic_payment_accounts",
    "payment_webhook_events",
    "patient_payment_customers",
    "payment_attention_items",
)
TOUCHED = NEW_TABLES + ("pix_deposits", "tenants", "booking_holds")
# Columns a LATER revision adds to this revision's tables (TASK-017, `e2b7c4a9d1f5`): the models
# declare them, this revision's CREATE TABLE rightly does not. The `last_charge_*` health
# columns come from `d4f7a1c9e2b6` (TASK-071, Plan 3).
ADDED_BY_LATER_REVISIONS = {
    ("payment_attention_items", "alerted_at"),
    ("clinic_payment_accounts", "last_charge_failure_reason"),
    ("clinic_payment_accounts", "last_charge_failure_stage"),
    ("clinic_payment_accounts", "last_charge_failure_at"),
    ("clinic_payment_accounts", "last_charge_ok_at"),
    # The onboarding states and the setup checklist come from `a7c3e9d1f5b2` (TASK-071, Plan 4).
    ("clinic_payment_accounts", "onboarding_general"),
    ("clinic_payment_accounts", "onboarding_docs"),
    ("clinic_payment_accounts", "onboarding_commercial"),
    ("clinic_payment_accounts", "onboarding_bank"),
    ("clinic_payment_accounts", "onboarding_checked_at"),
    ("clinic_payment_accounts", "credentials_changed_at"),
    ("clinic_payment_accounts", "setup_checklist"),
}


def _config() -> Config:
    cfg = Config(str(REPO / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO / "migrations"))
    return cfg


def _offline_sql() -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        command.upgrade(_config(), f"{PREVIOUS_HEAD}:{REVISION}", sql=True)
    return out.getvalue()


def _migration_module():
    path = next((REPO / "migrations" / "versions").glob(f"{REVISION}_*.py"))
    spec = importlib.util.spec_from_file_location("migration_payment_core", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------
# 1. offline SQL
# --------------------------------------------------------------------------


def test_revision_chain_has_a_single_head_after_the_previous_one():
    script = ScriptDirectory.from_config(_config())
    # TASK-017 chains `e2b7c4a9d1f5` on top of this revision, so it is no longer THE head; the
    # invariant that matters is one head overall and this revision still hanging off the
    # previous one.
    assert len(script.get_heads()) == 1
    assert script.get_revision(REVISION).down_revision == PREVIOUS_HEAD
    assert REVISION in {rev.revision for rev in script.walk_revisions()}


def test_offline_sql_is_additive_and_creates_the_four_tables():
    sql = _offline_sql()
    for table in NEW_TABLES:
        assert f"CREATE TABLE {table}" in sql
    assert "DROP " not in sql.upper().replace("DROP NOT NULL", "")
    assert "ALTER TABLE pix_deposits ALTER COLUMN appointment_id DROP NOT NULL" in sql
    assert (
        "ALTER TABLE tenants ADD COLUMN payment_provider VARCHAR(16) DEFAULT 'asaas' NOT NULL"
        in sql
    )
    assert "ALTER TABLE booking_holds ADD COLUMN phase VARCHAR(16) DEFAULT 'code' NOT NULL" in sql


def test_offline_sql_covers_every_column_the_models_declare():
    sql = _offline_sql()
    for table in NEW_TABLES:
        block = re.search(rf"CREATE TABLE {table} \((.*?)\n\);", sql, re.S).group(1)
        for column in Base.metadata.tables[table].columns:
            if (table, column.name) in ADDED_BY_LATER_REVISIONS:
                continue
            assert column.name in block, f"{table}.{column.name} missing from the migration"
    for column in (
        "provider", "provider_charge_id", "external_reference", "expires_at",
        "fulfillment_status", "confirming_event_id", "paid_amount_cents", "booking_snapshot",
        "hold_id", "calendar_event_id", "deposit_timing",
    ):
        assert f"ALTER TABLE pix_deposits ADD COLUMN {column} " in sql
    for column in ("payment_provider", "deposit_timing", "deposit_hold_ttl_minutes"):
        assert f"ALTER TABLE tenants ADD COLUMN {column} " in sql


# --------------------------------------------------------------------------
# 2. backfill on SQLite
# --------------------------------------------------------------------------


def _legacy_world(session: Session):
    with_key = Tenant(id=uuid.uuid4(), clinic_name="A", phone_number_id="pn-a")
    without_key = Tenant(id=uuid.uuid4(), clinic_name="B", phone_number_id="pn-b")
    session.add_all([with_key, without_key])
    session.flush()
    session.add_all(
        [
            TenantCredentials(
                tenant_id=with_key.id,
                asaas_api_key_encrypted="CIPHER-KEY",
                asaas_webhook_token_encrypted="CIPHER-TOKEN",
            ),
            TenantCredentials(tenant_id=without_key.id, waba_token_encrypted="CIPHER-WABA"),
        ]
    )
    patient = Patient(
        id=uuid.uuid4(),
        tenant_id=with_key.id,
        wa_id="5511999999999",
        name="Maria",
        asaas_customer_id="cus_legacy",
    )
    plain_patient = Patient(id=uuid.uuid4(), tenant_id=with_key.id, wa_id="5511888888888")
    session.add_all([patient, plain_patient])
    session.flush()
    appointment = Appointment(
        id=uuid.uuid4(),
        tenant_id=with_key.id,
        patient_id=patient.id,
        google_event_id="evt-1",
        status="scheduled",
    )
    session.add(appointment)
    session.flush()
    deposit = PixDeposit(
        tenant_id=with_key.id,
        appointment_id=appointment.id,
        patient_id=patient.id,
        asaas_payment_id="pay_legacy",
        amount_cents=6000,
        percent_applied=30,
    )
    unsent = PixDeposit(tenant_id=with_key.id, amount_cents=100, percent_applied=30)
    session.add_all([deposit, unsent])
    session.commit()
    return with_key, without_key, patient, appointment, deposit


def test_backfill_copies_asaas_data_without_reencrypting_and_is_idempotent():
    module = _migration_module()
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        with_key, without_key, patient, appointment, deposit = _legacy_world(session)
        connection = session.connection()
        module._backfill(connection)
        module._backfill(connection)  # a second run must change nothing
        session.commit()

        accounts = session.scalars(select(ClinicPaymentAccount)).all()
        assert [a.tenant_id for a in accounts] == [with_key.id]
        account = accounts[0]
        assert account.provider == "asaas"
        assert account.status == "connected"
        assert account.api_key_encrypted == "CIPHER-KEY"  # copied, not re-encrypted
        assert account.webhook_token_encrypted == "CIPHER-TOKEN"
        assert account.last_verified_at is None  # legacy: usable for `after`, not for `before`
        assert str(uuid.UUID(account.webhook_slug)) == account.webhook_slug
        assert not any(a.tenant_id == without_key.id for a in accounts)

        mappings = session.scalars(select(PatientPaymentCustomer)).all()
        assert [(m.patient_id, m.provider_customer_id) for m in mappings] == [
            (patient.id, "cus_legacy")
        ]

        session.refresh(deposit)
        assert deposit.provider_charge_id == "pay_legacy"
        assert deposit.external_reference == str(appointment.id)
        assert deposit.asaas_payment_id == "pay_legacy"  # the legacy column is never cleared
        others = session.scalars(select(PixDeposit).where(PixDeposit.id != deposit.id)).all()
        assert [o.provider_charge_id for o in others] == [None]


# --------------------------------------------------------------------------
# 3. real Postgres (opt-in)
# --------------------------------------------------------------------------

PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
needs_pg = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to a throwaway server")


def _with_database(monkeypatch, name: str) -> str:
    """Create database `name` on the throwaway server and point Settings/alembic at it."""
    admin_url = PG_URL
    base, _, _ = admin_url.rpartition("/")
    target = f"{base}/{name}"

    async def _create():
        engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
        async with engine.connect() as conn:
            await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
        await engine.dispose()

    asyncio.run(_create())
    monkeypatch.setenv("DATABASE_URL", target)
    get_settings.cache_clear()
    return target


def _drift(url: str) -> list:
    async def _run():
        engine = create_async_engine(url)
        async with engine.connect() as conn:

            def _diff(sync_conn):
                ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
                return compare_metadata(ctx, Base.metadata)

            diffs = await conn.run_sync(_diff)
        await engine.dispose()
        return diffs

    def _flatten(items):
        for item in items:
            if isinstance(item, list):
                yield from _flatten(item)
            else:
                yield item

    # Only what THIS migration adds: `booking_holds.created_at` nullability drift, for one,
    # predates it and is not ours to fix here.
    mine = re.compile(
        "|".join(NEW_TABLES)
        + "|pix_deposits|payment_provider|deposit_timing|deposit_hold_ttl_minutes"
        + "|'phase'|'charge_id'"
    )
    return [d for d in _flatten(asyncio.run(_run())) if mine.search(str(d))]


@needs_pg
def test_postgres_upgrade_downgrade_upgrade_and_no_model_drift(monkeypatch):
    url = _with_database(monkeypatch, f"t016_{uuid.uuid4().hex[:10]}")
    cfg = _config()
    command.upgrade(cfg, "head")
    assert _drift(url) == []
    command.downgrade(cfg, PREVIOUS_HEAD)
    command.upgrade(cfg, "head")
    assert _drift(url) == []


@needs_pg
def test_postgres_downgrade_refuses_while_a_charge_has_no_appointment(monkeypatch):
    url = _with_database(monkeypatch, f"t016_{uuid.uuid4().hex[:10]}")
    cfg = _config()
    command.upgrade(cfg, "head")
    tenant_id = uuid.uuid4()

    async def _seed():
        engine = create_async_engine(url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            session.add(Tenant(id=tenant_id, clinic_name="C", phone_number_id="pn-mig"))
            await session.flush()
            session.add(PixDeposit(tenant_id=tenant_id, amount_cents=100, percent_applied=30))
            await session.commit()
        await engine.dispose()

    asyncio.run(_seed())
    with pytest.raises(RuntimeError, match="cannot downgrade"):
        command.downgrade(cfg, PREVIOUS_HEAD)


@needs_pg
def test_postgres_backfill_binds_uuids_and_copies_the_legacy_rows(monkeypatch):
    url = _with_database(monkeypatch, f"t016_{uuid.uuid4().hex[:10]}")
    command.upgrade(_config(), "head")
    module = _migration_module()

    async def _run():
        engine = create_async_engine(url)
        async with async_sessionmaker(engine, expire_on_commit=False)() as session:
            with_key, _without_key, patient, appointment, deposit = await session.run_sync(
                _legacy_world
            )
            await session.run_sync(lambda sync_session: module._backfill(sync_session.connection()))
            await session.commit()
            account = await session.scalar(select(ClinicPaymentAccount))
            mapping = await session.scalar(select(PatientPaymentCustomer))
            await session.refresh(deposit)
            result = (
                account.tenant_id == with_key.id,
                account.api_key_encrypted,
                mapping.patient_id == patient.id,
                deposit.provider_charge_id,
                deposit.external_reference == str(appointment.id),
            )
        await engine.dispose()
        return result

    assert asyncio.run(_run()) == (True, "CIPHER-KEY", True, "pay_legacy", True)
