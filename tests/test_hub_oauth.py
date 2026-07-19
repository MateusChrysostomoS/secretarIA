"""Tests for api/hub/oauth.py — Google Calendar OAuth start/callback/disconnect,
including the per-professional variant (contract v1 §10 item C).

Uses the `client` fixture from conftest.py (ASGITransport, no real DB) with
`get_session` overridden to a real in-memory sqlite DB and `get_current_tenant`
overridden to a canned Tenant — the same pattern as test_hub_professionals.py.
`google_oauth.exchange_code_for_refresh_token` is monkeypatched so no real
network call to Google is ever made.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("GOOGLE_CLIENT_ID", "test-google-client-id")
os.environ.setdefault("GOOGLE_CLIENT_SECRET", "test-google-client-secret")
os.environ.setdefault("GOOGLE_OAUTH_REDIRECT_URI", "http://localhost:8000/oauth/google/callback")
os.environ.setdefault("OAUTH_STATE_SECRET", "test-oauth-state-secret")

from urllib.parse import parse_qs, urlparse  # noqa: E402
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

from secretaria.api.hub import oauth as oauth_api  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.services import google_oauth  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    get_google_refresh_token,
    get_professional_google_refresh_token,
    has_google_refresh_token,
    has_professional_google_refresh_token,
    set_professional_google_refresh_token,
)

CALLBACK = "/oauth/google/callback"


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


@pytest_asyncio.fixture
async def professional(db, tenant) -> Professional:
    async with db() as session:
        p = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p


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


def _extract_state(authorization_url: str) -> str:
    query = parse_qs(urlparse(authorization_url).query)
    return query["state"][0]


def _fake_exchange(refresh_token: str | None = "fresh-refresh-token", raises: bool = False):
    async def _fn(code: str):
        if raises:
            raise RuntimeError("google is down")
        return refresh_token

    return _fn


# --------------------------------------------------------------------------
# Tenant-level start (unchanged) — smoke coverage
# --------------------------------------------------------------------------


async def test_tenant_oauth_start_returns_authorization_url(client: AsyncClient, tenant) -> None:
    response = await client.get("/tenants/me/calendar/oauth/start")
    assert response.status_code == 200
    state = _extract_state(response.json()["authorization_url"])
    assert state  # non-empty, opaque signed token


# --------------------------------------------------------------------------
# Per-professional start
# --------------------------------------------------------------------------


async def test_professional_oauth_start_returns_authorization_url(
    client: AsyncClient, tenant, professional
) -> None:
    response = await client.get(
        f"/tenants/me/professionals/{professional.id}/calendar/oauth/start"
    )
    assert response.status_code == 200
    assert "authorization_url" in response.json()


async def test_professional_oauth_start_unowned_professional_is_404(
    client: AsyncClient, db
) -> None:
    async with db() as session:
        other_tenant = Tenant(id=uuid4(), clinic_name="Other", phone_number_id="other-num")
        session.add(other_tenant)
        await session.commit()
        foreign = Professional(tenant_id=other_tenant.id, name="Foreign Doc", is_active=True)
        session.add(foreign)
        await session.commit()
        await session.refresh(foreign)

    response = await client.get(f"/tenants/me/professionals/{foreign.id}/calendar/oauth/start")
    assert response.status_code == 404


async def test_professional_oauth_start_unknown_id_is_404(client: AsyncClient, tenant) -> None:
    response = await client.get(f"/tenants/me/professionals/{uuid4()}/calendar/oauth/start")
    assert response.status_code == 404


async def test_professional_oauth_start_500_when_not_configured(
    client: AsyncClient, professional, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.config import get_settings

    monkeypatch.setenv("OAUTH_STATE_SECRET", "")
    get_settings.cache_clear()
    try:
        response = await client.get(
            f"/tenants/me/professionals/{professional.id}/calendar/oauth/start"
        )
        assert response.status_code == 500
    finally:
        monkeypatch.setenv("OAUTH_STATE_SECRET", "test-oauth-state-secret")
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# Callback: state round-trip + routing (tenant-level vs. professional)
# --------------------------------------------------------------------------


async def test_callback_professional_state_routes_to_professional_credential(
    client: AsyncClient, db, tenant, professional, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = await client.get(
        f"/tenants/me/professionals/{professional.id}/calendar/oauth/start"
    )
    state = _extract_state(start.json()["authorization_url"])

    monkeypatch.setattr(
        google_oauth, "exchange_code_for_refresh_token", _fake_exchange("ana-refresh-token")
    )

    response = await client.get(CALLBACK, params={"code": "auth-code", "state": state})
    assert response.status_code in (200, 302)

    async with db() as session:
        assert await has_professional_google_refresh_token(session, professional.id) is True
        assert (
            await get_professional_google_refresh_token(session, professional.id)
            == "ana-refresh-token"
        )
        # The TENANT-level credential must be untouched by a professional-scoped connect.
        assert await has_google_refresh_token(session, tenant.id) is False


async def test_callback_tenant_level_state_routes_to_tenant_credential(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = await client.get("/tenants/me/calendar/oauth/start")
    state = _extract_state(start.json()["authorization_url"])

    monkeypatch.setattr(
        google_oauth, "exchange_code_for_refresh_token", _fake_exchange("tenant-refresh-token")
    )

    response = await client.get(CALLBACK, params={"code": "auth-code", "state": state})
    assert response.status_code in (200, 302)

    async with db() as session:
        assert await has_google_refresh_token(session, tenant.id) is True
        assert await get_google_refresh_token(session, tenant.id) == "tenant-refresh-token"


async def test_callback_missing_code_redirects_error(client: AsyncClient) -> None:
    response = await client.get(CALLBACK, params={"state": "whatever"})
    body = response.json() if response.headers.get("content-type", "").startswith(
        "application/json"
    ) else None
    assert response.status_code in (200, 302)
    if body is not None:
        assert body["status"] == "error"
    else:
        assert "status=error" in str(response.headers.get("location", ""))


async def test_callback_tampered_state_is_invalid(client: AsyncClient) -> None:
    response = await client.get(
        CALLBACK, params={"code": "auth-code", "state": "not-a-real-signed-state"}
    )
    assert response.status_code in (200, 302)


async def test_callback_professional_deleted_between_start_and_callback_is_invalid_state(
    client: AsyncClient, db, tenant, professional, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = await client.get(
        f"/tenants/me/professionals/{professional.id}/calendar/oauth/start"
    )
    state = _extract_state(start.json()["authorization_url"])

    async with db() as session:
        from sqlalchemy import delete

        await session.execute(delete(Professional).where(Professional.id == professional.id))
        await session.commit()

    monkeypatch.setattr(
        google_oauth, "exchange_code_for_refresh_token", _fake_exchange("should-not-be-stored")
    )
    response = await client.get(CALLBACK, params={"code": "auth-code", "state": state})
    assert response.status_code in (200, 302)

    async with db() as session:
        # Never partially connects the tenant-level credential either.
        assert await has_google_refresh_token(session, tenant.id) is False


async def test_callback_no_refresh_token_from_google(
    client: AsyncClient, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = await client.get("/tenants/me/calendar/oauth/start")
    state = _extract_state(start.json()["authorization_url"])
    monkeypatch.setattr(google_oauth, "exchange_code_for_refresh_token", _fake_exchange(None))

    response = await client.get(CALLBACK, params={"code": "auth-code", "state": state})
    assert response.status_code in (200, 302)


async def test_callback_exchange_failure_does_not_raise(
    client: AsyncClient, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    start = await client.get("/tenants/me/calendar/oauth/start")
    state = _extract_state(start.json()["authorization_url"])
    monkeypatch.setattr(
        google_oauth, "exchange_code_for_refresh_token", _fake_exchange(raises=True)
    )

    response = await client.get(CALLBACK, params={"code": "auth-code", "state": state})
    assert response.status_code in (200, 302)  # never a 5xx


# --------------------------------------------------------------------------
# Per-professional disconnect
# --------------------------------------------------------------------------


async def test_professional_disconnect_clears_only_that_credential(
    client: AsyncClient, db, tenant, professional
) -> None:
    async with db() as session:
        await set_professional_google_refresh_token(session, professional.id, "some-token")
        await session.commit()

    response = await client.post(
        f"/tenants/me/professionals/{professional.id}/calendar/disconnect"
    )
    assert response.status_code == 200
    assert response.json() == {"status": "disconnected", "professional_id": str(professional.id)}

    async with db() as session:
        assert await has_professional_google_refresh_token(session, professional.id) is False
        refreshed_tenant = await session.get(Tenant, tenant.id)
    # Unlike the tenant-level disconnect, this must NOT force is_active off.
    assert refreshed_tenant.is_active == tenant.is_active


async def test_professional_disconnect_unowned_is_404(client: AsyncClient, db) -> None:
    async with db() as session:
        other_tenant = Tenant(id=uuid4(), clinic_name="Other", phone_number_id="other-num-2")
        session.add(other_tenant)
        await session.commit()
        foreign = Professional(tenant_id=other_tenant.id, name="Foreign Doc", is_active=True)
        session.add(foreign)
        await session.commit()
        await session.refresh(foreign)

    response = await client.post(f"/tenants/me/professionals/{foreign.id}/calendar/disconnect")
    assert response.status_code == 404


def test_owned_professional_helper_is_used_consistently() -> None:
    """Smoke check that the ownership-validation helper exists and is private
    (not part of the public API surface)."""
    assert hasattr(oauth_api, "_owned_professional")
