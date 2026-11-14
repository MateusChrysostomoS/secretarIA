"""The clinic's operating channel on the hub (TASK-084): read in GET /tenants/me/config.

The hub auth is the REAL `get_current_tenant` over a faked brain-api introspection
(tests/hub_auth.py); the database is an in-memory sqlite. Same wiring as
tests/test_hub_contact_email.py.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.core.database import Base, get_session
from secretaria.models import Tenant
from tests.hub_auth import install_fake_brain_api, make_hub_token, reset_fake_brain_api

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


async def _seed(db, name: str, **fields) -> Tenant:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name=name, phone_number_id=None, **fields)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _stored(db, tenant_id) -> str:
    async with db() as session:
        return (await session.get(Tenant, tenant_id)).operating_channel


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


async def test_config_reports_the_portal_and_what_is_available(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    response = await client.get(CONFIG, headers=_auth(tenant))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["operating_channel"] == "brain_message"
    assert body["available_operating_channels"] == ["brain_message"]


async def test_config_reports_a_clinic_already_on_whatsapp(client: AsyncClient, db):
    # Only reachable through the database today; the read must still say so truthfully.
    tenant = await _seed(db, "A", operating_channel="whatsapp")
    response = await client.get(CONFIG, headers=_auth(tenant))
    assert response.json()["operating_channel"] == "whatsapp"
    assert response.json()["available_operating_channels"] == ["brain_message"]
