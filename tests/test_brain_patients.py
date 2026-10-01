"""Patient contact lookup fails softly and keeps contact data out of logs."""

import json
import os
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from secretaria.services import brain_patients  # noqa: E402


@pytest.fixture
def configured(monkeypatch):
    settings = SimpleNamespace(
        BRAIN_API_BASE_URL="http://brain.test",
        INTERNAL_API_KEY="test-internal-key",
        BRAIN_API_TIMEOUT_SECONDS=2,
    )
    monkeypatch.setattr(brain_patients, "get_settings", lambda: settings)
    return settings


def _mock(monkeypatch, handler):
    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def _client(*args, **kwargs):
        return real(*args, transport=transport, **kwargs)

    monkeypatch.setattr(brain_patients.httpx, "AsyncClient", _client)


async def test_posts_tenant_and_uuid_handle_with_internal_auth(configured, monkeypatch):
    tenant_id, external_id = uuid4(), str(uuid4())

    def _response(request):
        assert request.method == "POST"
        assert str(request.url) == "http://brain.test/internal/brain-message/patient-contact"
        assert request.headers["X-Internal-Api-Key"] == "test-internal-key"
        assert json.loads(request.content) == {
            "tenant_id": str(tenant_id), "external_id": external_id
        }
        assert request.extensions["timeout"]["read"] == 2
        return httpx.Response(200, json={"email": "  patient@example.test  "})

    _mock(monkeypatch, _response)
    result = await brain_patients.fetch_patient_email(tenant_id, external_id)
    assert result == "patient@example.test"


@pytest.mark.parametrize("status", [404, 401, 403, 422, 500])
async def test_non_200_is_none(configured, monkeypatch, status):
    _mock(monkeypatch, lambda request: httpx.Response(status))
    assert await brain_patients.fetch_patient_email(uuid4(), str(uuid4())) is None


@pytest.mark.parametrize("body", [{"email": None}, {}, {"email": " "}, {"email": 42}, [], "bad"])
async def test_unusable_response_is_none(configured, monkeypatch, body):
    _mock(monkeypatch, lambda request: httpx.Response(200, json=body))
    assert await brain_patients.fetch_patient_email(uuid4(), str(uuid4())) is None


async def test_invalid_json_is_none(configured, monkeypatch):
    _mock(monkeypatch, lambda request: httpx.Response(200, content=b"not json"))
    assert await brain_patients.fetch_patient_email(uuid4(), str(uuid4())) is None


@pytest.mark.parametrize("error", [httpx.ConnectError, httpx.ReadTimeout])
async def test_network_error_is_none_without_logging_exception_text(
    configured, monkeypatch, capsys, error
):
    def _boom(request):
        raise error("patient@example.test secret-contact-payload")

    _mock(monkeypatch, _boom)
    assert await brain_patients.fetch_patient_email(uuid4(), str(uuid4())) is None
    output = capsys.readouterr()
    assert "patient@example.test" not in output.out + output.err
    assert "secret-contact-payload" not in output.out + output.err


@pytest.mark.parametrize("missing", ["BRAIN_API_BASE_URL", "INTERNAL_API_KEY"])
async def test_unconfigured_does_not_make_a_request(configured, monkeypatch, missing):
    setattr(configured, missing, "")

    def _unexpected(request):
        pytest.fail("Unconfigured lookup must not send a request")

    _mock(monkeypatch, _unexpected)
    assert await brain_patients.fetch_patient_email(uuid4(), str(uuid4())) is None


@pytest.mark.parametrize(
    "status,body,available,email",
    [
        (200, {"email": None}, True, None),
        (200, {"email": " "}, True, None),
        (200, {"email": " patient@example.test "}, True, "patient@example.test"),
        (404, None, False, None),
        (200, {}, False, None),
        (200, {"email": 42}, False, None),
    ],
)
async def test_result_distinguishes_authority_from_unavailable(
    configured, monkeypatch, status, body, available, email
):
    _mock(monkeypatch, lambda request: httpx.Response(status, json=body))
    result = await brain_patients.fetch_patient_email_result(uuid4(), str(uuid4()))
    assert result.available is available
    assert result.email == email
