"""TASK-093 Plano 3, Task 2: services/whatsapp_connect.py - the secretarIA connects WhatsApp itself.

Mirrors brain-api `services/meta_graph.py` (`exchange_code_for_token`, `subscribe_app_to_waba`)
and reuses `services/provisioning.py::connect_whatsapp` (phone uniqueness, encrypted token).
Meta is NEVER called for real: every test plugs an `httpx.MockTransport` into the module.
"""

import logging
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.config import get_settings
from secretaria.core.database import Base
from secretaria.models import Tenant, WhatsAppConnectionAttempt
from secretaria.services import whatsapp_connect
from secretaria.services.tenant_config import get_waba_token, has_waba_token
from secretaria.services.whatsapp_connect import (
    CODE_EXCHANGE_FAILED,
    PHONE_NUMBER_CONFLICT,
    WABA_SUBSCRIBE_FAILED,
    ConnectError,
    complete_attempt,
    start_attempt,
)

TOKEN = "EAAG-meta-token-never-logged-123"
CODE = "meta-oauth-code-456"
PHONE = "109876543210"
WABA = "998877665544"


class FakeMeta:
    """A scripted Graph API: records every request, answers per path."""

    def __init__(self, *, exchange_status=200, exchange_body=None, subscribe_status=200):
        self.requests: list[httpx.Request] = []
        self.exchange_status = exchange_status
        self.exchange_body = {"access_token": TOKEN} if exchange_body is None else exchange_body
        self.subscribe_status = subscribe_status
        self.unreachable = False

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.unreachable:
            raise httpx.ConnectError("down", request=request)
        if request.url.path.endswith("/oauth/access_token"):
            return httpx.Response(self.exchange_status, json=self.exchange_body)
        if request.url.path.endswith(f"/{WABA}/subscribed_apps"):
            return httpx.Response(self.subscribe_status, json={"success": True})
        return httpx.Response(404, json={})


@pytest.fixture
def meta(monkeypatch) -> FakeMeta:
    fake = FakeMeta()
    monkeypatch.setattr(whatsapp_connect, "_TRANSPORT", httpx.MockTransport(fake.handler))
    monkeypatch.setenv("META_APP_ID", "app-123")
    monkeypatch.setenv("META_APP_SECRET", "app-secret-789")
    get_settings.cache_clear()
    yield fake
    get_settings.cache_clear()


@pytest.fixture
async def db():
    engine = create_async_engine("sqlite+aiosqlite://")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _tenant(session, **fields) -> Tenant:
    tenant = Tenant(id=uuid4(), clinic_name="Clínica", **fields)
    session.add(tenant)
    await session.commit()
    return tenant


async def _complete(session, tenant, attempt_id="att-1"):
    return await complete_attempt(
        session, tenant.id, attempt_id, code=CODE, phone_number_id=PHONE, waba_id=WABA
    )


# --- start_attempt -------------------------------------------------------------------------


async def test_start_attempt_creates_once_and_returns_the_same_row(db):
    async with db() as session:
        tenant = await _tenant(session)
        first = await start_attempt(session, tenant.id, "att-1")
        again = await start_attempt(session, tenant.id, "att-1")
        await session.commit()
        assert first.id == again.id
        assert first.status == "pending"


# --- happy path ----------------------------------------------------------------------------


