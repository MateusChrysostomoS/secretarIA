"""Tests for plugins/analytics_bi.py (post_booking hook) and the doctor-hub
GET /tenants/me/analytics/summary endpoint (api/hub/analytics.py).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub import analytics as analytics_api  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Appointment, Tenant  # noqa: E402
from secretaria.models.analytics_event import AnalyticsEvent  # noqa: E402
from secretaria.plugins import analytics_bi  # noqa: E402
from secretaria.plugins.base import PostBookingContext  # noqa: E402
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


def _entitled_fake(**overrides):
    async def _fake(tenant_id, redis=None):
        return _summary(**overrides)

    return _fake


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


# --------------------------------------------------------------------------
# The post_booking hook itself
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _patch_session_factory(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(analytics_bi, "async_session_factory", db)
    yield


async def test_hook_records_minimal_non_personal_payload(db):
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    professional_id = uuid4()
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        google_event_id="evt-1",
        appointment_type="Primeira consulta",
        professional_id=professional_id,
    )
    ctx = PostBookingContext(
        tenant=tenant, patient=None, appointment=appointment, waba_token=None, source="flow"
    )

    await analytics_bi._post_booking(ctx)

    async with db() as session:
        rows = (await session.scalars(select(AnalyticsEvent))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tenant_id == tenant.id
    assert row.event_type == "appointment_booked"
    assert row.payload == {
        "appointment_type": "Primeira consulta",
        "professional_id": str(professional_id),
        "source": "flow",
    }


async def test_hook_db_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(analytics_bi, "async_session_factory", _boom)
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    appointment = Appointment(id=uuid4(), tenant_id=tenant.id, google_event_id="evt-1")
    ctx = PostBookingContext(
        tenant=tenant, patient=None, appointment=appointment, waba_token=None, source="agent"
    )

    # Must not raise.
    await analytics_bi._post_booking(ctx)


# --------------------------------------------------------------------------
# GET /tenants/me/analytics/summary
# --------------------------------------------------------------------------

ENDPOINT = "/tenants/me/analytics/summary"


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    async with db() as session:
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant():
        return tenant

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _seed_event(db, tenant: Tenant, *, appointment_type: str, days_ago: int = 0):
    async with db() as session:
        session.add(
            AnalyticsEvent(
                tenant_id=tenant.id,
                event_type="appointment_booked",
                payload={
                    "appointment_type": appointment_type,
                    "professional_id": None,
                    "source": "agent",
                },
                created_at=datetime.now(UTC) - timedelta(days=days_ago),
            )
        )
        await session.commit()


async def test_summary_not_entitled_returns_403(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(analytics_api, "get_entitlements", _entitled_fake())  # addon off
    response = await client.get(ENDPOINT)
    assert response.status_code == 403
    assert response.json()["detail"] == "analytics_not_entitled"


async def test_summary_entitlement_fetch_failure_fails_closed_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
):
    async def _fake_none(tenant_id, redis=None):
        return None

    monkeypatch.setattr(analytics_api, "get_entitlements", _fake_none)
    response = await client.get(ENDPOINT)
    assert response.status_code == 503


async def test_summary_entitled_computes_counts_and_by_type(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        analytics_api,
        "get_entitlements",
        _entitled_fake(addons={**_ALL_ADDONS_OFF, "analytics_bi": True}),
    )
    await _seed_event(db, tenant, appointment_type="Primeira consulta", days_ago=1)
    await _seed_event(db, tenant, appointment_type="Primeira consulta", days_ago=40)
    await _seed_event(db, tenant, appointment_type="Retorno", days_ago=5)

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert body["bookings_total"] == 3
    assert body["bookings_last_30d"] == 2  # the 40-day-old one is excluded
    assert body["by_type"] == {"Primeira consulta": 2, "Retorno": 1}


async def test_summary_scoped_to_tenant(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        analytics_api,
        "get_entitlements",
        _entitled_fake(addons={**_ALL_ADDONS_OFF, "analytics_bi": True}),
    )
    other_tenant = Tenant(id=uuid4(), clinic_name="Other", phone_number_id=str(uuid4())[:12])
    async with db() as session:
        session.add(other_tenant)
        await session.commit()
    await _seed_event(db, other_tenant, appointment_type="Should not count")

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    assert response.json()["bookings_total"] == 0
