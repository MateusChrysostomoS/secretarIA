"""Tests for api/hub/professionals.py — multi_professional CRUD + gating.

Uses the `client` fixture from conftest.py (ASGITransport, no real DB) with
`get_session` overridden to a real in-memory sqlite DB and `get_current_tenant`
overridden to a canned Tenant — the same override-the-FastAPI-dependency
pattern as test_internal.py, but with a REAL session (not a stub) since these
endpoints actually write rows. `get_entitlements` is monkeypatched at the
point professionals.py imported it, so no brain-api call is ever made.
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

from secretaria.api.hub import professionals as professionals_api  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
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

ENDPOINT = "/tenants/me/professionals"


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


async def _seed_professional(db, tenant: Tenant, **overrides) -> Professional:
    fields = dict(tenant_id=tenant.id, name="Dra. Ana", google_calendar_id=None, is_active=True)
    fields.update(overrides)
    async with db() as session:
        prof = Professional(**fields)
        session.add(prof)
        await session.commit()
        await session.refresh(prof)
        return prof


# --------------------------------------------------------------------------
# GET — always allowed, no entitlement check
# --------------------------------------------------------------------------


async def test_list_is_allowed_without_entitlement_check(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    assert response.json() == []


async def test_list_shape_is_whitelisted(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    await _seed_professional(db, tenant, name="Dra. Ana", google_calendar_id="ana-cal")

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    row = response.json()[0]
    assert set(row.keys()) == {
        "id",
        "name",
        "google_calendar_id",
        "is_active",
        "created_at",
        "specialty",
        "about",
        "context_doctor_message",
        "business_hours",
        "appointment_types",
        "has_calendar",
        "has_hours",
        "has_services",
        "complete",
    }
    assert row["name"] == "Dra. Ana"
    assert row["google_calendar_id"] == "ana-cal"


async def test_list_includes_onboarding_completeness(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    await _seed_professional(db, tenant, name="Bare", google_calendar_id=None)

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    row = response.json()[0]
    # No calendar/hours/services configured anywhere -> incomplete on every axis.
    assert row["has_calendar"] is False
    assert row["has_hours"] is False
    assert row["has_services"] is False
    assert row["complete"] is False


async def test_list_completeness_reflects_config(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.services.tenant_config import set_google_refresh_token

    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    await _seed_professional(
        db,
        tenant,
        name="Configured",
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
    )
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "tenant-refresh-token")
        await session.commit()

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    row = response.json()[0]
    assert row["has_calendar"] is True  # tenant-level token covers it
    assert row["has_hours"] is True
    assert row["has_services"] is True
    assert row["complete"] is True


# --------------------------------------------------------------------------
# POST — entitlement + limit gating
# --------------------------------------------------------------------------


async def test_create_active_professional_succeeds_when_entitled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        professionals_api,
        "get_entitlements",
        _entitled_fake(
            addons={**_ALL_ADDONS_OFF, "multi_professional": True}, limits={"professionals": 5}
        ),
    )
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Dra. Ana"
    assert body["is_active"] is True


async def test_create_not_entitled_returns_403(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _entitled_fake())  # addon off
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana"})
    assert response.status_code == 403
    assert response.json()["detail"] == "multi_professional_not_entitled"


async def test_create_over_limit_returns_409(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_professional(db, tenant, name="Existing", is_active=True)
    monkeypatch.setattr(
        professionals_api,
        "get_entitlements",
        _entitled_fake(
            addons={**_ALL_ADDONS_OFF, "multi_professional": True}, limits={"professionals": 1}
        ),
    )
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana"})
    assert response.status_code == 409
    assert response.json()["detail"] == "professional_limit_reached"


async def test_create_entitlement_fetch_failure_fails_closed_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_none(tenant_id, redis=None):
        return None

    monkeypatch.setattr(professionals_api, "get_entitlements", _fake_none)
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana"})
    assert response.status_code == 503


async def test_create_inactive_professional_skips_entitlement_check(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana", "is_active": False})
    assert response.status_code == 201
    assert response.json()["is_active"] is False


# --------------------------------------------------------------------------
# PATCH — activation is gated, rename/deactivate are not
# --------------------------------------------------------------------------


async def test_patch_rename_without_activation_skips_entitlement_check(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, name="Old Name", is_active=True)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"name": "New Name"})
    assert response.status_code == 200
    assert response.json()["name"] == "New Name"


async def test_patch_deactivate_skips_entitlement_check(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, is_active=True)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_patch_activate_not_entitled_returns_403(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, is_active=False)
    monkeypatch.setattr(professionals_api, "get_entitlements", _entitled_fake())  # addon off

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"is_active": True})
    assert response.status_code == 403
    assert response.json()["detail"] == "multi_professional_not_entitled"


async def test_patch_activate_over_limit_returns_409(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_professional(db, tenant, name="Existing active", is_active=True)
    prof = await _seed_professional(db, tenant, name="To activate", is_active=False)
    monkeypatch.setattr(
        professionals_api,
        "get_entitlements",
        _entitled_fake(
            addons={**_ALL_ADDONS_OFF, "multi_professional": True}, limits={"professionals": 1}
        ),
    )

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"is_active": True})
    assert response.status_code == 409
    assert response.json()["detail"] == "professional_limit_reached"


async def test_patch_activate_within_limit_succeeds(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, is_active=False)
    monkeypatch.setattr(
        professionals_api,
        "get_entitlements",
        _entitled_fake(
            addons={**_ALL_ADDONS_OFF, "multi_professional": True}, limits={"professionals": 5}
        ),
    )

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"is_active": True})
    assert response.status_code == 200
    assert response.json()["is_active"] is True


async def test_patch_unknown_id_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    response = await client.patch(f"{ENDPOINT}/{uuid4()}", json={"name": "Ghost"})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# PUT /{professional_id}/config — NEVER entitlement/limit gated
# --------------------------------------------------------------------------


async def test_put_config_updates_fields_and_is_never_entitlement_gated(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.put(
        f"{ENDPOINT}/{prof.id}/config",
        json={
            "business_hours": {"monday": [{"start": "08:00", "end": "12:00"}]},
            "appointment_types": [
                {"name": "Consulta", "duration_min": 30, "is_active": True}
            ],
            "specialty": "Cardiologia",
            "about": "Atende há 10 anos.",
            "context_doctor_message": "Prefere retornos rápidos.",
            "google_calendar_id": "ana-own-cal",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["specialty"] == "Cardiologia"
    assert body["about"] == "Atende há 10 anos."
    assert body["context_doctor_message"] == "Prefere retornos rápidos."
    assert body["google_calendar_id"] == "ana-own-cal"
    assert body["business_hours"] == {"monday": [{"start": "08:00", "end": "12:00"}]}
    assert body["has_hours"] is True
    assert body["has_services"] is True


async def test_put_config_partial_update_leaves_other_fields_untouched(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, name="Original Name")
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.put(f"{ENDPOINT}/{prof.id}/config", json={"specialty": "Pediatria"})
    assert response.status_code == 200
    body = response.json()
    assert body["specialty"] == "Pediatria"
    assert body["name"] == "Original Name"  # PUT /config never touches `name`


async def test_put_config_rejects_overlapping_hours(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.put(
        f"{ENDPOINT}/{prof.id}/config",
        json={
            "business_hours": {
                "monday": [
                    {"start": "08:00", "end": "12:00"},
                    {"start": "11:00", "end": "14:00"},
                ]
            }
        },
    )
    assert response.status_code == 422


async def test_put_config_unknown_id_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    response = await client.put(f"{ENDPOINT}/{uuid4()}/config", json={"specialty": "Ghost"})
    assert response.status_code == 404
