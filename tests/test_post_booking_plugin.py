"""Tests for the post_booking seam: plugins/base.py's PostBookingContext,
registry.run_post_booking, and plugins/post_booking.py (enqueue helper + the
arq job that fans out to entitled plugins' hooks).

Mirrors the in-memory-sqlite pattern established by test_reminders_plugin.py
(a real DB, monkeypatched in place of the Postgres-backed
`async_session_factory`). The plugin REGISTRY is monkeypatched to an empty
dict per test so these tests never interact with the real ehr/pix_whatsapp/
analytics_bi plugins registered at import time.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

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
from secretaria.plugins import post_booking, registry  # noqa: E402
from secretaria.plugins.base import PluginSpec, PostBookingContext  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_whatsapp": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="bronze_1",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


def _ctx(**overrides) -> PostBookingContext:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    appointment = Appointment(id=uuid4(), tenant_id=tenant.id, google_event_id="evt-1")
    base = dict(tenant=tenant, patient=None, appointment=appointment, waba_token=None, source="agent")
    base.update(overrides)
    return PostBookingContext(**base)


@pytest.fixture(autouse=True)
def _empty_registry(monkeypatch: pytest.MonkeyPatch):
    """Isolate the registry so real plugins (ehr, pix_whatsapp, ...) never run."""
    monkeypatch.setattr(registry, "REGISTRY", {})
    yield


# --------------------------------------------------------------------------
# registry.run_post_booking
# --------------------------------------------------------------------------


async def test_all_entitled_hooks_run_no_short_circuit():
    calls: list[str] = []

    async def hook_a(ctx):
        calls.append("a")

    async def hook_b(ctx):
        calls.append("b")

    registry.register(PluginSpec(id="a", entitlement_keys=("ehr",), post_booking=hook_a))
    registry.register(PluginSpec(id="b", entitlement_keys=("pix_whatsapp",), post_booking=hook_b))

    summary = _summary(addons={**_ALL_ADDONS_OFF, "ehr": True, "pix_whatsapp": True})
    await registry.run_post_booking(summary, _ctx())

    assert sorted(calls) == ["a", "b"]


async def test_disabled_plugin_hook_never_runs():
    calls: list[str] = []

    async def hook_a(ctx):
        calls.append("a")

    registry.register(PluginSpec(id="a", entitlement_keys=("ehr",), post_booking=hook_a))

    summary = _summary()  # ehr addon off
    await registry.run_post_booking(summary, _ctx())

    assert calls == []


async def test_one_failing_hook_does_not_stop_others():
    calls: list[str] = []

    async def hook_boom(ctx):
        raise RuntimeError("ehr push exploded")

    async def hook_ok(ctx):
        calls.append("ok")

    registry.register(
        PluginSpec(id="boom", entitlement_keys=("ehr",), post_booking=hook_boom)
    )
    registry.register(
        PluginSpec(id="ok", entitlement_keys=("analytics_bi",), post_booking=hook_ok)
    )

    summary = _summary(addons={**_ALL_ADDONS_OFF, "ehr": True, "analytics_bi": True})
    # Must not raise.
    await registry.run_post_booking(summary, _ctx())

    assert calls == ["ok"]


async def test_plugin_without_post_booking_hook_is_skipped():
    registry.register(PluginSpec(id="no-hook", entitlement_keys=("ehr",)))
    summary = _summary(addons={**_ALL_ADDONS_OFF, "ehr": True})
    # Must not raise (post_booking is None).
    await registry.run_post_booking(summary, _ctx())


# --------------------------------------------------------------------------
# enqueue_post_booking_hooks
# --------------------------------------------------------------------------


async def test_enqueue_with_no_redis_is_silent_noop():
    # Must not raise.
    await post_booking.enqueue_post_booking_hooks(None, uuid4(), uuid4(), "agent")


async def test_enqueue_calls_redis_enqueue_job_with_expected_args():
    calls: list[tuple] = []

    class _FakeRedis:
        async def enqueue_job(self, name, *args):
            calls.append((name, args))

    tenant_id = uuid4()
    appointment_id = uuid4()
    await post_booking.enqueue_post_booking_hooks(_FakeRedis(), tenant_id, appointment_id, "flow")

    assert calls == [
        ("run_post_booking_hooks", (str(tenant_id), str(appointment_id), "flow"))
    ]


async def test_enqueue_swallows_redis_failure():
    class _BoomRedis:
        async def enqueue_job(self, *args, **kwargs):
            raise RuntimeError("redis down")

    # Must not raise: the booking already succeeded.
    await post_booking.enqueue_post_booking_hooks(_BoomRedis(), uuid4(), uuid4(), "agent")


# --------------------------------------------------------------------------
# run_post_booking_hooks (the arq job)
# --------------------------------------------------------------------------


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


async def _fake_get_waba_token(session, tenant_id):
    return "decrypted-waba-token"


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(post_booking, "async_session_factory", db)
    monkeypatch.setattr(post_booking, "get_waba_token", _fake_get_waba_token)
    yield


async def _make_appointment(db, *, with_patient: bool = True):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        patient = None
        patient_id = None
        if with_patient:
            patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999", name="Maria")
            session.add(patient)
            await session.flush()
            patient_id = patient.id
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient_id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
        )
        session.add(appointment)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(appointment)
        return tenant, patient, appointment


async def test_job_loads_rows_and_dispatches_to_registry(db, monkeypatch: pytest.MonkeyPatch):
    tenant, patient, appointment = await _make_appointment(db)
    captured: list[PostBookingContext] = []

    async def _fake_run_post_booking(summary, ctx):
        captured.append(ctx)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    monkeypatch.setattr(post_booking, "run_post_booking", _fake_run_post_booking)
    monkeypatch.setattr(post_booking, "get_entitlements", _fake_get_entitlements)

    await post_booking.run_post_booking_hooks({}, str(tenant.id), str(appointment.id), "flow")

    assert len(captured) == 1
    ctx = captured[0]
    assert ctx.tenant.id == tenant.id
    assert ctx.patient.id == patient.id
    assert ctx.appointment.id == appointment.id
    assert ctx.waba_token == "decrypted-waba-token"
    assert ctx.source == "flow"


async def test_job_appointment_with_no_patient_passes_none(db, monkeypatch: pytest.MonkeyPatch):
    tenant, _patient, appointment = await _make_appointment(db, with_patient=False)
    captured: list[PostBookingContext] = []

    async def _fake_run_post_booking(summary, ctx):
        captured.append(ctx)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    monkeypatch.setattr(post_booking, "run_post_booking", _fake_run_post_booking)
    monkeypatch.setattr(post_booking, "get_entitlements", _fake_get_entitlements)

    await post_booking.run_post_booking_hooks({}, str(tenant.id), str(appointment.id))

    assert captured[0].patient is None


async def test_job_missing_appointment_is_noop(db, monkeypatch: pytest.MonkeyPatch):
    called = False

    async def _fake_run_post_booking(summary, ctx):
        nonlocal called
        called = True

    monkeypatch.setattr(post_booking, "run_post_booking", _fake_run_post_booking)

    await post_booking.run_post_booking_hooks({}, str(uuid4()), str(uuid4()))

    assert called is False


async def test_job_unresolvable_entitlements_is_noop(db, monkeypatch: pytest.MonkeyPatch):
    tenant, _patient, appointment = await _make_appointment(db)
    called = False

    async def _fake_run_post_booking(summary, ctx):
        nonlocal called
        called = True

    async def _fake_get_entitlements(tenant_id, redis):
        return None

    monkeypatch.setattr(post_booking, "run_post_booking", _fake_run_post_booking)
    monkeypatch.setattr(post_booking, "get_entitlements", _fake_get_entitlements)

    await post_booking.run_post_booking_hooks({}, str(tenant.id), str(appointment.id))

    assert called is False
