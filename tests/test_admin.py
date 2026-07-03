"""Tests for the admin reset endpoint and the X-Admin-Token guard."""

import os

# Set the admin token BEFORE secretaria imports so Settings picks it up.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ["ADMIN_TOKEN"] = "test-admin-token"

import pytest  # noqa: E402
from httpx import AsyncClient  # noqa: E402

from secretaria.services import admin as admin_service  # noqa: E402

GOOD_TOKEN = "test-admin-token"
ENDPOINT = "/admin/reset"


@pytest.fixture(autouse=True)
def _fake_wipe(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """Replace the destructive coroutine with a recorder.

    Tests must never reach Postgres. The recorder yields the args the
    endpoint passed so each test can assert on them.
    """
    calls: list[dict] = []

    async def _record(**kwargs: object) -> list[str]:
        calls.append(dict(kwargs))
        tables = list(admin_service.DEFAULT_WIPE_TABLES)
        if kwargs.get("include_tenants"):
            tables.append(admin_service.TENANT_TABLE)
        return tables

    monkeypatch.setattr("secretaria.api.admin.panel.wipe_data", _record)
    return calls


async def test_missing_token_is_forbidden(client: AsyncClient) -> None:
    response = await client.post(ENDPOINT, json={"confirm": True})
    assert response.status_code == 403


async def test_wrong_token_is_forbidden(client: AsyncClient) -> None:
    response = await client.post(
        ENDPOINT,
        json={"confirm": True},
        headers={"X-Admin-Token": "wrong-token"},
    )
    assert response.status_code == 403


async def test_missing_confirm_is_rejected(client: AsyncClient) -> None:
    response = await client.post(
        ENDPOINT,
        json={"confirm": False},
        headers={"X-Admin-Token": GOOD_TOKEN},
    )
    assert response.status_code == 400


async def test_default_wipe_keeps_tenants(
    client: AsyncClient,
    _fake_wipe: list[dict],
) -> None:
    response = await client.post(
        ENDPOINT,
        json={"confirm": True},
        headers={"X-Admin-Token": GOOD_TOKEN},
    )
    assert response.status_code == 200
    body = response.json()
    assert "tenants" not in body["wiped"]
    assert body["wiped"] == list(admin_service.DEFAULT_WIPE_TABLES)
    assert _fake_wipe == [{"include_tenants": False}]


async def test_include_tenants_wipes_tenants(
    client: AsyncClient,
    _fake_wipe: list[dict],
) -> None:
    response = await client.post(
        ENDPOINT,
        json={"confirm": True, "include_tenants": True},
        headers={"X-Admin-Token": GOOD_TOKEN},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["wiped"][-1] == admin_service.TENANT_TABLE
    assert _fake_wipe == [{"include_tenants": True}]


async def test_unconfigured_token_returns_503(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from secretaria.config import get_settings

    # Force a fresh Settings with ADMIN_TOKEN empty for this request only.
    monkeypatch.setenv("ADMIN_TOKEN", "")
    get_settings.cache_clear()
    try:
        response = await client.post(
            ENDPOINT,
            json={"confirm": True},
            headers={"X-Admin-Token": "anything"},
        )
        assert response.status_code == 503
    finally:
        monkeypatch.setenv("ADMIN_TOKEN", GOOD_TOKEN)
        get_settings.cache_clear()


async def test_endpoint_is_in_openapi_schema(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert "/admin/reset" in schema["paths"]
    post = schema["paths"]["/admin/reset"]["post"]
    assert "admin" in post["tags"]


# --------------------------------------------------------------------------
# Key rotation: ADMIN_TOKEN_PREVIOUS is accepted alongside ADMIN_TOKEN
# --------------------------------------------------------------------------


async def test_previous_token_is_accepted_during_rotation(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    _fake_wipe: list[dict],
) -> None:
    from secretaria.config import get_settings

    monkeypatch.setenv("ADMIN_TOKEN_PREVIOUS", "old-admin-token")
    get_settings.cache_clear()
    try:
        response = await client.post(
            ENDPOINT,
            json={"confirm": True},
            headers={"X-Admin-Token": "old-admin-token"},
        )
        assert response.status_code == 200
    finally:
        monkeypatch.setenv("ADMIN_TOKEN_PREVIOUS", "")
        get_settings.cache_clear()


async def test_current_token_still_accepted_when_previous_is_configured(
    client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    _fake_wipe: list[dict],
) -> None:
    from secretaria.config import get_settings

    monkeypatch.setenv("ADMIN_TOKEN_PREVIOUS", "old-admin-token")
    get_settings.cache_clear()
    try:
        response = await client.post(
            ENDPOINT,
            json={"confirm": True},
            headers={"X-Admin-Token": GOOD_TOKEN},
        )
        assert response.status_code == 200
    finally:
        monkeypatch.setenv("ADMIN_TOKEN_PREVIOUS", "")
        get_settings.cache_clear()


async def test_neither_current_nor_previous_token_is_forbidden(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.config import get_settings

    monkeypatch.setenv("ADMIN_TOKEN_PREVIOUS", "old-admin-token")
    get_settings.cache_clear()
    try:
        response = await client.post(
            ENDPOINT,
            json={"confirm": True},
            headers={"X-Admin-Token": "some-other-token"},
        )
        assert response.status_code == 403
    finally:
        monkeypatch.setenv("ADMIN_TOKEN_PREVIOUS", "")
        get_settings.cache_clear()