async def test_connects_exchanging_the_code_and_subscribing_the_waba(db, meta):
    async with db() as session:
        tenant = await _tenant(session)
        attempt = await _complete(session, tenant)
        await session.commit()

        assert attempt.status == "connected"
        assert attempt.failure_code is None
        assert attempt.phone_number_id == PHONE
        assert attempt.waba_id == WABA
        await session.refresh(tenant)
        assert tenant.phone_number_id == PHONE
        assert tenant.waba_id == WABA
        assert tenant.connected_at is not None
        assert await get_waba_token(session, tenant.id) == TOKEN

    exchange, subscribe = meta.requests
    assert exchange.method == "GET"
    assert exchange.url.path.endswith("/oauth/access_token")
    assert exchange.url.params["client_id"] == "app-123"
    assert exchange.url.params["client_secret"] == "app-secret-789"
    assert exchange.url.params["code"] == CODE
    assert subscribe.method == "POST"
    assert subscribe.headers["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in str(subscribe.url)


async def test_the_same_attempt_id_never_exchanges_the_code_twice(db, meta):
    async with db() as session:
        tenant = await _tenant(session)
        await _complete(session, tenant)
        await session.commit()
        calls = len(meta.requests)

        replay = await _complete(session, tenant)
        await session.commit()

        assert replay.status == "connected"
        assert len(meta.requests) == calls
        rows = (await session.execute(WhatsAppConnectionAttempt.__table__.select())).all()
        assert len(rows) == 1


# --- failures: clear error, no partial credential ------------------------------------------


async def _assert_nothing_stored(session, tenant):
    await session.refresh(tenant)
    assert tenant.phone_number_id is None
    assert tenant.connected_at is None
    assert await has_waba_token(session, tenant.id) is False


async def test_invalid_code_fails_without_storing_any_credential(db, meta):
    meta.exchange_status = 400
    meta.exchange_body = {"error": {"message": "Invalid verification code"}}
    async with db() as session:
        tenant = await _tenant(session)
        with pytest.raises(ConnectError) as caught:
            await _complete(session, tenant)
        await session.commit()

        assert caught.value.code == CODE_EXCHANGE_FAILED == "code_exchange_failed"
        attempt = caught.value.attempt
        assert attempt.status == "failed"
        assert attempt.failure_code == "code_exchange_failed"
        await _assert_nothing_stored(session, tenant)
    # Nothing was subscribed with a token we never got.
    assert len(meta.requests) == 1


async def test_a_failed_attempt_replayed_fails_the_same_way_without_calling_meta(db, meta):
    meta.exchange_status = 400
    async with db() as session:
        tenant = await _tenant(session)
        with pytest.raises(ConnectError):
            await _complete(session, tenant)
        await session.commit()
        meta.exchange_status = 200
        with pytest.raises(ConnectError) as again:
            await _complete(session, tenant)
        assert again.value.code == "code_exchange_failed"
    assert len(meta.requests) == 1


async def test_a_response_without_token_is_a_code_exchange_failure(db, meta):
    meta.exchange_body = {"token_type": "bearer"}
    async with db() as session:
        tenant = await _tenant(session)
        with pytest.raises(ConnectError) as caught:
            await _complete(session, tenant)
        assert caught.value.code == "code_exchange_failed"
        await _assert_nothing_stored(session, tenant)


async def test_meta_unreachable_is_a_code_exchange_failure(db, meta):
    meta.unreachable = True
    async with db() as session:
        tenant = await _tenant(session)
        with pytest.raises(ConnectError) as caught:
            await _complete(session, tenant)
        assert caught.value.code == "code_exchange_failed"
        await _assert_nothing_stored(session, tenant)


async def test_unconfigured_app_fails_without_calling_meta(db, meta, monkeypatch):
    monkeypatch.setenv("META_APP_ID", "")
    get_settings.cache_clear()
    async with db() as session:
        tenant = await _tenant(session)
        with pytest.raises(ConnectError) as caught:
            await _complete(session, tenant)
        assert caught.value.code == "code_exchange_failed"
    assert meta.requests == []


async def test_waba_subscription_failure_stores_nothing(db, meta):
    meta.subscribe_status = 500
    async with db() as session:
        tenant = await _tenant(session)
        with pytest.raises(ConnectError) as caught:
            await _complete(session, tenant)
        await session.commit()
        assert caught.value.code == WABA_SUBSCRIBE_FAILED == "waba_subscribe_failed"
        assert caught.value.attempt.status == "failed"
        await _assert_nothing_stored(session, tenant)


async def test_a_number_of_another_clinic_is_a_conflict_and_meta_is_not_called(db, meta):
    async with db() as session:
        await _tenant(session, phone_number_id=PHONE)
        tenant = await _tenant(session)
        with pytest.raises(ConnectError) as caught:
            await _complete(session, tenant)
        await session.commit()
        assert caught.value.code == PHONE_NUMBER_CONFLICT == "phone_number_conflict"
        assert caught.value.attempt.failure_code == "phone_number_conflict"
        await _assert_nothing_stored(session, tenant)
    assert meta.requests == []


async def test_a_conflict_that_appears_during_the_meta_calls_still_stores_nothing(
    db, meta, monkeypatch
):
    # The other clinic took the number between the pre-check and the write
    # (provisioning.connect_whatsapp re-checks): conflict, no token, no number.
    async with db() as session:
        tenant = await _tenant(session)
        other_id = uuid4()

        real_subscribe = whatsapp_connect.subscribe_app_to_waba

        async def subscribe_then_lose_the_race(waba_id, token):
            async with db() as other:
                other.add(Tenant(id=other_id, clinic_name="Outra", phone_number_id=PHONE))
                await other.commit()
            return await real_subscribe(waba_id, token)

        monkeypatch.setattr(whatsapp_connect, "subscribe_app_to_waba", subscribe_then_lose_the_race)
        with pytest.raises(ConnectError) as caught:
            await _complete(session, tenant)
        await session.commit()
        assert caught.value.code == "phone_number_conflict"
        await _assert_nothing_stored(session, tenant)


# --- secrets never in logs -----------------------------------------------------------------


async def test_token_code_and_app_secret_never_reach_the_logs(db, meta, caplog):
    # recorded_logs, not structlog.testing.capture_logs: with cache_logger_on_first_use a logger
    # an earlier test already used ignores capture_logs (green alone, red in the full suite).
    # It records the raw kwargs, before any redaction processor - a stricter check.
    from tests.before_support import recorded_logs

    caplog.set_level(logging.DEBUG)
    with recorded_logs() as logs:
        async with db() as session:
            tenant = await _tenant(session)
            await _complete(session, tenant, "ok-1")
            await session.commit()
            meta.exchange_status = 400
            with pytest.raises(ConnectError):
                await complete_attempt(
                    session, tenant.id, "bad-1", code=CODE, phone_number_id=PHONE, waba_id=WABA
                )
    rendered = repr(logs) + caplog.text
    assert logs, "the service should log its outcomes"
    for secret in (TOKEN, CODE, "app-secret-789"):
        assert secret not in rendered


# --- concurrency (Postgres only: SQLite has no row lock) -----------------------------------

PG_URL = __import__("os").environ.get("SECRETARIA_PG_TEST_URL", "")


@pytest.mark.skipif(not PG_URL, reason="needs a disposable Postgres (SECRETARIA_PG_TEST_URL)")
async def test_two_managers_at_once_with_the_same_attempt_connect_once(monkeypatch):
    import asyncio

    # PG_URL points at a disposable database already at `alembic upgrade head`.
    engine = create_async_engine(PG_URL)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    exchanges = 0

    async def slow_meta(request: httpx.Request) -> httpx.Response:
        nonlocal exchanges
        if request.url.path.endswith("/oauth/access_token"):
            exchanges += 1
            await asyncio.sleep(0.5)
            return httpx.Response(200, json={"access_token": TOKEN})
        return httpx.Response(200, json={"success": True})

    monkeypatch.setattr(whatsapp_connect, "_TRANSPORT", httpx.MockTransport(slow_meta))
    monkeypatch.setenv("META_APP_ID", "app-123")
    monkeypatch.setenv("META_APP_SECRET", "app-secret-789")
    get_settings.cache_clear()
    try:
        async with maker() as session:
            tenant = await _tenant(session)

        async def one_manager():
            async with maker() as session:
                attempt = await _complete(session, tenant, "same-attempt")
                await session.commit()
                return attempt.status

        statuses = await asyncio.gather(one_manager(), one_manager())
        assert statuses == ["connected", "connected"]
        assert exchanges == 1
        async with maker() as session:
            table = WhatsAppConnectionAttempt.__table__
            rows = (
                await session.execute(table.select().where(table.c.tenant_id == tenant.id))
            ).all()
            assert len(rows) == 1
            assert await get_waba_token(session, tenant.id) == TOKEN
    finally:
        get_settings.cache_clear()
        async with maker() as session:
            await session.execute(Tenant.__table__.delete().where(Tenant.id == tenant.id))
            await session.commit()
        await engine.dispose()
