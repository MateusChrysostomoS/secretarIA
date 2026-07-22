"""Tests for POST /internal/tenants/{tenant_id}/asaas-connection.

Mirrors tests/test_internal_provisioning.py's whatsapp-connection coverage
(auth guard, 404, encrypted-at-rest round trip, no echo) for the new Asaas
credential-ingestion endpoint (services/tenant_config.py's
get/set_asaas_api_key / get/set_asaas_webhook_token pair).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

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

from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.models.tenant_credentials import TenantCredentials  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    get_asaas_api_key,
    get_asaas_webhook_token,
    has_asaas_api_key,
)

GOOD_KEY = "test-internal-key"
HEADERS = {"X-Internal-Api-Key": GOOD_KEY}
VALID_BODY = {"api_key": "asaas-key-1234567890", "webhook_token": "whsec-token-1234567890"}


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
def _override_session(db):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _fake_get_session
    yield
    app.dependency_overrides.pop(get_session, None)


async def _seed_tenant(db, **overrides) -> Tenant:
    fields = dict(id=uuid4(), clinic_name="Clinic", phone_number_id=None, is_active=False)
    fields.update(overrides)
    async with db() as session:
        tenant = Tenant(**fields)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


# --------------------------------------------------------------------------
# Auth guard — mirrors test_internal_provisioning.py
# --------------------------------------------------------------------------


async def test_missing_key_is_unauthorized(client: AsyncClient) -> None:
    response = await client.post(
        f"/internal/tenants/{uuid4()}/asaas-connection", json=VALID_BODY
    )
    assert response.status_code == 401


async def test_wrong_key_is_unauthorized(client: AsyncClient) -> None:
    response = await client.post(
        f"/internal/tenants/{uuid4()}/asaas-connection",
        json=VALID_BODY,
        headers={"X-Internal-Api-Key": "wrong-key"},
    )
    assert response.status_code == 401


async def test_unconfigured_key_is_forbidden(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.config import get_settings

    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = await client.post(
            f"/internal/tenants/{uuid4()}/asaas-connection",
            json=VALID_BODY,
            headers={"X-Internal-Api-Key": "anything"},
        )
        assert response.status_code == 403
    finally:
        monkeypatch.setenv("INTERNAL_API_KEY", GOOD_KEY)
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# 404 unknown tenant
# --------------------------------------------------------------------------


async def test_unknown_tenant_is_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/internal/tenants/{uuid4()}/asaas-connection", json=VALID_BODY, headers=HEADERS
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# Validation: min_length=10 on both fields
# --------------------------------------------------------------------------


async def test_short_api_key_is_422(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection",
        json={"api_key": "short", "webhook_token": "whsec-token-1234567890"},
        headers=HEADERS,
    )
    assert response.status_code == 422


async def test_short_webhook_token_is_422(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection",
        json={"api_key": "asaas-key-1234567890", "webhook_token": "short"},
        headers=HEADERS,
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Happy path: stores both encrypted, never echoes, round-trips
# --------------------------------------------------------------------------


async def test_stores_both_encrypted_never_echoes_and_round_trips(
    client: AsyncClient, db
) -> None:
    tenant = await _seed_tenant(db)
    api_key = "asaas-super-secret-key-999"
    webhook_token = "whsec-super-secret-token-888"

    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection",
        json={"api_key": api_key, "webhook_token": webhook_token},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "asaas_connected": True}
    assert api_key not in response.text
    assert webhook_token not in response.text

    async with db() as session:
        cred = await session.scalar(
            select(TenantCredentials).where(TenantCredentials.tenant_id == tenant.id)
        )
        assert cred is not None
        # What PERSISTS is Fernet ciphertext, never the plaintext.
        assert cred.asaas_api_key_encrypted != api_key
        assert api_key not in cred.asaas_api_key_encrypted
        assert cred.asaas_webhook_token_encrypted != webhook_token
        assert webhook_token not in cred.asaas_webhook_token_encrypted

        # The single decrypt seam returns the original values.
        assert await has_asaas_api_key(session, tenant.id) is True
        assert await get_asaas_api_key(session, tenant.id) == api_key
        assert await get_asaas_webhook_token(session, tenant.id) == webhook_token


async def test_reconnect_upserts_same_credentials_row(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)
    await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection",
        json={"api_key": "asaas-key-first-111", "webhook_token": "whsec-first-111111"},
        headers=HEADERS,
    )
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection",
        json={"api_key": "asaas-key-second-222", "webhook_token": "whsec-second-222222"},
        headers=HEADERS,
    )
    assert response.status_code == 200

    async with db() as session:
        rows = (
            await session.scalars(
                select(TenantCredentials).where(TenantCredentials.tenant_id == tenant.id)
            )
        ).all()
        assert len(rows) == 1  # upsert, not a second row
        assert await get_asaas_api_key(session, tenant.id) == "asaas-key-second-222"
        assert await get_asaas_webhook_token(session, tenant.id) == "whsec-second-222222"
