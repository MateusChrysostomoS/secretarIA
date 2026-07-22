"""Tests for services/entitlements_client.py — the plugin/tier gating seam.

`get_entitlements` calls brain-api's GET /internal/tenants/{id}/entitlements.
These tests fake `httpx.AsyncClient` at the module level (mirrors
test_subscription.py), so no real network call is ever made, and fake Redis
with a minimal in-memory get/setex/delete so the caching contract can be
exercised without a live Redis.
"""

import os

# Set a deterministic environment BEFORE any `secretaria` import.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from uuid import uuid4

import httpx  # noqa: E402
import pytest  # noqa: E402

from secretaria.services import entitlements_client as ec  # noqa: E402
from secretaria.services.entitlements_client import (  # noqa: E402
    EntitlementSummary,
    get_entitlements,
    is_entitled,
)

_BAD_JSON = object()
_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="bronze_1",
        addons={**_ALL_ADDONS_OFF, "reactivation_pack": True, "pix_deposit": True},
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


# --------------------------------------------------------------------------
# is_entitled
# --------------------------------------------------------------------------


def test_inactive_tenant_entitled_to_nothing():
    summary = _summary(active=False, secretaria_tier="bronze_2")
    assert is_entitled(summary, "pix_deposit") is False
    assert is_entitled(summary, "reactivation_pack") is False
    assert is_entitled(summary, "bronze_1") is False
    assert is_entitled(summary, "ferro") is False


def test_addon_flag_true_and_false():
    summary = _summary()
    assert is_entitled(summary, "pix_deposit") is True
    assert is_entitled(summary, "verified_identity") is False


@pytest.mark.parametrize(
    "tenant_tier,check_tier,expected",
    [
        ("ferro", "ferro", True),
        ("ferro", "bronze_1", False),
        ("ferro", "bronze_2", False),
        ("bronze_1", "ferro", True),
        ("bronze_1", "bronze_1", True),
        ("bronze_1", "bronze_2", False),
        ("bronze_2", "ferro", True),
        ("bronze_2", "bronze_1", True),
        ("bronze_2", "bronze_2", True),
        (None, "ferro", False),
    ],
)
def test_tier_cumulativity(tenant_tier, check_tier, expected):
    summary = _summary(secretaria_tier=tenant_tier)
    assert is_entitled(summary, check_tier) is expected


def test_unknown_key_raises():
    summary = _summary()
    with pytest.raises(ValueError):
        is_entitled(summary, "not_a_real_key")


# --------------------------------------------------------------------------
# get_entitlements — httpx + Redis fakes
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int = 200, body: object = None) -> None:
        self.status_code = status_code
        self._body = body

    def json(self) -> object:
        if self._body is _BAD_JSON:
            raise ValueError("invalid json")
        return self._body


class _FakeAsyncClient:
    """Records init kwargs + GET requests; returns a canned response or raises."""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        raise_exc: Exception | None = None,
        calls: dict | None = None,
    ) -> None:
        self._response = response
        self._raise_exc = raise_exc
        self.calls = calls if calls is not None else {"init_kwargs": None, "gets": []}

    def __call__(self, **kwargs: object) -> "_FakeAsyncClient":
        self.calls["init_kwargs"] = kwargs
        return self

    async def __aenter__(self) -> "_FakeAsyncClient":
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        return False

    async def get(self, path: str, **kwargs: object) -> _FakeResponse:
        self.calls["gets"].append((path, kwargs))
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
    calls: dict = {"init_kwargs": None, "gets": []}
    response = None if raise_exc is not None else _FakeResponse(status_code, body)
    fake = _FakeAsyncClient(response=response, raise_exc=raise_exc, calls=calls)
    monkeypatch.setattr(ec.httpx, "AsyncClient", fake)
    return calls


def _body_for(tenant_id, **overrides) -> dict:
    body = dict(
        tenant_id=str(tenant_id),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="bronze_1",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    body.update(overrides)
    return body


class _FakeRedis:
    """Minimal async Redis stub: get / setex / delete / exists, in memory."""

    def __init__(self):
        self.store: dict[str, str] = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, seconds, value):
        self.store[key] = value

    async def delete(self, key):
        self.store.pop(key, None)

    async def exists(self, key):
        return 1 if key in self.store else 0


@pytest.fixture(autouse=True)
def _configured_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRAIN_API_BASE_URL", "http://brain-api.internal")
    monkeypatch.setenv("INTERNAL_API_KEY", "shared-secret")
    monkeypatch.setenv("ENTITLEMENT_CACHE_TTL_SECONDS", "300")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_fresh_fetch_parses_summary(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    _install_fake_client(
        monkeypatch,
        status_code=200,
        body=_body_for(
            tenant_id, secretaria_tier="bronze_2", addons={**_ALL_ADDONS_OFF, "ehr": True}
        ),
    )
    summary = await get_entitlements(tenant_id, redis=None)
    assert summary is not None
    assert summary.tenant_id == str(tenant_id)
    assert summary.active is True
    assert summary.secretaria_tier == "bronze_2"
    assert summary.addons["ehr"] is True


async def test_redis_none_is_direct_fetch_no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    calls = _install_fake_client(monkeypatch, status_code=200, body=_body_for(tenant_id))
    await get_entitlements(tenant_id, redis=None)
    await get_entitlements(tenant_id, redis=None)
    assert len(calls["gets"]) == 2


async def test_cache_hit_makes_one_http_call_for_two_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    calls = _install_fake_client(monkeypatch, status_code=200, body=_body_for(tenant_id))
    redis = _FakeRedis()
    first = await get_entitlements(tenant_id, redis=redis)
    second = await get_entitlements(tenant_id, redis=redis)
    assert first == second
    assert len(calls["gets"]) == 1


async def test_fetch_failure_falls_back_to_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    tenant_id = uuid4()
    redis = _FakeRedis()
    _install_fake_client(
        monkeypatch, status_code=200, body=_body_for(tenant_id, plan="bronze-stale")
    )
    first = await get_entitlements(tenant_id, redis=redis)
    assert first is not None

    # Force the next read to miss the fresh cache and hit brain-api again, this
    # time failing — the stale copy (written alongside the fresh one above)
    # must be returned instead of None.
    await redis.delete(ec._fresh_key(tenant_id))
    _install_fake_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))

    second = await get_entitlements(tenant_id, redis=redis)
    assert second == first


async def test_fetch_failure_without_stale_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tenant_id = uuid4()
    redis = _FakeRedis()
    _install_fake_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    assert await get_entitlements(tenant_id, redis=redis) is None


async def test_unconfigured_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BRAIN_API_BASE_URL", "")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    tenant_id = uuid4()
    calls = _install_fake_client(monkeypatch, status_code=200, body=_body_for(tenant_id))
    assert await get_entitlements(tenant_id, redis=None) is None
    assert calls["init_kwargs"] is None


async def test_ttl_zero_disables_fresh_cache_but_stale_still_used_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENTITLEMENT_CACHE_TTL_SECONDS", "0")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    tenant_id = uuid4()
    redis = _FakeRedis()
    calls = _install_fake_client(monkeypatch, status_code=200, body=_body_for(tenant_id))

    await get_entitlements(tenant_id, redis=redis)
    await get_entitlements(tenant_id, redis=redis)
    # Fresh caching disabled -> every call re-fetches.
    assert len(calls["gets"]) == 2
    # But the stale copy (TTL 86400, independent of the setting above) was
    # still written on each successful fetch and is usable on failure.
    _install_fake_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    assert await get_entitlements(tenant_id, redis=redis) is not None
