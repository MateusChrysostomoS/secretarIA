"""Tests for services/brain_staff.py — "does this clinic have a working secretary?".

TASK-052 Task 16 (TASK-067 s06). brain-api answers `GET /internal/tenants/{id}/staff-roles`
-> `{"roles": [...]}`; True/False by the presence of "secretary"; None on EVERY ambiguity
(unconfigured, network, timeout, non-200 incl. 404/5xx, bad JSON, bad shape). None means
"unknown" and the caller falls back to the doctor's closing time (owner rule). No cache.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")

import logging  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from secretaria.services import brain_staff  # noqa: E402
from secretaria.services.brain_staff import fetch_has_secretary  # noqa: E402

TENANT_ID = uuid4()


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    settings = brain_staff.get_settings()
    monkeypatch.setattr(settings, "BRAIN_API_BASE_URL", "https://brain.test", raising=False)
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "pair-key", raising=False)
    monkeypatch.setattr(settings, "BRAIN_API_TIMEOUT_SECONDS", 1.0, raising=False)
    yield


CLIENT_KWARGS: list[dict] = []


def _transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Route the module's httpx client at `handler`; return the seen requests (the client's
    constructor kwargs land in CLIENT_KWARGS)."""
    seen: list[httpx.Request] = []
    CLIENT_KWARGS.clear()

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        CLIENT_KWARGS.append(dict(kwargs))
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(brain_staff.httpx, "AsyncClient", _factory)
    return seen


def _roles(*roles):
    return lambda r: httpx.Response(200, json={"roles": list(roles)})


async def test_true_with_a_secretary_and_the_pair_key_and_the_timeout_are_sent(monkeypatch):
    seen = _transport(monkeypatch, _roles("doctor", "secretary"))

    assert await fetch_has_secretary(TENANT_ID) is True

    assert seen[0].method == "GET"
    assert seen[0].url.path == f"/internal/tenants/{TENANT_ID}/staff-roles"
    assert seen[0].headers["X-Internal-Api-Key"] == "pair-key"
    assert CLIENT_KWARGS[0]["timeout"] == 1.0


@pytest.mark.parametrize(
    "roles",
    [
        ("secretary", "secretary"),  # duplicated
        ("manager", "doctor", "secretary"),
    ],
)
async def test_true_whatever_the_other_roles(monkeypatch, roles):
    _transport(monkeypatch, _roles(*roles))
    assert await fetch_has_secretary(TENANT_ID) is True


@pytest.mark.parametrize("roles", [(), ("doctor",), ("doctor", "manager"), ("Secretary",)])
async def test_false_without_a_secretary(monkeypatch, roles):
    """Absent role = False (an answer), including an empty clinic; the match is exact."""
    _transport(monkeypatch, _roles(*roles))
    assert await fetch_has_secretary(TENANT_ID) is False


async def test_none_when_unconfigured_and_no_request_is_made(monkeypatch):
    seen = _transport(monkeypatch, _roles("secretary"))
    monkeypatch.setattr(brain_staff.get_settings(), "INTERNAL_API_KEY", "", raising=False)
    assert await fetch_has_secretary(TENANT_ID) is None
    monkeypatch.setattr(brain_staff.get_settings(), "INTERNAL_API_KEY", "pair-key", raising=False)
    monkeypatch.setattr(brain_staff.get_settings(), "BRAIN_API_BASE_URL", "", raising=False)
    assert await fetch_has_secretary(TENANT_ID) is None
    assert seen == []


@pytest.mark.parametrize("status_code", [401, 403, 404, 422, 500, 502, 503])
async def test_none_on_any_non_200(monkeypatch, status_code):
    _transport(monkeypatch, lambda r: httpx.Response(status_code, json={"roles": ["secretary"]}))
    assert await fetch_has_secretary(TENANT_ID) is None


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("refused"),
        httpx.ReadTimeout("slow"),
        httpx.ConnectTimeout("slow"),
    ],
)
async def test_none_on_network_errors_and_timeouts(monkeypatch, error):
    def _raise(request):
        raise error

    _transport(monkeypatch, _raise)
    assert await fetch_has_secretary(TENANT_ID) is None


async def test_none_on_invalid_json(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(200, content=b"<html>oops</html>"))
    assert await fetch_has_secretary(TENANT_ID) is None


@pytest.mark.parametrize(
    "body",
    [
        [],
        ["secretary"],
        {},
        {"roles": None},
        {"roles": "secretary"},
        {"roles": [None]},
        {"roles": ["secretary", 7]},
        {"items": ["secretary"]},
    ],
)
async def test_none_on_a_bad_shape(monkeypatch, body):
    """A body the contract does not describe is unknown, never a guessed True/False."""
    _transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    assert await fetch_has_secretary(TENANT_ID) is None


async def test_every_call_asks_again_no_cache(monkeypatch):
    answers = iter([["secretary"], ["doctor"]])
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"roles": next(answers)}))
    assert await fetch_has_secretary(TENANT_ID) is True
    assert await fetch_has_secretary(TENANT_ID) is False
    assert len(seen) == 2


async def test_logs_carry_no_url_key_or_body(monkeypatch, caplog, capsys):
    """Only the tenant id, a reason and a status/type: never the key, the URL or the body.
    structlog may render to stdout or to the logging records: both are checked."""
    caplog.set_level(logging.DEBUG)

    def _raise(request):
        raise httpx.ConnectError("boom https://brain.test/internal?pair-key")

    _transport(monkeypatch, _raise)
    await fetch_has_secretary(TENANT_ID)
    _transport(monkeypatch, lambda r: httpx.Response(200, content=b"secret-body-ana@x.com"))
    await fetch_has_secretary(TENANT_ID)

    captured = capsys.readouterr()
    text = "\n".join(
        [captured.out, captured.err, caplog.text, *(r.getMessage() for r in caplog.records)]
    )
    assert "staff_roles_fetch_failed" in text  # the capture works: the failures were logged
    assert "pair-key" not in text and "brain.test" not in text
    assert "ana@x.com" not in text and "secret-body" not in text
