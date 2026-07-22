"""Tests for plugins/pix_deposit.py — the real Pix deposit post_booking hook.

In-memory-sqlite pattern established by tests/test_reminders_plugin.py /
tests/test_deposit_lifecycle.py: a real DB, monkeypatched in place of
`async_session_factory`. `deposit_lifecycle.maybe_create_deposit` is
monkeypatched with a recorder (this file is NOT the place to re-test the
money brain itself — that's tests/test_deposit_lifecycle.py's job); these
tests only cover the plugin's OWN responsibilities: entitlement gating via
the registry, the no-patient no-op, re-fetching detached rows into a fresh
session-attached instance, and staying fail-open when the lifecycle call
explodes.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Appointment, Patient, Tenant  # noqa: E402
from secretaria.plugins import pix_deposit  # noqa: E402
from secretaria.plugins.base import PostBookingContext  # noqa: E402
from secretaria.plugins.registry import enabled_plugins  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.payments import deposit_lifecycle  # noqa: E402

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "analytics_bi_advanced": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _patch_session_factory(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(pix_deposit, "async_session_factory", db)
    yield


async def _make_rows(db, *, with_patient: bool = True):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinica Boa Saude",
            phone_number_id=str(uuid4())[:12],
            pix_deposit_enabled=True,
        )
        patient = None
        patient_id = None
        if with_patient:
            patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999", name="Maria")
            patient_id = patient.id
            session.add(patient)
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient_id,
            google_event_id="evt-1",
            appointment_type="Primeira consulta",
            start_at=datetime(2026, 8, 1, 14, 0, tzinfo=UTC),
            end_at=datetime(2026, 8, 1, 14, 30, tzinfo=UTC),
        )
        session.add_all([tenant, appointment])
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(appointment)
        if patient is not None:
            await session.refresh(patient)
        return tenant, patient, appointment


def _detached_ctx(tenant, patient, appointment, *, waba_token="tok") -> PostBookingContext:
    """A PostBookingContext carrying DETACHED copies (own session already
    closed) — mirrors what plugins/post_booking.py::run_post_booking_hooks
    hands the hook in production, per PostBookingContext's own docstring."""
    return PostBookingContext(
        tenant=tenant, patient=patient, appointment=appointment, waba_token=waba_token,
        source="agent",
    )


# --------------------------------------------------------------------------
# Registry gating — fail-closed on the addon, fail-open once entitled
# --------------------------------------------------------------------------


def test_addon_off_excludes_plugin_from_enabled_plugins():
    summary = _summary(addons={**_ALL_ADDONS_OFF, "pix_deposit": False})
    assert "pix_deposit" not in [s.id for s in enabled_plugins(summary)]


def test_addon_on_includes_plugin_in_enabled_plugins():
    summary = _summary(addons={**_ALL_ADDONS_OFF, "pix_deposit": True})
    assert "pix_deposit" in [s.id for s in enabled_plugins(summary)]


def test_spec_shape():
    assert pix_deposit.PIX_DEPOSIT_SPEC.id == "pix_deposit"
    assert pix_deposit.PIX_DEPOSIT_SPEC.entitlement_keys == ("pix_deposit",)


# --------------------------------------------------------------------------
# Hook behavior
# --------------------------------------------------------------------------


async def test_no_patient_is_silent_noop(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_rows(db, with_patient=False)
    calls: list = []

    async def _recorder(session, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(deposit_lifecycle, "maybe_create_deposit", _recorder)

    await pix_deposit._post_booking(_detached_ctx(tenant, None, appointment))

    assert calls == []


async def test_hook_refetches_rows_and_calls_maybe_create_deposit(
    db, monkeypatch: pytest.MonkeyPatch
):
    tenant, patient, appointment = await _make_rows(db)
    calls: list = []

    async def _recorder(session, *, tenant, patient, appointment, waba_token):
        # The rows handed to maybe_create_deposit must be attached to the
        # CURRENT session (re-fetched), not the caller's detached instances.
        assert session.get_bind() is not None
        calls.append(
            {
                "tenant_id": tenant.id,
                "patient_id": patient.id,
                "appointment_id": appointment.id,
                "waba_token": waba_token,
            }
        )
        return None

    monkeypatch.setattr(deposit_lifecycle, "maybe_create_deposit", _recorder)

    await pix_deposit._post_booking(_detached_ctx(tenant, patient, appointment, waba_token="tok-1"))

    assert len(calls) == 1
    assert calls[0] == {
        "tenant_id": tenant.id,
        "patient_id": patient.id,
        "appointment_id": appointment.id,
        "waba_token": "tok-1",
    }


async def test_hook_commits_deposit_lifecycle_mutations(db, monkeypatch: pytest.MonkeyPatch):
    """maybe_create_deposit mutates rows but never commits (see its own
    docstring) - the hook must commit, or a real deposit row would be lost."""
    tenant, patient, appointment = await _make_rows(db)

    async def _mutate(session, *, tenant, patient, appointment, waba_token):
        patient.asaas_customer_id = "cus_committed"
        return None

    monkeypatch.setattr(deposit_lifecycle, "maybe_create_deposit", _mutate)

    await pix_deposit._post_booking(_detached_ctx(tenant, patient, appointment))

    async with db() as session:
        refreshed = await session.get(Patient, patient.id)
        assert refreshed.asaas_customer_id == "cus_committed"


async def test_lifecycle_exception_never_raises(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_rows(db)

    async def _boom(session, **kwargs):
        raise RuntimeError("asaas down")

    monkeypatch.setattr(deposit_lifecycle, "maybe_create_deposit", _boom)

    # Must not raise.
    await pix_deposit._post_booking(_detached_ctx(tenant, patient, appointment))


async def test_missing_row_is_silent_noop(db, monkeypatch: pytest.MonkeyPatch):
    """A tenant/appointment id that no longer resolves (deleted between the
    booking commit and this fire-and-forget hook running) never crashes."""
    tenant, patient, appointment = await _make_rows(db)
    ghost_appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id="evt-ghost",
    )  # never persisted

    calls: list = []

    async def _recorder(session, **kwargs):
        calls.append(kwargs)
        return None

    monkeypatch.setattr(deposit_lifecycle, "maybe_create_deposit", _recorder)

    await pix_deposit._post_booking(_detached_ctx(tenant, patient, ghost_appointment))

    assert calls == []
