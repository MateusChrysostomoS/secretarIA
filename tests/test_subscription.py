"""Tests for core/subscription.py — the doctor-hub token verification seam.

`verify_subscription_token` calls brain-api's
POST /internal/secretaria/hub-token/verify. These tests fake `httpx.AsyncClient`
at the module level (monkeypatching `subscription.httpx.AsyncClient`), so no
real network call is ever made. They cover the fail-closed behaviour, the
outbound request shape, and the in-process positive-result cache.
"""

import os
from uuid import UUID, uuid4

# Set a deterministic environment BEFORE any `secretaria` import.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import httpx  # noqa: E402
import pytest  # noqa: E402

from secretaria.core import subscription  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402

_BAD_JSON = object()


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if self._body is _BAD_JSON:
            raise ValueError("invalid json")
        return self._body


class _FakeAsyncClient:
    """Records init kwargs + posted requests; returns a canned response or raises."""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        raise_exc: Exception | None = None,
        calls: dict | None = None,
    ) -> None:
        self._response = response
        self._raise_exc = raise_exc
        self.calls = calls if calls is not None else {"init_kwargs": None, "posts": []}

    def __call__(self, **kwargs: object) -> "_FakeAsyncClient":
        # Instantiated by subscription.py as httpx.AsyncClient(**kwargs).
        self.calls["init_kwargs"] = kwargs
        return self

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def post(self, path: str, **kwargs: object) -> _FakeResponse:
        self.calls["posts"].append((path, kwargs))
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._response is not None
        return self._response


def _install_fake_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_code: int = 200,
    body: object = None,
    raise_exc: Exception | None = None,
) -> dict:
    """Patch subscription.httpx.AsyncClient; return the calls-tracking dict."""
    calls: dict = {"init_kwargs": None, "posts": []}
    response = None if raise_exc is not None else _FakeResponse(status_code, body)
    fake = _FakeAsyncClient(response=response, raise_exc=raise_exc, calls=calls)
    monkeypatch.setattr(subscription.httpx, "AsyncClient", fake)
    return calls


@pytest.fixture(autouse=True)
def _clear_cache_and_settings(monkeypatch: pytest.MonkeyPatch):
    """Every test starts with a clean positive-result cache and fresh settings,
    with BRAIN_API_BASE_URL/INTERNAL_API_KEY configured by default."""
    subscription._cache.clear()
    monkeypatch.setenv("BRAIN_API_BASE_URL", "http://brain-api.internal")
    monkeypatch.setenv("INTERNAL_API_KEY", "shared-secret")
    monkeypatch.setenv("SUBSCRIPTION_CACHE_TTL_SECONDS", "60")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    yield
    subscription._cache.clear()
    get_settings.cache_clear()


async def test_empty_token_returns_none_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _install_fake_client(monkeypatch, status_code=200, body={"active": True})
    assert await subscription.verify_subscription_token("") is None
    assert await subscription.verify_subscription_token("   ") is None
    assert calls["init_kwargs"] is None


async def test_active_true_returns_claim_with_tenant_uuid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    _install_fake_client(
        monkeypatch, status_code=200, body={"active": True, "tenant_id": str(tenant_id)}
    )
    claim = await subscription.verify_subscription_token("some-token")
    assert claim == SubscriptionClaim(tenant_id=tenant_id, active=True)


async def test_active_false_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=200, body={"active": False, "tenant_id": None})
    assert await subscription.verify_subscription_token("some-token") is None


async def test_network_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    assert await subscription.verify_subscription_token("some-token") is None


async def test_timeout_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, raise_exc=httpx.TimeoutException("timed out"))
    assert await subscription.verify_subscription_token("some-token") is None


async def test_non_200_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=500, body={"active": True})
    assert await subscription.verify_subscription_token("some-token") is None


async def test_bad_json_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=200, body=_BAD_JSON)
    assert await subscription.verify_subscription_token("some-token") is None


async def test_unconfigured_base_url_returns_none_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_API_BASE_URL", "")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"active": True})
    assert await subscription.verify_subscription_token("some-token") is None
    assert calls["init_kwargs"] is None


async def test_unconfigured_internal_key_returns_none_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"active": True})
    assert await subscription.verify_subscription_token("some-token") is None
    assert calls["init_kwargs"] is None


async def test_positive_cache_makes_one_http_call_within_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    calls = _install_fake_client(
        monkeypatch, status_code=200, body={"active": True, "tenant_id": str(tenant_id)}
    )
    token = "same-token"
    first = await subscription.verify_subscription_token(token)
    second = await subscription.verify_subscription_token(token)
    assert first == second == SubscriptionClaim(tenant_id=tenant_id, active=True)
    assert len(calls["posts"]) == 1


async def test_ttl_zero_disables_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUBSCRIPTION_CACHE_TTL_SECONDS", "0")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    tenant_id = uuid4()
    calls = _install_fake_client(
        monkeypatch, status_code=200, body={"active": True, "tenant_id": str(tenant_id)}
    )
    token = "same-token"
    await subscription.verify_subscription_token(token)
    await subscription.verify_subscription_token(token)
    assert len(calls["posts"]) == 2


async def test_negative_result_is_never_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _install_fake_client(monkeypatch, status_code=200, body={"active": False})
    token = "same-token"
    await subscription.verify_subscription_token(token)
    await subscription.verify_subscription_token(token)
    assert len(calls["posts"]) == 2


async def test_outbound_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request carries X-Internal-Api-Key and the token in the JSON body."""
    tenant_id = uuid4()
    calls = _install_fake_client(
        monkeypatch, status_code=200, body={"active": True, "tenant_id": str(tenant_id)}
    )
    await subscription.verify_subscription_token("the-bearer-token")

    assert calls["init_kwargs"]["base_url"] == "http://brain-api.internal"
    assert calls["init_kwargs"]["headers"]["X-Internal-Api-Key"] == "shared-secret"

    assert len(calls["posts"]) == 1
    path, kwargs = calls["posts"][0]
    assert path == "/internal/secretaria/hub-token/verify"
    assert kwargs["json"] == {"token": "the-bearer-token"}


async def test_claim_is_frozen_dataclass() -> None:
    import dataclasses

    claim = SubscriptionClaim(tenant_id=None, active=False)
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.active = True  # type: ignore[misc]


def test_tenant_id_type() -> None:
    tenant_id = uuid4()
    claim = SubscriptionClaim(tenant_id=tenant_id, active=True)
    assert isinstance(claim.tenant_id, UUID)
