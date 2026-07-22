"""Tests for the six Pix-deposit policy fields on GET/PUT /tenants/me/config
(api/hub/config.py), plus the derived, read-only `asaas_connected` flag.

Same db/tenant/_override pattern as test_hub_config.py.
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

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.services.tenant_config import set_asaas_api_key  # noqa: E402

CONFIG = "/tenants/me/config"


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
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=None)
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    # Same instance/session guarantee as api/hub/deps.get_current_tenant in
    # production — see test_hub_config.py's identical comment.
    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


# --------------------------------------------------------------------------
# GET — six fields with defaults + asaas_connected false -> true after set
# --------------------------------------------------------------------------


async def test_get_config_exposes_six_fields_with_defaults(client: AsyncClient) -> None:
    response = await client.get(CONFIG)
    assert response.status_code == 200
    body = response.json()
    assert body["pix_deposit_enabled"] is False
    assert body["pix_deposit_percent"] == 30
    assert body["pix_refund_window_hours"] == 24
    assert body["pix_retention_policy"] == "total"
    assert body["pix_partial_refund_percent"] == 50
    assert body["pix_reschedule_limit"] == 2
    assert body["asaas_connected"] is False


async def test_asaas_connected_flips_true_after_key_set(
    client: AsyncClient, db, tenant: Tenant
) -> None:
    async with db() as session:
        await set_asaas_api_key(session, tenant.id, "asaas-key-1234567890")
        await session.commit()

    response = await client.get(CONFIG)
    assert response.status_code == 200
    assert response.json()["asaas_connected"] is True


# --------------------------------------------------------------------------
# PUT — persists valid values, validates ranges, cannot flip asaas_connected
# --------------------------------------------------------------------------


async def test_put_persists_valid_values(client: AsyncClient) -> None:
    response = await client.put(
        CONFIG,
        json={
            "pix_deposit_enabled": True,
            "pix_deposit_percent": 40,
            "pix_refund_window_hours": 12,
            "pix_retention_policy": "partial",
            "pix_partial_refund_percent": 60,
            "pix_reschedule_limit": 1,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pix_deposit_enabled"] is True
    assert body["pix_deposit_percent"] == 40
    assert body["pix_refund_window_hours"] == 12
    assert body["pix_retention_policy"] == "partial"
    assert body["pix_partial_refund_percent"] == 60
    assert body["pix_reschedule_limit"] == 1


async def test_put_partial_update_leaves_other_pix_fields_untouched(
    client: AsyncClient,
) -> None:
    first = await client.put(CONFIG, json={"pix_deposit_percent": 45})
    assert first.status_code == 200
    assert first.json()["pix_deposit_percent"] == 45
    assert first.json()["pix_reschedule_limit"] == 2  # untouched default

    second = await client.put(CONFIG, json={"pix_reschedule_limit": 5})
    assert second.status_code == 200
    assert second.json()["pix_reschedule_limit"] == 5
    assert second.json()["pix_deposit_percent"] == 45  # preserved from the first PUT


@pytest.mark.parametrize(
    "field,value",
    [
        ("pix_deposit_percent", 0),
        ("pix_deposit_percent", 101),
        ("pix_partial_refund_percent", 0),
        ("pix_partial_refund_percent", 101),
        ("pix_refund_window_hours", -1),
        ("pix_reschedule_limit", -1),
    ],
)
async def test_put_rejects_out_of_range_values(
    client: AsyncClient, field: str, value: int
) -> None:
    response = await client.put(CONFIG, json={field: value})
    assert response.status_code == 422


async def test_put_rejects_bad_retention_policy(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={"pix_retention_policy": "half"})
    assert response.status_code == 422


async def test_put_accepts_boundary_values(client: AsyncClient) -> None:
    response = await client.put(
        CONFIG,
        json={
            "pix_deposit_percent": 1,
            "pix_partial_refund_percent": 100,
            "pix_refund_window_hours": 0,
            "pix_reschedule_limit": 0,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["pix_deposit_percent"] == 1
    assert body["pix_partial_refund_percent"] == 100
    assert body["pix_refund_window_hours"] == 0
    assert body["pix_reschedule_limit"] == 0


async def test_put_cannot_flip_asaas_connected(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={"asaas_connected": True})
    assert response.status_code == 200  # an undeclared field is silently ignored
    assert response.json()["asaas_connected"] is False  # still false — no key was ever set
