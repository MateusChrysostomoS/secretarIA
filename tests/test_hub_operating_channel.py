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
from tests.before_support import recorded_logs
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


# ---------------------------------------------------------------------------
# PATCH /tenants/me/operating-channel - manager only, 409 until WhatsApp is released
# ---------------------------------------------------------------------------

PATCH = "/tenants/me/operating-channel"
UNAVAILABLE = {
    "code": "operating_channel_unavailable",
    "message": "Esta opção ainda não está disponível.",
}


@pytest.fixture
def released_whatsapp(monkeypatch):
    from secretaria.services import operating_channel as service

    monkeypatch.setattr(service, "RELEASED_OPERATING_CHANNELS", ("brain_message", "whatsapp"))


async def test_requires_authentication(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    response = await client.patch(PATCH, json={"operating_channel": "brain_message"})
    assert response.status_code == 401
    assert await _stored(db, tenant.id) == "brain_message"


@pytest.mark.parametrize(
    "claims",
    [
        {"role": "secretary"},
        {"role": "doctor"},
        {"role": "secretary", "is_manager": True},
    ],
)
async def test_secretary_and_plain_doctor_are_refused(
    client: AsyncClient, db, released_whatsapp, claims
):
    tenant = await _seed(db, "A")
    response = await client.patch(
        PATCH, json={"operating_channel": "whatsapp"}, headers=_auth(tenant, **claims)
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden_role"
    assert await _stored(db, tenant.id) == "brain_message"


async def test_token_without_role_is_refused(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    response = await client.patch(
        PATCH, json={"operating_channel": "brain_message"}, headers=_auth(tenant, role=None)
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "role_claim_missing"


@pytest.mark.parametrize(
    "claims",
    [
        {"role": "manager"},
        {"role": "doctor", "is_owner": True},
        {"role": "doctor", "is_manager": True},
    ],
)
async def test_manager_and_owner_or_manager_doctor_may_change(client: AsyncClient, db, claims):
    tenant = await _seed(db, "A")
    response = await client.patch(
        PATCH, json={"operating_channel": "brain_message"}, headers=_auth(tenant, **claims)
    )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "operating_channel": "brain_message",
        "available_operating_channels": ["brain_message"],
    }


async def test_whatsapp_is_refused_while_not_released(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    response = await client.patch(
        PATCH, json={"operating_channel": "whatsapp"}, headers=_auth(tenant, role="manager")
    )
    assert response.status_code == 409
    assert response.json()["detail"] == UNAVAILABLE
    assert await _stored(db, tenant.id) == "brain_message"


@pytest.mark.parametrize(
    "body",
    [
        {"operating_channel": "sms"},
        {"operating_channel": "WhatsApp"},
        {"operating_channel": None},
        {},
        {"operating_channel": "brain_message", "extra": 1},
    ],
)
async def test_unknown_missing_or_extra_keys_are_422(client: AsyncClient, db, body):
    tenant = await _seed(db, "A")
    response = await client.patch(PATCH, json=body, headers=_auth(tenant, role="manager"))
    assert response.status_code == 422
    assert await _stored(db, tenant.id) == "brain_message"


async def test_released_whatsapp_is_stored_and_read_back(
    client: AsyncClient, db, released_whatsapp
):
    tenant = await _seed(db, "A")
    with recorded_logs() as logs:
        response = await client.patch(
            PATCH, json={"operating_channel": "whatsapp"}, headers=_auth(tenant, role="manager")
        )
    assert response.status_code == 200, response.text
    assert response.json() == {
        "operating_channel": "whatsapp",
        "available_operating_channels": ["brain_message", "whatsapp"],
    }
    assert await _stored(db, tenant.id) == "whatsapp"
    read = await client.get(CONFIG, headers=_auth(tenant))
    assert read.json()["operating_channel"] == "whatsapp"
    changed = [entry for entry in logs if entry["event"] == "operating_channel_changed"]
    assert changed == [
        {
            "event": "operating_channel_changed",
            "log_level": "info",
            "tenant_id": str(tenant.id),
            "previous": "brain_message",
            "channel": "whatsapp",
        }
    ]


async def test_restating_the_channel_logs_nothing(client: AsyncClient, db):
    tenant = await _seed(db, "A")
    with recorded_logs() as logs:
        response = await client.patch(
            PATCH,
            json={"operating_channel": "brain_message"},
            headers=_auth(tenant, role="manager"),
        )
    assert response.status_code == 200
    assert not [entry for entry in logs if entry["event"] == "operating_channel_changed"]


async def test_only_the_callers_clinic_changes(client: AsyncClient, db, released_whatsapp):
    clinic_a = await _seed(db, "A")
    clinic_b = await _seed(db, "B")
    response = await client.patch(
        PATCH, json={"operating_channel": "whatsapp"}, headers=_auth(clinic_a, role="manager")
    )
    assert response.status_code == 200
    assert await _stored(db, clinic_a.id) == "whatsapp"
    assert await _stored(db, clinic_b.id) == "brain_message"


async def test_config_puts_never_change_the_channel(client: AsyncClient, db, released_whatsapp):
    tenant = await _seed(db, "A")
    headers = _auth(tenant, role="manager")
    legacy = await client.put(
        CONFIG, json={"clinic_description": "x", "operating_channel": "whatsapp"}, headers=headers
    )
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["operating_channel"] == "brain_message"
    aggregate = await client.put(
        "/tenants/me/configuration",
        json={"tenant": {"clinic_description": "y", "operating_channel": "whatsapp"}},
        headers=headers,
    )
    assert aggregate.status_code == 200, aggregate.text
    assert await _stored(db, tenant.id) == "brain_message"
