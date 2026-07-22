"""Tests for api/hub/units.py — multi_unit CRUD + gating. Mirrors
test_hub_professionals.py exactly, against the `multi_unit` addon and
`limits["units"]`.
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
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub import units as units_api  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Tenant, Unit  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}

ENDPOINT = "/tenants/me/units"


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


async def _never_called_fake(tenant_id, redis=None):
    raise AssertionError("get_entitlements must not be called for this operation")


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


async def _seed_unit(db, tenant: Tenant, **overrides) -> Unit:
    fields = dict(tenant_id=tenant.id, name="Unidade Centro", address="Rua A, 1", is_active=True)
    fields.update(overrides)
    async with db() as session:
        unit = Unit(**fields)
        session.add(unit)
        await session.commit()
        await session.refresh(unit)
        return unit


# --------------------------------------------------------------------------
# GET — always allowed, no entitlement check
# --------------------------------------------------------------------------


async def test_list_is_allowed_without_entitlement_check(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(units_api, "get_entitlements", _never_called_fake)
    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    assert response.json() == []


async def test_list_shape_is_whitelisted(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(units_api, "get_entitlements", _never_called_fake)
    await _seed_unit(db, tenant, name="Unidade Centro", address="Rua A, 1")

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    row = response.json()[0]
    assert set(row.keys()) == {"id", "name", "address", "is_active", "created_at"}
    assert row["name"] == "Unidade Centro"
    assert row["address"] == "Rua A, 1"


# --------------------------------------------------------------------------
# POST — entitlement + limit gating
# --------------------------------------------------------------------------


async def test_create_active_unit_succeeds_when_entitled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        units_api,
        "get_entitlements",
        _entitled_fake(addons={**_ALL_ADDONS_OFF, "multi_unit": True}, limits={"units": 5}),
    )
    response = await client.post(ENDPOINT, json={"name": "Unidade Centro"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Unidade Centro"
    assert body["is_active"] is True


async def test_create_not_entitled_returns_403(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(units_api, "get_entitlements", _entitled_fake())  # addon off
    response = await client.post(ENDPOINT, json={"name": "Unidade Centro"})
    assert response.status_code == 403
    assert response.json()["detail"] == "multi_unit_not_entitled"


async def test_create_over_limit_returns_409(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_unit(db, tenant, name="Existing", is_active=True)
    monkeypatch.setattr(
        units_api,
        "get_entitlements",
        _entitled_fake(addons={**_ALL_ADDONS_OFF, "multi_unit": True}, limits={"units": 1}),
    )
    response = await client.post(ENDPOINT, json={"name": "Unidade Centro"})
    assert response.status_code == 409
    assert response.json()["detail"] == "unit_limit_reached"


async def test_create_entitlement_fetch_failure_fails_closed_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_none(tenant_id, redis=None):
        return None

    monkeypatch.setattr(units_api, "get_entitlements", _fake_none)
    response = await client.post(ENDPOINT, json={"name": "Unidade Centro"})
    assert response.status_code == 503


async def test_create_inactive_unit_skips_entitlement_check(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(units_api, "get_entitlements", _never_called_fake)
    response = await client.post(ENDPOINT, json={"name": "Unidade Centro", "is_active": False})
    assert response.status_code == 201
    assert response.json()["is_active"] is False


# --------------------------------------------------------------------------
# PATCH — activation is gated, rename/deactivate are not
# --------------------------------------------------------------------------


async def test_patch_rename_without_activation_skips_entitlement_check(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = await _seed_unit(db, tenant, name="Old Name", is_active=True)
    monkeypatch.setattr(units_api, "get_entitlements", _never_called_fake)

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"name": "New Name"})
    assert response.status_code == 200
    assert response.json()["name"] == "New Name"


async def test_patch_deactivate_skips_entitlement_check(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = await _seed_unit(db, tenant, is_active=True)
    monkeypatch.setattr(units_api, "get_entitlements", _never_called_fake)

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_patch_activate_not_entitled_returns_403(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = await _seed_unit(db, tenant, is_active=False)
    monkeypatch.setattr(units_api, "get_entitlements", _entitled_fake())  # addon off

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"is_active": True})
    assert response.status_code == 403
    assert response.json()["detail"] == "multi_unit_not_entitled"


async def test_patch_activate_over_limit_returns_409(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_unit(db, tenant, name="Existing active", is_active=True)
    unit = await _seed_unit(db, tenant, name="To activate", is_active=False)
    monkeypatch.setattr(
        units_api,
        "get_entitlements",
        _entitled_fake(addons={**_ALL_ADDONS_OFF, "multi_unit": True}, limits={"units": 1}),
    )

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"is_active": True})
    assert response.status_code == 409
    assert response.json()["detail"] == "unit_limit_reached"


async def test_patch_activate_within_limit_succeeds(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    unit = await _seed_unit(db, tenant, is_active=False)
    monkeypatch.setattr(
        units_api,
        "get_entitlements",
        _entitled_fake(addons={**_ALL_ADDONS_OFF, "multi_unit": True}, limits={"units": 5}),
    )

    response = await client.patch(f"{ENDPOINT}/{unit.id}", json={"is_active": True})
    assert response.status_code == 200
    assert response.json()["is_active"] is True


async def test_patch_unknown_id_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(units_api, "get_entitlements", _never_called_fake)
    response = await client.patch(f"{ENDPOINT}/{uuid4()}", json={"name": "Ghost"})
    assert response.status_code == 404
