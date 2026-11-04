"""GET/PATCH /internal/tenants/{tenant_id}/contact-email (brain-api -> secretarIA)."""

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
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Tenant  # noqa: E402

HEADERS = {"X-Internal-Api-Key": "test-internal-key"}


def _path(tenant_id) -> str:
    return f"/internal/tenants/{tenant_id}/contact-email"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
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


async def _seed(db, contact_email: str | None = None) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(), clinic_name="Clinic", phone_number_id=None, contact_email=contact_email
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _stored(db, tenant_id) -> str | None:
    async with db() as session:
        return (await session.get(Tenant, tenant_id)).contact_email


@pytest.mark.parametrize("method", ["get", "patch"])
async def test_missing_key_is_unauthorized(client: AsyncClient, method):
    kwargs = {"json": {"contact_email": "a@b.com"}} if method == "patch" else {}
    response = await getattr(client, method)(_path(uuid4()), **kwargs)
    assert response.status_code == 401


@pytest.mark.parametrize("method", ["get", "patch"])
async def test_wrong_key_is_unauthorized(client: AsyncClient, method):
    kwargs = {"json": {"contact_email": "a@b.com"}} if method == "patch" else {}
    response = await getattr(client, method)(
        _path(uuid4()), headers={"X-Internal-Api-Key": "wrong"}, **kwargs
    )
    assert response.status_code == 401


async def test_unconfigured_key_is_forbidden(client: AsyncClient, monkeypatch):
    from secretaria.config import get_settings

    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = await client.get(_path(uuid4()), headers={"X-Internal-Api-Key": "anything"})
        assert response.status_code == 403
    finally:
        monkeypatch.setenv("INTERNAL_API_KEY", "test-internal-key")
        get_settings.cache_clear()


async def test_get_returns_the_stored_value(client: AsyncClient, db):
    tenant = await _seed(db, "clinica@example.com")
    response = await client.get(_path(tenant.id), headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json() == {"contact_email": "clinica@example.com"}


async def test_get_returns_null_when_unset(client: AsyncClient, db):
    tenant = await _seed(db)
    response = await client.get(_path(tenant.id), headers=HEADERS)
    assert response.json() == {"contact_email": None}


async def test_get_unknown_tenant_is_404(client: AsyncClient):
    response = await client.get(_path(uuid4()), headers=HEADERS)
    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown tenant"


async def test_patch_sets_and_normalises(client: AsyncClient, db):
    tenant = await _seed(db)
    response = await client.patch(
        _path(tenant.id), headers=HEADERS, json={"contact_email": " Foo@Bar.COM "}
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"contact_email": "foo@bar.com"}
    assert await _stored(db, tenant.id) == "foo@bar.com"


@pytest.mark.parametrize("cleared", [None, ""])
async def test_patch_clears(client: AsyncClient, db, cleared):
    tenant = await _seed(db, "old@example.com")
    response = await client.patch(
        _path(tenant.id), headers=HEADERS, json={"contact_email": cleared}
    )
    assert response.status_code == 200
    assert response.json() == {"contact_email": None}
    assert await _stored(db, tenant.id) is None


async def test_patch_invalid_is_422(client: AsyncClient, db):
    tenant = await _seed(db, "old@example.com")
    response = await client.patch(
        _path(tenant.id), headers=HEADERS, json={"contact_email": "nope"}
    )
    assert response.status_code == 422
    assert await _stored(db, tenant.id) == "old@example.com"


async def test_patch_missing_key_is_422(client: AsyncClient, db):
    tenant = await _seed(db, "old@example.com")
    response = await client.patch(_path(tenant.id), headers=HEADERS, json={})
    assert response.status_code == 422
    assert await _stored(db, tenant.id) == "old@example.com"


async def test_patch_unknown_tenant_is_404(client: AsyncClient):
    response = await client.patch(
        _path(uuid4()), headers=HEADERS, json={"contact_email": "a@b.com"}
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "Unknown tenant"


async def test_patch_only_touches_the_addressed_tenant(client: AsyncClient, db):
    a = await _seed(db, "a@example.com")
    b = await _seed(db, "b@example.com")
    await client.patch(_path(a.id), headers=HEADERS, json={"contact_email": "new@example.com"})
    assert await _stored(db, a.id) == "new@example.com"
    assert await _stored(db, b.id) == "b@example.com"
