"""PATCH /tenants/me/contact-email (hub) and the read-back on GET /tenants/me/config.

The hub auth is the REAL `get_current_tenant` over a faked brain-api introspection
(tests/hub_auth.py); the database is an in-memory sqlite.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.core.database import Base, get_session
from secretaria.models import Tenant
from tests.before_support import recorded_logs
from tests.hub_auth import install_fake_brain_api, make_hub_token, reset_fake_brain_api

PATCH = "/tenants/me/contact-email"
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
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed(db, name: str, contact_email: str | None = None) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(), clinic_name=name, phone_number_id=None, contact_email=contact_email
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _stored(db, tenant_id) -> str | None:
    async with db() as session:
        return (await session.get(Tenant, tenant_id)).contact_email


@pytest.fixture(autouse=True)
def _wiring(db, monkeypatch):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _fake_get_session
    install_fake_brain_api(monkeypatch)
    yield
    app.dependency_overrides.pop(get_session, None)
    reset_fake_brain_api()


def _auth(tenant: Tenant, **kwargs) -> dict:
    return {"Authorization": f"Bearer {make_hub_token(tenant_id=tenant.id, **kwargs)}"}


async def test_requires_authentication(client: AsyncClient, db):
    await _seed(db, "A")
    response = await client.patch(PATCH, json={"contact_email": "a@b.com"})
    assert response.status_code == 401


async def test_inactive_token_is_unauthorized(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    token = make_hub_token(tenant_id=tenant.id, active=False)
    response = await client.patch(
        PATCH, json={"contact_email": "a@b.com"}, headers={"Authorization": f"Bearer {token}"}
    )
    assert response.status_code == 401


async def test_sets_the_email_and_echoes_it(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    response = await client.patch(
        PATCH, json={"contact_email": "clinica@example.com"}, headers=_auth(tenant)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"contact_email": "clinica@example.com"}
    assert await _stored(db, tenant.id) == "clinica@example.com"


async def test_normalises_before_storing(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    response = await client.patch(
        PATCH, json={"contact_email": " Foo@Bar.COM "}, headers=_auth(tenant)
    )
    assert response.json() == {"contact_email": "foo@bar.com"}
    assert await _stored(db, tenant.id) == "foo@bar.com"


@pytest.mark.parametrize("cleared", [None, "", "   "])
async def test_clears_with_null_or_blank(client: AsyncClient, db, cleared):
    tenant = await _seed(db, "A", contact_email="old@example.com")
    response = await client.patch(PATCH, json={"contact_email": cleared}, headers=_auth(tenant))
    assert response.status_code == 200, response.text
    assert response.json() == {"contact_email": None}
    assert await _stored(db, tenant.id) is None


async def test_invalid_email_is_422_and_changes_nothing(client: AsyncClient, db):
    tenant = await _seed(db, "A", contact_email="old@example.com")
    response = await client.patch(
        PATCH, json={"contact_email": "not-an-email"}, headers=_auth(tenant)
    )
    assert response.status_code == 422
    assert await _stored(db, tenant.id) == "old@example.com"


async def test_missing_key_is_422_and_never_clears(client: AsyncClient, db):
    tenant = await _seed(db, "A", contact_email="old@example.com")
    response = await client.patch(PATCH, json={}, headers=_auth(tenant))
    assert response.status_code == 422
    assert await _stored(db, tenant.id) == "old@example.com"


async def test_any_role_of_the_tenant_may_write(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    for role in ("doctor", "secretary", "owner"):
        response = await client.patch(
            PATCH, json={"contact_email": f"{role}@example.com"}, headers=_auth(tenant, role=role)
        )
        assert response.status_code == 200, (role, response.text)


async def test_a_token_of_tenant_a_cannot_change_tenant_b(client: AsyncClient, db):
    a = await _seed(db, "A", contact_email="a@example.com")
    b = await _seed(db, "B", contact_email="b@example.com")
    response = await client.patch(
        PATCH, json={"contact_email": "new@example.com"}, headers=_auth(a)
    )
    assert response.status_code == 200
    assert await _stored(db, a.id) == "new@example.com"
    assert await _stored(db, b.id) == "b@example.com"


async def test_the_email_is_never_logged(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    with recorded_logs() as logs:
        await client.patch(
            PATCH, json={"contact_email": "private.person@example.com"}, headers=_auth(tenant)
        )
        await client.patch(PATCH, json={"contact_email": None}, headers=_auth(tenant))
    assert "private.person" not in repr(logs)
    events = [e for e in logs if e["event"] == "contact_email_updated"]
    assert [e["cleared"] for e in events] == [False, True]
    assert all(e["tenant_id"] == str(tenant.id) for e in events)


async def test_get_config_returns_the_value(client: AsyncClient, db):
    tenant = await _seed(db, "A", contact_email="clinica@example.com")
    response = await client.get(CONFIG, headers=_auth(tenant))
    assert response.status_code == 200, response.text
    assert response.json()["contact_email"] == "clinica@example.com"


async def test_get_config_returns_null_when_unset(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    response = await client.get(CONFIG, headers=_auth(tenant))
    assert response.json()["contact_email"] is None


async def test_legacy_put_config_does_not_touch_it(client: AsyncClient, db):
    tenant = await _seed(db, "A", contact_email="keep@example.com")
    response = await client.put(
        CONFIG,
        json={"persona_notes": "Renamed", "contact_email": "clobber@example.com"},
        headers=_auth(tenant),
    )
    assert response.status_code == 200, response.text
    assert response.json()["persona_notes"] == "Renamed"
    assert response.json()["contact_email"] == "keep@example.com"
    assert await _stored(db, tenant.id) == "keep@example.com"


async def test_aggregate_configuration_put_does_not_touch_it(client: AsyncClient, db):
    tenant = await _seed(db, "A", contact_email="keep@example.com")
    response = await client.put(
        "/tenants/me/configuration",
        json={"tenant": {"persona_notes": "Renamed", "contact_email": "clobber@example.com"}},
        headers=_auth(tenant),
    )
    assert response.status_code == 200, response.text
    assert await _stored(db, tenant.id) == "keep@example.com"
