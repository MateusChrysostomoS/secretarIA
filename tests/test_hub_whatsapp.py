"""TASK-093 Plano 3, Task 3: /tenants/me/whatsapp/* on the hub (backend only, no Portal screen).

Behind WHATSAPP_CONNECT_BACKEND_ENABLED (off by default -> 404 on all three routes). Hub auth is
the REAL `get_current_tenant` over a faked brain-api introspection (tests/hub_auth.py), the
database an in-memory sqlite, and Meta an httpx.MockTransport - never the real Graph API.
"""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.config import get_settings
from secretaria.core.database import Base, get_session
from secretaria.models import Tenant, WhatsAppConnectionAttempt
from secretaria.services import brain_onboarding, whatsapp_connect
from secretaria.services.tenant_config import has_waba_token
from tests.hub_auth import install_fake_brain_api, make_hub_token, reset_fake_brain_api

BASE = "/tenants/me/whatsapp"
CONFIG = f"{BASE}/embedded-signup-config"
ATTEMPTS = f"{BASE}/attempts"
CONNECTION = f"{BASE}/connection"

TOKEN = "EAAG-hub-token-never-returned"
PHONE = "109876543210"
WABA = "998877665544"
BODY = {"attempt_id": "att-1", "code": "meta-code", "phone_number_id": PHONE, "waba_id": WABA}


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
def _wiring(db, monkeypatch):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _fake_get_session
    install_fake_brain_api(monkeypatch)

    # The notice to brain-api after a connection is Task 4's subject
    # (tests/test_whatsapp_connect_notify.py); here it simply succeeds.
    async def _brain_acknowledges(*args, **kwargs):
        return True

    monkeypatch.setattr(brain_onboarding, "notify_whatsapp_connected", _brain_acknowledges)
    yield
    app.dependency_overrides.pop(get_session, None)
    reset_fake_brain_api()
    get_settings.cache_clear()


class FakeMeta:
    def __init__(self):
        self.requests: list[httpx.Request] = []
        self.exchange_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(self.exchange_status, json={"access_token": TOKEN})
        return httpx.Response(200, json={"success": True})


@pytest.fixture
def meta(monkeypatch) -> FakeMeta:
    fake = FakeMeta()
    monkeypatch.setattr(whatsapp_connect, "_TRANSPORT", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("WHATSAPP_CONNECT_BACKEND_ENABLED", "true")
    monkeypatch.setenv("META_APP_ID", "app-123")
    monkeypatch.setenv("META_ES_CONFIG_ID", "cfg-456")
    monkeypatch.setenv("META_APP_SECRET", "app-secret-789")
    get_settings.cache_clear()


async def _seed(db, **fields) -> Tenant:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica", **fields)
        session.add(tenant)
        await session.commit()
        return tenant


def _auth(tenant: Tenant, **claims) -> dict:
    return {"Authorization": f"Bearer {make_hub_token(tenant_id=tenant.id, **claims)}"}


MANAGER = {"role": "manager"}


# --- flag off ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"), [("get", CONFIG), ("post", ATTEMPTS), ("get", CONNECTION)]
)
async def test_flag_off_answers_404_on_every_route(client: AsyncClient, db, meta, method, path):
    tenant = await _seed(db)
    kwargs = {"json": BODY} if method == "post" else {}
    response = await getattr(client, method)(path, headers=_auth(tenant, **MANAGER), **kwargs)
    assert response.status_code == 404
    unauthenticated = await getattr(client, method)(path, **kwargs)
    assert unauthenticated.status_code == 404
    assert meta.requests == []


# --- GET /embedded-signup-config -----------------------------------------------------------


async def test_config_gives_the_public_ids_to_the_manager(client: AsyncClient, db, enabled):
    tenant = await _seed(db)
    response = await client.get(CONFIG, headers=_auth(tenant, **MANAGER))
    assert response.status_code == 200, response.text
    assert response.json() == {"app_id": "app-123", "config_id": "cfg-456"}
    assert "app-secret-789" not in response.text


async def test_config_unset_ids_are_null(client: AsyncClient, db, enabled, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "")
    monkeypatch.setenv("META_ES_CONFIG_ID", "")
    get_settings.cache_clear()
    tenant = await _seed(db)
    response = await client.get(CONFIG, headers=_auth(tenant, **MANAGER))
    assert response.json() == {"app_id": None, "config_id": None}


async def test_config_is_refused_to_a_non_manager(client: AsyncClient, db, enabled):
    tenant = await _seed(db)
    response = await client.get(CONFIG, headers=_auth(tenant, role="secretary"))
    assert response.status_code == 403


# --- POST /attempts ------------------------------------------------------------------------


async def test_manager_connects(client: AsyncClient, db, enabled, meta):
    tenant = await _seed(db)
    response = await client.post(ATTEMPTS, json=BODY, headers=_auth(tenant, **MANAGER))
    assert response.status_code == 200, response.text
    assert response.json() == {"attempt_id": "att-1", "status": "connected", "failure_code": None}
    assert TOKEN not in response.text
    async with db() as session:
        stored = await session.get(Tenant, tenant.id)
        assert stored.phone_number_id == PHONE
        assert await has_waba_token(session, tenant.id)


@pytest.mark.parametrize(
    "claims", [{"role": "doctor"}, {"role": "secretary"}, {"role": "secretary", "is_manager": True}]
)
async def test_non_manager_cannot_connect(client: AsyncClient, db, enabled, meta, claims):
    tenant = await _seed(db)
    response = await client.post(ATTEMPTS, json=BODY, headers=_auth(tenant, **claims))
    assert response.status_code == 403
    assert meta.requests == []
    async with db() as session:
        assert (await session.get(Tenant, tenant.id)).phone_number_id is None
        assert (await session.scalar(select(WhatsAppConnectionAttempt))) is None


