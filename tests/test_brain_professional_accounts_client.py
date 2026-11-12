"""services/brain_professionals.py::fetch_professional_accounts (TASK-083, spec §6).

brain-api answers a BARE JSON list `[{"professional_id", "account_id"}]` (not `{items: [...]}`).
Same fail-soft contract as `fetch_professional_emails`: `None` on any ambiguity, `{}` for a tenant
nobody is linked to.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from secretaria.services import brain_professionals  # noqa: E402
from secretaria.services.brain_professionals import fetch_professional_accounts  # noqa: E402

TENANT_ID = uuid4()
PROFESSIONAL_ID = uuid4()
ACCOUNT_ID = uuid4()


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    settings = brain_professionals.get_settings()
    monkeypatch.setattr(settings, "BRAIN_API_BASE_URL", "https://brain.test", raising=False)
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "pair-key", raising=False)
    monkeypatch.setattr(settings, "BRAIN_API_TIMEOUT_SECONDS", 1.0, raising=False)
    yield


def _transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(brain_professionals.httpx, "AsyncClient", _factory)
    return seen


async def test_parses_the_bare_list_and_sends_the_pair_key(monkeypatch):
    seen = _transport(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json=[{"professional_id": str(PROFESSIONAL_ID), "account_id": str(ACCOUNT_ID).upper()}],
        ),
    )
    result = await fetch_professional_accounts(TENANT_ID)
    # account ids are canonicalised (lower-case uuid) so the same person always yields one key
    assert result == {str(PROFESSIONAL_ID): str(ACCOUNT_ID)}
    assert seen[0].url.path == f"/internal/tenants/{TENANT_ID}/professional-accounts"
    assert seen[0].headers["X-Internal-Api-Key"] == "pair-key"


async def test_empty_list_is_an_answer_not_a_failure(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(200, json=[]))
    assert await fetch_professional_accounts(TENANT_ID) == {}


async def test_malformed_rows_are_skipped_not_fatal(monkeypatch):
    _transport(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json=[
                "junk",
                {"professional_id": str(PROFESSIONAL_ID)},
                {"professional_id": "not-a-uuid", "account_id": str(ACCOUNT_ID)},
                {"professional_id": str(uuid4()), "account_id": "not-a-uuid"},
                {"professional_id": str(PROFESSIONAL_ID), "account_id": str(ACCOUNT_ID)},
            ],
        ),
    )
    assert await fetch_professional_accounts(TENANT_ID) == {str(PROFESSIONAL_ID): str(ACCOUNT_ID)}


async def test_unconfigured_returns_none_without_calling(monkeypatch):
    settings = brain_professionals.get_settings()
    monkeypatch.setattr(settings, "BRAIN_API_BASE_URL", "", raising=False)
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json=[]))
    assert await fetch_professional_accounts(TENANT_ID) is None
    assert seen == []


@pytest.mark.parametrize("status", [401, 403, 404, 500, 503])
async def test_non_200_returns_none(monkeypatch, status):
    _transport(monkeypatch, lambda r: httpx.Response(status, json=[]))
    assert await fetch_professional_accounts(TENANT_ID) is None


async def test_network_error_and_timeout_return_none(monkeypatch):
    def boom(request):
        raise httpx.ConnectTimeout("slow", request=request)

    _transport(monkeypatch, boom)
    assert await fetch_professional_accounts(TENANT_ID) is None


async def test_invalid_json_and_wrapped_shape_return_none(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(200, content=b"<html>"))
    assert await fetch_professional_accounts(TENANT_ID) is None
    # The contract is a bare list; the `{items: ...}` shape of professional-emails is NOT accepted.
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"items": []}))
    assert await fetch_professional_accounts(TENANT_ID) is None
