"""Tests for services/precheck.py — the precheck hand-off seam.

`request_precheck_handoff` calls brain-api's POST /internal/precheck-handoff.
These tests fake `httpx.AsyncClient` at the module level (mirrors
test_subscription.py), so no real network call is ever made. They cover every
outcome the frozen contract defines, the outbound request shape, and the
fail-closed behaviour on any ambiguity.
"""

import os

# Set a deterministic environment BEFORE any `secretaria` import.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from secretaria.services import precheck  # noqa: E402
from secretaria.services.precheck import HandoffOutcome, HandoffResult  # noqa: E402

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
    calls: dict = {"init_kwargs": None, "posts": []}
    response = None if raise_exc is not None else _FakeResponse(status_code, body)
    fake = _FakeAsyncClient(response=response, raise_exc=raise_exc, calls=calls)
    monkeypatch.setattr(precheck.httpx, "AsyncClient", fake)
    return calls


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch):
    """Every test gets BRAIN_API_BASE_URL/INTERNAL_API_KEY configured by default."""
    monkeypatch.setenv("BRAIN_API_BASE_URL", "http://brain-api.internal")
    monkeypatch.setenv("INTERNAL_API_KEY", "shared-secret")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


async def test_seeded_maps_to_seeded_outcome(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.SEEDED)


async def test_already_active_maps_to_already_active_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_client(monkeypatch, status_code=200, body={"status": "already_active"})
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.ALREADY_ACTIVE)


async def test_unexpected_200_body_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=200, body={"status": "something_else"})
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)


async def test_bad_json_on_200_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=200, body=_BAD_JSON)
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)


async def test_403_maps_to_not_entitled(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=403, body={"detail": "precheck_not_entitled"})
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.NOT_ENTITLED)


async def test_404_maps_to_no_clinic(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=404, body={"detail": "no_clinic_for_tenant"})
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.NO_CLINIC)


async def test_409_maps_to_conflict(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(
        monkeypatch, status_code=409, body={"detail": "conflicting_active_session"}
    )
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.CONFLICT)


async def test_503_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=503, body=None)
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)


async def test_502_maps_to_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=502, body=None)
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)


async def test_unexpected_status_code_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, status_code=418, body=None)
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)


async def test_network_error_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)


async def test_timeout_returns_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_client(monkeypatch, raise_exc=httpx.TimeoutException("timed out"))
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)


async def test_unconfigured_base_url_returns_unavailable_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("BRAIN_API_BASE_URL", "")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)
    assert calls["init_kwargs"] is None


async def test_unconfigured_internal_key_returns_unavailable_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})
    result = await precheck.request_precheck_handoff(uuid4(), "5511999999999")
    assert result == HandoffResult(HandoffOutcome.UNAVAILABLE)
    assert calls["init_kwargs"] is None


async def test_outbound_request_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request carries X-Internal-Api-Key and {tenant_id, phone_number} in the body."""
    tenant_id = uuid4()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})
    await precheck.request_precheck_handoff(tenant_id, "5511999999999")

    assert calls["init_kwargs"]["base_url"] == "http://brain-api.internal"
    assert calls["init_kwargs"]["headers"]["X-Internal-Api-Key"] == "shared-secret"

    assert len(calls["posts"]) == 1
    path, kwargs = calls["posts"][0]
    assert path == "/internal/precheck-handoff"
    assert kwargs["json"] == {"tenant_id": str(tenant_id), "phone_number": "5511999999999"}


async def test_result_is_frozen_dataclass() -> None:
    import dataclasses

    result = HandoffResult(HandoffOutcome.SEEDED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = HandoffOutcome.UNAVAILABLE  # type: ignore[misc]