async def test_replaying_the_same_attempt_does_not_call_meta_again(
    client: AsyncClient, db, enabled, meta
):
    tenant = await _seed(db)
    first = await client.post(ATTEMPTS, json=BODY, headers=_auth(tenant, **MANAGER))
    calls = len(meta.requests)
    again = await client.post(ATTEMPTS, json=BODY, headers=_auth(tenant, **MANAGER))
    assert first.status_code == again.status_code == 200, again.text
    assert first.json() == again.json()
    assert len(meta.requests) == calls


async def test_number_of_another_clinic_is_a_409_and_the_attempt_is_recorded(
    client: AsyncClient, db, enabled, meta
):
    await _seed(db, phone_number_id=PHONE)
    tenant = await _seed(db)
    response = await client.post(ATTEMPTS, json=BODY, headers=_auth(tenant, **MANAGER))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "phone_number_conflict"
    async with db() as session:
        attempt = await session.scalar(
            select(WhatsAppConnectionAttempt).where(
                WhatsAppConnectionAttempt.tenant_id == tenant.id
            )
        )
        assert attempt.status == "failed"
        assert attempt.failure_code == "phone_number_conflict"
        assert await has_waba_token(session, tenant.id) is False


async def test_invalid_code_is_a_clear_error_and_nothing_is_stored(
    client: AsyncClient, db, enabled, meta
):
    meta.exchange_status = 400
    tenant = await _seed(db)
    response = await client.post(ATTEMPTS, json=BODY, headers=_auth(tenant, **MANAGER))
    assert response.status_code == 502
    detail = response.json()["detail"]
    assert detail["code"] == "code_exchange_failed"
    assert detail["message"]
    async with db() as session:
        assert (await session.get(Tenant, tenant.id)).phone_number_id is None
        assert await has_waba_token(session, tenant.id) is False
        attempt = await session.scalar(select(WhatsAppConnectionAttempt))
        assert attempt.status == "failed"


@pytest.mark.parametrize(
    "bad",
    [
        {k: v for k, v in BODY.items() if k != "code"},
        {**BODY, "attempt_id": ""},
        {**BODY, "attempt_id": "x" * 65},
        {**BODY, "phone_number_id": "12ab"},
        {**BODY, "waba_id": "../x"},
        {**BODY, "extra": 1},
    ],
)
async def test_malformed_body_is_a_422_without_meta(client: AsyncClient, db, enabled, meta, bad):
    tenant = await _seed(db)
    response = await client.post(ATTEMPTS, json=bad, headers=_auth(tenant, **MANAGER))
    assert response.status_code == 422
    assert meta.requests == []


# --- GET /connection -----------------------------------------------------------------------


async def test_connection_of_a_clinic_never_connected(client: AsyncClient, db, enabled):
    tenant = await _seed(db)
    response = await client.get(CONNECTION, headers=_auth(tenant, **MANAGER))
    assert response.status_code == 200, response.text
    assert response.json() == {
        "connected": False,
        "phone_number_id_masked": None,
        "connected_at": None,
        "channel_released": False,
    }


@pytest.mark.parametrize("claims", [MANAGER, {"role": "doctor"}, {"role": "secretary"}])
async def test_connection_is_masked_for_everyone_and_never_carries_the_token(
    client: AsyncClient, db, enabled, meta, claims
):
    tenant = await _seed(db)
    await client.post(ATTEMPTS, json=BODY, headers=_auth(tenant, **MANAGER))
    response = await client.get(CONNECTION, headers=_auth(tenant, **claims))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["connected"] is True
    assert body["phone_number_id_masked"] == "••••3210"
    assert datetime.fromisoformat(body["connected_at"]) <= datetime.now(UTC)
    assert PHONE not in response.text
    assert TOKEN not in response.text
    assert WABA not in response.text


async def test_channel_released_follows_the_release_switch(
    client: AsyncClient, db, enabled, monkeypatch
):
    from secretaria.services import operating_channel

    tenant = await _seed(db)
    monkeypatch.setattr(
        operating_channel, "RELEASED_OPERATING_CHANNELS", ("brain_message", "whatsapp")
    )
    response = await client.get(CONNECTION, headers=_auth(tenant, **MANAGER))
    assert response.json()["channel_released"] is True


async def test_a_number_taken_at_commit_time_is_a_409_and_keeps_nothing(
    client: AsyncClient, db, enabled, meta, monkeypatch
):
    # Another clinic committed the same number after every check passed: the partial unique
    # index refuses our write; the clinic gets the conflict, no token, the attempt recorded.
    from sqlalchemy.exc import IntegrityError

    from secretaria.services import provisioning

    real_connect = provisioning.connect_whatsapp

    async def connect_then_lose_the_race(session, **kwargs):
        await real_connect(session, **kwargs)
        raise IntegrityError("INSERT", {}, Exception("uq_tenants_phone_number_id_not_null"))

    monkeypatch.setattr(provisioning, "connect_whatsapp", connect_then_lose_the_race)
    tenant = await _seed(db)
    response = await client.post(ATTEMPTS, json=BODY, headers=_auth(tenant, **MANAGER))
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "phone_number_conflict"
    async with db() as session:
        assert (await session.get(Tenant, tenant.id)).phone_number_id is None
        assert await has_waba_token(session, tenant.id) is False
        attempt = await session.scalar(select(WhatsAppConnectionAttempt))
        assert (attempt.status, attempt.failure_code) == ("failed", "phone_number_conflict")
