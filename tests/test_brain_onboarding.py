"""Tests for services/brain_onboarding.py — the onboarding-cron -> brain-api
client (contract v1 §4 endpoints 7/8).

Fakes `httpx.AsyncClient` at the module level (mirrors test_subscription.py /
test_entitlements_client.py), so no real network call is ever made.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from datetime import UTC, datetime  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from secretaria.services import brain_onboarding as bo  # noqa: E402

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
    """Records init kwargs + requests; returns a canned response or raises."""

    def __init__(
        self,
        *,
        response: _FakeResponse | None = None,
        raise_exc: Exception | None = None,
        calls: dict | None = None,
    ) -> None:
        self._response = response
        self._raise_exc = raise_exc
        self.calls = calls if calls is not None else {"init_kwargs": None, "gets": [], "posts": []}

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
    calls: dict = {"init_kwargs": None, "gets": [], "posts": []}
    response = None if raise_exc is not None else _FakeResponse(status_code, body)
    fake = _FakeAsyncClient(response=response, raise_exc=raise_exc, calls=calls)
    monkeypatch.setattr(bo.httpx, "AsyncClient", fake)
    return calls


@pytest.fixture(autouse=True)
def _configured_settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRAIN_API_BASE_URL", "http://brain-api.internal")
    monkeypatch.setenv("INTERNAL_API_KEY", "shared-secret")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _raw_item(**overrides) -> dict:
    base = dict(
        tenant_id=str(uuid4()),
        onboarding_state="aquecimento",
        blocker_reason=None,
        config_status="incompleta",
        onboarding_anchor_at="2026-07-01T12:00:00Z",
        next_retry_at="2026-07-04T12:00:00+00:00",
        retry_paused=False,
        config_reminder_paused=False,
        config_reminder_anchor_at="2026-07-01T12:00:00Z",
        last_config_reminder_at=None,
        closing_email_sent_at=None,
        manual_review_flagged_at=None,
        owner_email="owner@example.com",
        owner_name="Owner",
        clinic_name="Clinic",
        subscription_active=True,
    )
    base.update(overrides)
    return base


# --------------------------------------------------------------------------
# list_onboarding_tenants
# --------------------------------------------------------------------------


async def test_list_unconfigured_returns_none_without_http_call(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BRAIN_API_BASE_URL", "")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"items": []})
    assert await bo.list_onboarding_tenants() is None
    assert calls["init_kwargs"] is None


async def test_list_network_error_returns_none(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    assert await bo.list_onboarding_tenants() is None


async def test_list_non_200_returns_none(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, status_code=500, body={"items": []})
    assert await bo.list_onboarding_tenants() is None


async def test_list_bad_json_returns_none(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, status_code=200, body=_BAD_JSON)
    assert await bo.list_onboarding_tenants() is None


async def test_list_bad_shape_returns_none(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, status_code=200, body={"not_items": []})
    assert await bo.list_onboarding_tenants() is None


async def test_list_empty_items_returns_empty_list(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_client(monkeypatch, status_code=200, body={"items": []})
    result = await bo.list_onboarding_tenants()
    assert result == []
    assert calls["gets"][0][0] == "/internal/onboarding/tenants"


async def test_list_parses_full_item_shape(monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    raw = _raw_item(tenant_id=str(tenant_id))
    _install_fake_client(monkeypatch, status_code=200, body={"items": [raw]})

    result = await bo.list_onboarding_tenants()

    assert result is not None
    assert len(result) == 1
    item = result[0]
    assert item.tenant_id == tenant_id
    assert item.onboarding_state == "aquecimento"
    assert item.blocker_reason is None
    assert item.config_status == "incompleta"
    assert item.onboarding_anchor_at == datetime(2026, 7, 1, 12, 0, tzinfo=UTC)
    assert item.next_retry_at == datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
    assert item.retry_paused is False
    assert item.config_reminder_paused is False
    assert item.last_config_reminder_at is None
    assert item.owner_email == "owner@example.com"
    assert item.owner_name == "Owner"
    assert item.clinic_name == "Clinic"
    assert item.subscription_active is True


async def test_list_malformed_item_is_skipped_others_still_parsed(monkeypatch: pytest.MonkeyPatch):
    good = _raw_item()
    bad = _raw_item(tenant_id="not-a-uuid")
    _install_fake_client(monkeypatch, status_code=200, body={"items": [bad, good]})

    result = await bo.list_onboarding_tenants()

    assert result is not None
    assert len(result) == 1
    assert str(result[0].tenant_id) == good["tenant_id"]


async def test_list_sends_internal_api_key_header(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_client(monkeypatch, status_code=200, body={"items": []})
    await bo.list_onboarding_tenants()
    assert calls["init_kwargs"]["headers"] == {"X-Internal-Api-Key": "shared-secret"}


# --------------------------------------------------------------------------
# post_onboarding_event
# --------------------------------------------------------------------------


async def test_post_event_unconfigured_returns_false_without_http_call(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    from secretaria.config import get_settings

    get_settings.cache_clear()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"applied": True})
    result = await bo.post_onboarding_event(uuid4(), "retry_nudge_sent", datetime.now(UTC))
    assert result is False
    assert calls["init_kwargs"] is None


async def test_post_event_network_error_returns_false(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))
    result = await bo.post_onboarding_event(uuid4(), "retry_nudge_sent", datetime.now(UTC))
    assert result is False


async def test_post_event_non_200_returns_false(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, status_code=500, body={"applied": True})
    result = await bo.post_onboarding_event(uuid4(), "retry_nudge_sent", datetime.now(UTC))
    assert result is False


async def test_post_event_bad_json_returns_false(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, status_code=200, body=_BAD_JSON)
    result = await bo.post_onboarding_event(uuid4(), "retry_nudge_sent", datetime.now(UTC))
    assert result is False


async def test_post_event_applied_true_is_returned(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, status_code=200, body={"applied": True})
    result = await bo.post_onboarding_event(uuid4(), "closing_email_sent", datetime.now(UTC))
    assert result is True


async def test_post_event_applied_false_is_returned(monkeypatch: pytest.MonkeyPatch):
    _install_fake_client(monkeypatch, status_code=200, body={"applied": False})
    result = await bo.post_onboarding_event(uuid4(), "closing_email_sent", datetime.now(UTC))
    assert result is False


async def test_post_event_request_body_shape(monkeypatch: pytest.MonkeyPatch):
    tenant_id = uuid4()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"applied": True})
    at = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)
    next_retry_at = datetime(2026, 7, 25, 10, 0, tzinfo=UTC)

    await bo.post_onboarding_event(tenant_id, "retry_nudge_sent", at, next_retry_at)

    path, kwargs = calls["posts"][0]
    assert path == f"/internal/onboarding/tenants/{tenant_id}/events"
    assert kwargs["json"] == {
        "event": "retry_nudge_sent",
        "at": at.isoformat(),
        "next_retry_at": next_retry_at.isoformat(),
    }


async def test_post_event_next_retry_at_none_serializes_to_null(monkeypatch: pytest.MonkeyPatch):
    calls = _install_fake_client(monkeypatch, status_code=200, body={"applied": True})
    tenant_id = uuid4()
    at = datetime(2026, 7, 18, 10, 0, tzinfo=UTC)

    await bo.post_onboarding_event(tenant_id, "manual_review_flagged", at, None)

    _path, kwargs = calls["posts"][0]
    assert kwargs["json"]["next_retry_at"] is None
