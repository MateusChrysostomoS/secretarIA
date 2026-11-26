"""TASK-093 Plano 3, Task 4 (secretarIA side): after connecting, tell brain-api.

brain-api still owns the onboarding state and the charge tied to it
(`POST /internal/tenants/{id}/whatsapp-connected` runs its "became connected" logic). The
secretarIA notifies it right after the connection; when brain-api is down the connection stays
recorded (`brain_notified_at` NULL) and the worker cron sends it again until a 200.
Brain and Meta are both faked - nothing leaves the process.
"""

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
from secretaria.workers import onboarding_cron
from tests.hub_auth import install_fake_brain_api, make_hub_token, reset_fake_brain_api

PHONE = "109876543210"
WABA = "998877665544"
BODY = {"attempt_id": "att-1", "code": "meta-code", "phone_number_id": PHONE, "waba_id": WABA}

_REAL_ASYNC_CLIENT = httpx.AsyncClient


# --- the client: brain_onboarding.notify_whatsapp_connected --------------------------------


@pytest.fixture
def brain(monkeypatch):
    """A fake brain-api behind the real client code (httpx.MockTransport)."""
    state = {"status": 200, "requests": [], "down": False}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append(request)
        if state["down"]:
            raise httpx.ConnectError("down", request=request)
        return httpx.Response(state["status"], json={"onboarding_state": "conectado"})

    def factory(**kwargs):
        return _REAL_ASYNC_CLIENT(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setenv("BRAIN_API_BASE_URL", "http://brain-api.internal")
    monkeypatch.setenv("INTERNAL_API_KEY", "pair-key")
    get_settings.cache_clear()
    monkeypatch.setattr(brain_onboarding.httpx, "AsyncClient", factory)
    yield state
    get_settings.cache_clear()


async def test_notify_posts_to_the_internal_route_with_the_pair_key(brain):
    tenant_id = uuid4()
    ok = await brain_onboarding.notify_whatsapp_connected(
        tenant_id, attempt_id="att-1", phone_number_id=PHONE, waba_id=WABA
    )
    assert ok is True
    (request,) = brain["requests"]
    assert request.method == "POST"
    assert request.url.path == f"/internal/tenants/{tenant_id}/whatsapp-connected"
    assert request.headers["X-Internal-Api-Key"] == "pair-key"
    assert request.read() == (
        b'{"attempt_id":"att-1","phone_number_id":"' + PHONE.encode() + b'",'
        b'"waba_id":"' + WABA.encode() + b'"}'
    )


@pytest.mark.parametrize("status", [404, 500, 401])
async def test_notify_non_200_is_false(brain, status):
    brain["status"] = status
    assert await brain_onboarding.notify_whatsapp_connected(
        uuid4(), attempt_id="a", phone_number_id=PHONE, waba_id=WABA
    ) is False


async def test_notify_brain_down_is_false_never_raises(brain):
    brain["down"] = True
    assert await brain_onboarding.notify_whatsapp_connected(
        uuid4(), attempt_id="a", phone_number_id=PHONE, waba_id=WABA
    ) is False


async def test_notify_unconfigured_is_false_without_network(brain, monkeypatch):
    monkeypatch.setenv("BRAIN_API_BASE_URL", "")
    get_settings.cache_clear()
    assert await brain_onboarding.notify_whatsapp_connected(
        uuid4(), attempt_id="a", phone_number_id=PHONE, waba_id=WABA
    ) is False
    assert brain["requests"] == []


# --- the hub route notifies right after the connection -------------------------------------


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


@pytest.fixture
def notified(monkeypatch) -> dict:
    """brain_onboarding.notify_whatsapp_connected replaced: records calls, scripted result."""
    state = {"ok": True, "calls": []}

    async def fake(tenant_id, *, attempt_id, phone_number_id, waba_id):
        state["calls"].append((tenant_id, attempt_id, phone_number_id, waba_id))
        return state["ok"]

    monkeypatch.setattr(brain_onboarding, "notify_whatsapp_connected", fake)
    return state


@pytest.fixture
def hub(db, monkeypatch):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _fake_get_session
    install_fake_brain_api(monkeypatch)

    def meta(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(200, json={"access_token": "EAAG-token"})
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(whatsapp_connect, "_TRANSPORT", httpx.MockTransport(meta))
    monkeypatch.setenv("WHATSAPP_CONNECT_BACKEND_ENABLED", "true")
    monkeypatch.setenv("META_APP_ID", "app-123")
    monkeypatch.setenv("META_APP_SECRET", "app-secret-789")
    get_settings.cache_clear()
    yield
    app.dependency_overrides.pop(get_session, None)
    reset_fake_brain_api()
    get_settings.cache_clear()


async def _seed(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clínica")
        session.add(tenant)
        await session.commit()
        return tenant


def _manager(tenant) -> dict:
    token = make_hub_token(tenant_id=tenant.id, role="manager")
    return {"Authorization": f"Bearer {token}"}


async def _attempt(db, tenant_id) -> WhatsAppConnectionAttempt:
    async with db() as session:
        return await session.scalar(
            select(WhatsAppConnectionAttempt).where(
                WhatsAppConnectionAttempt.tenant_id == tenant_id
            )
        )


async def test_connecting_notifies_brain_once_and_stamps_it(
    client: AsyncClient, db, hub, notified
):
    tenant = await _seed(db)
    response = await client.post(
        "/tenants/me/whatsapp/attempts", json=BODY, headers=_manager(tenant)
    )
    assert response.status_code == 200, response.text
    assert notified["calls"] == [(tenant.id, "att-1", PHONE, WABA)]
    assert (await _attempt(db, tenant.id)).brain_notified_at is not None


async def test_brain_down_keeps_the_connection_and_leaves_it_for_the_worker(
    client: AsyncClient, db, hub, notified
):
    notified["ok"] = False
    tenant = await _seed(db)
    response = await client.post(
        "/tenants/me/whatsapp/attempts", json=BODY, headers=_manager(tenant)
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "connected"
    attempt = await _attempt(db, tenant.id)
    assert attempt.status == "connected"
    assert attempt.brain_notified_at is None
    async with db() as session:
        assert (await session.get(Tenant, tenant.id)).phone_number_id == PHONE


async def test_an_unexpected_notice_error_never_undoes_the_connection(
    client: AsyncClient, db, hub, monkeypatch
):
    async def explode(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(brain_onboarding, "notify_whatsapp_connected", explode)
    tenant = await _seed(db)
    response = await client.post(
        "/tenants/me/whatsapp/attempts", json=BODY, headers=_manager(tenant)
    )
    assert response.status_code == 200, response.text
    attempt = await _attempt(db, tenant.id)
    assert (attempt.status, attempt.brain_notified_at) == ("connected", None)


async def test_a_failed_connection_never_notifies(client: AsyncClient, db, hub, notified):
    async with db() as session:
        session.add(Tenant(id=uuid4(), clinic_name="Outra", phone_number_id=PHONE))
        await session.commit()
    tenant = await _seed(db)
    response = await client.post(
        "/tenants/me/whatsapp/attempts", json=BODY, headers=_manager(tenant)
    )
    assert response.status_code == 409
    assert notified["calls"] == []


# --- the worker cron sends what is still pending -------------------------------------------


async def _connected_attempt(db, *, notified_at=None, status="connected") -> Tenant:
    tenant = await _seed(db)
    async with db() as session:
        session.add(
            WhatsAppConnectionAttempt(
                tenant_id=tenant.id,
                attempt_id=f"a-{tenant.id.hex[:6]}",
                status=status,
                phone_number_id=PHONE,
                waba_id=WABA,
                brain_notified_at=notified_at,
            )
        )
        await session.commit()
    return tenant


@pytest.fixture
def cron_db(db, monkeypatch):
    monkeypatch.setattr(onboarding_cron, "async_session_factory", db)
    monkeypatch.setenv("WHATSAPP_CONNECT_BACKEND_ENABLED", "true")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_cron_resends_pending_notices_until_brain_answers(db, cron_db, notified):
    from datetime import UTC, datetime

    pending = await _connected_attempt(db)
    already = await _connected_attempt(db, notified_at=datetime.now(UTC))
    failed = await _connected_attempt(db, status="failed")

    notified["ok"] = False
    await onboarding_cron.retry_whatsapp_connected_notices({})
    assert [c[0] for c in notified["calls"]] == [pending.id]
    assert (await _attempt(db, pending.id)).brain_notified_at is None

    notified["ok"] = True
    await onboarding_cron.retry_whatsapp_connected_notices({})
    assert [c[0] for c in notified["calls"]] == [pending.id, pending.id]
    assert (await _attempt(db, pending.id)).brain_notified_at is not None

    await onboarding_cron.retry_whatsapp_connected_notices({})
    assert len(notified["calls"]) == 2
    assert already.id not in [c[0] for c in notified["calls"]]
    assert failed.id not in [c[0] for c in notified["calls"]]


async def test_cron_is_a_no_op_with_the_flag_off(db, cron_db, notified, monkeypatch):
    await _connected_attempt(db)
    monkeypatch.setenv("WHATSAPP_CONNECT_BACKEND_ENABLED", "false")
    get_settings.cache_clear()
    await onboarding_cron.retry_whatsapp_connected_notices({})
    assert notified["calls"] == []


def test_cron_is_registered_in_the_worker():
    from secretaria.workers.arq_worker import WorkerSettings

    names = {getattr(job.coroutine, "__name__", "") for job in WorkerSettings.cron_jobs}
    assert "retry_whatsapp_connected_notices" in names
