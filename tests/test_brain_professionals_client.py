"""Tests for services/brain_professionals.py — the professional-email lookup.

Mirrors the fail-soft contract its sibling `services/brain_onboarding.py`
already follows: every ambiguity (unconfigured, network error, non-200, bad
JSON, bad shape) answers `None` rather than raising, so the caller can tell
"we could not find out" apart from "nobody is linked" (`{}`) — and neither
ever crashes a booking's post-hooks.
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

from secretaria.services import brain_professionals  # noqa: E402
from secretaria.services.brain_professionals import fetch_professional_emails  # noqa: E402

TENANT_ID = uuid4()
PROFESSIONAL_ID = uuid4()
DOCTOR_EMAIL = "ana@clinica.example"


@pytest.fixture(autouse=True)
def _configured(monkeypatch: pytest.MonkeyPatch):
    """Base URL / key set, so each test isolates the failure mode it is about."""
    settings = brain_professionals.get_settings()
    monkeypatch.setattr(settings, "BRAIN_API_BASE_URL", "https://brain.test", raising=False)
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "pair-key", raising=False)
    monkeypatch.setattr(settings, "BRAIN_API_TIMEOUT_SECONDS", 1.0, raising=False)
    yield


def _transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Route the module's httpx client at `handler`; return the seen requests."""
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


async def test_returns_the_mapping_and_sends_the_pair_key(monkeypatch):
    seen = _transport(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={"items": [{"professional_id": str(PROFESSIONAL_ID), "email": DOCTOR_EMAIL}]},
        ),
    )

    result = await fetch_professional_emails(TENANT_ID)

    assert result == {str(PROFESSIONAL_ID): DOCTOR_EMAIL}
    assert seen[0].url.path == f"/internal/tenants/{TENANT_ID}/professional-emails"
    assert seen[0].headers["X-Internal-Api-Key"] == "pair-key"


async def test_empty_items_is_an_answer_not_a_failure(monkeypatch):
    """`{}` means "brain-api says nobody is linked" — distinct from `None`."""
    _transport(monkeypatch, lambda r: httpx.Response(200, json={"items": []}))
    assert await fetch_professional_emails(TENANT_ID) == {}


async def test_a_malformed_row_is_skipped_not_fatal(monkeypatch):
    """One bad row must not blind the caller to every other professional —
    the same per-item skip `brain_onboarding` applies to its tenant list."""
    _transport(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={
                "items": [
                    {"professional_id": str(PROFESSIONAL_ID), "email": DOCTOR_EMAIL},
                    {"professional_id": "", "email": "orphan@x.test"},
                    {"professional_id": str(uuid4())},  # no email
                    "not-a-dict",
                ]
            },
        ),
    )
    assert await fetch_professional_emails(TENANT_ID) == {str(PROFESSIONAL_ID): DOCTOR_EMAIL}


@pytest.mark.parametrize("status", [401, 403, 404, 500, 502])
async def test_non_200_is_none(monkeypatch, status):
    _transport(monkeypatch, lambda r: httpx.Response(status, json={}))
    assert await fetch_professional_emails(TENANT_ID) is None


async def test_network_error_is_none(monkeypatch):
    def _boom(request):
        raise httpx.ConnectError("unreachable", request=request)

    _transport(monkeypatch, _boom)
    assert await fetch_professional_emails(TENANT_ID) is None


async def test_invalid_json_is_none(monkeypatch):
    _transport(monkeypatch, lambda r: httpx.Response(200, content=b"not json"))
    assert await fetch_professional_emails(TENANT_ID) is None


@pytest.mark.parametrize("body", [{"items": "nope"}, {"wrong": []}, []])
async def test_bad_shape_is_none(monkeypatch, body):
    _transport(monkeypatch, lambda r: httpx.Response(200, json=body))
    assert await fetch_professional_emails(TENANT_ID) is None


@pytest.mark.parametrize("missing", ["BRAIN_API_BASE_URL", "INTERNAL_API_KEY"])
async def test_unconfigured_is_none_without_calling_out(monkeypatch, missing):
    settings = brain_professionals.get_settings()
    monkeypatch.setattr(settings, missing, "", raising=False)
    seen = _transport(monkeypatch, lambda r: httpx.Response(200, json={"items": []}))

    assert await fetch_professional_emails(TENANT_ID) is None
    assert seen == []  # never even attempted


async def test_never_logs_an_address(monkeypatch, caplog):
    """Count-only observability: the tenant id and how many are linked, never
    who they are — the rule `brain_onboarding` keeps for `owner_email`."""
    _transport(
        monkeypatch,
        lambda r: httpx.Response(
            200,
            json={"items": [{"professional_id": str(PROFESSIONAL_ID), "email": DOCTOR_EMAIL}]},
        ),
    )

    with caplog.at_level(logging.INFO):
        await fetch_professional_emails(TENANT_ID)

    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert DOCTOR_EMAIL not in logged
