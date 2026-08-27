"""Tests for services/precheck.py — the precheck hand-off seam.

`request_precheck_handoff` calls brain-api's POST /internal/precheck-handoff.
These tests fake `httpx.AsyncClient` at the module level (mirrors
test_subscription.py), so no real network call is ever made. They cover every
outcome the contract defines, the outbound request shape, and the fail-closed
behaviour on any ambiguity.

The outbound BODY is this file's most load-bearing subject since FEAT 39 added
optional booking context to it. brain-api validates that body with
`extra="forbid"`, so a wrong or unexpected key name there is not a dropped
field - it is a 422 that this module converts into an UNAVAILABLE
indistinguishable from an outage, taking the whole hand-off (including the
automatic post-booking trigger, which needs neither new field) down with it.
Hence `test_a_call_without_booking_context_sends_todays_exact_payload`: proof
that this module deployed alone changes nothing on the wire.
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


# A name of the shape `Patient.name` really carries. No assertion in this file
# may ever find it in a log line - only in the request body.
PATIENT_NAME = "Maria Silva"
BOOKED_SERVICE = "Cirurgia de Catarata"


class _LogRecorder:
    """Records every structlog call - same shape as
    tests/test_precheck_handoff_plugin.py::_LogRecorder.

    `caplog` is blind to these modules (structlog renders through its own
    factory, not stdlib logging), so a leak assertion over `caplog.records`
    would pass against any code at all, including code that logs the name.
    """

    def __init__(self) -> None:
        self.records: list[tuple[str, str, dict]] = []

    def __getattr__(self, level: str):
        def _log(event: str, **fields) -> None:
            self.records.append((level, event, fields))

        return _log

    @property
    def text(self) -> str:
        return "\n".join(f"{lvl} {event} {fields}" for lvl, event, fields in self.records)


@pytest.fixture
def log(monkeypatch: pytest.MonkeyPatch) -> _LogRecorder:
    recorder = _LogRecorder()
    monkeypatch.setattr(precheck, "logger", recorder)
    return recorder


def _posted_body(calls: dict) -> dict:
    """The single JSON body this call put on the wire."""
    assert len(calls["posts"]) == 1
    _path, kwargs = calls["posts"][0]
    return kwargs["json"]


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


# --------------------------------------------------------------------------
# Booking context (FEAT 39): what goes on the wire, and what must not
# --------------------------------------------------------------------------


async def test_booking_context_is_sent_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of the feature: PreCheck opens the questionnaire already knowing
    who booked and what they booked."""
    tenant_id = uuid4()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})

    await precheck.request_precheck_handoff(
        tenant_id,
        "5511999999999",
        patient_name=PATIENT_NAME,
        booked_service=BOOKED_SERVICE,
    )

    assert _posted_body(calls) == {
        "tenant_id": str(tenant_id),
        "phone_number": "5511999999999",
        "patient_name": PATIENT_NAME,
        "booked_service": BOOKED_SERVICE,
    }


async def test_a_call_without_booking_context_sends_todays_exact_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE regression guard for the whole FEAT 37-40 chain.

    This module deploys after brain-api and PreCheck, but nothing enforces that
    at runtime - and every caller that knows neither value (the agent tool
    `iniciar_pre_consulta`, a booking with no type) still comes through here.
    Its payload must remain exactly what it was before FEAT 39: the same two
    keys, no `"patient_name": null` padding, no third key. Anything else is a
    change to a body validated with `extra="forbid"` on the other side.
    """
    tenant_id = uuid4()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})

    await precheck.request_precheck_handoff(tenant_id, "5511999999999")

    assert _posted_body(calls) == {
        "tenant_id": str(tenant_id),
        "phone_number": "5511999999999",
    }


async def test_explicit_none_context_is_the_same_as_omitting_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ctx.patient.name` and `ctx.appointment.appointment_type` are both nullable
    columns, so the caller passes None constantly. None is the ordinary case,
    not an error - and it must not become a null on the wire."""
    tenant_id = uuid4()
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})

    await precheck.request_precheck_handoff(
        tenant_id, "5511999999999", patient_name=None, booked_service=None
    )

    assert _posted_body(calls) == {
        "tenant_id": str(tenant_id),
        "phone_number": "5511999999999",
    }


@pytest.mark.parametrize("blank", ["", "   ", "\n\t "])
async def test_blank_context_is_omitted_rather_than_sent_as_a_value(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """A name that is really whitespace is not a name. Forwarding it would have
    PreCheck greet the patient by an empty string, which reads worse than not
    knowing the name at all."""
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})

    await precheck.request_precheck_handoff(
        uuid4(), "5511999999999", patient_name=blank, booked_service=blank
    )

    body = _posted_body(calls)
    assert "patient_name" not in body
    assert "booked_service" not in body


async def test_each_context_field_travels_independently(monkeypatch: pytest.MonkeyPatch) -> None:
    """Knowing the service but not the name (or the reverse) is a real state - an
    appointment typed by the flow router for a patient whose profile name Meta
    never sent. One missing value must not suppress the other."""
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})

    await precheck.request_precheck_handoff(
        uuid4(), "5511999999999", patient_name=None, booked_service=BOOKED_SERVICE
    )

    body = _posted_body(calls)
    assert "patient_name" not in body
    assert body["booked_service"] == BOOKED_SERVICE


async def test_the_service_name_is_forwarded_raw(monkeypatch: pytest.MonkeyPatch) -> None:
    """Product decision: the clinic's own appointment type, verbatim. No
    casefolding, no canonicalisation, nothing stripped but surrounding
    whitespace - the string the patient saw in the confirmation bubble is the
    string PreCheck receives."""
    calls = _install_fake_client(monkeypatch, status_code=200, body={"status": "seeded"})
    raw = "  Consulta - Retorno (pos-operatorio)  "

    await precheck.request_precheck_handoff(uuid4(), "5511999999999", booked_service=raw)

    assert _posted_body(calls)["booked_service"] == "Consulta - Retorno (pos-operatorio)"


async def test_context_fields_are_keyword_only() -> None:
    """Two adjacent `str | None` with no shape that tells them apart: passed
    positionally, a swap would ship the patient's name as the service they
    booked, and nothing anywhere would raise."""
    import inspect

    params = inspect.signature(precheck.request_precheck_handoff).parameters
    for name in ("patient_name", "booked_service"):
        assert params[name].kind is inspect.Parameter.KEYWORD_ONLY
        assert params[name].default is None


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"status_code": 200, "body": {"status": "seeded"}}, "seeded"),
        ({"status_code": 200, "body": {"status": "already_active"}}, "already_active"),
        ({"status_code": 200, "body": {"status": "nonsense"}}, "unexpected_status_body"),
        ({"status_code": 200, "body": _BAD_JSON}, "invalid_json"),
        ({"status_code": 403, "body": {"detail": "precheck_not_entitled"}}, "not_entitled"),
        ({"status_code": 404, "body": {"detail": "no_clinic_for_tenant"}}, "no_clinic"),
        ({"status_code": 409, "body": {"detail": "conflicting_active_session"}}, "conflict"),
        ({"status_code": 503, "body": None}, "unavailable"),
        ({"raise_exc": httpx.ConnectError("boom")}, "network_error"),
    ],
)
async def test_the_patient_name_never_reaches_a_log_line(
    monkeypatch: pytest.MonkeyPatch, log: _LogRecorder, kwargs: dict, why: str
) -> None:
    """The name now travels through this function, so "we never log it" stopped
    being true by default and became something to hold.

    Every outcome, including the four failure paths that are the tempting ones
    to over-log: a rendered exception, an unparseable body, an unexpected
    status. Only the hashed phone, the status code and the exception string may
    appear.
    """
    _install_fake_client(monkeypatch, **kwargs)

    await precheck.request_precheck_handoff(
        uuid4(),
        "5511999999999",
        patient_name=PATIENT_NAME,
        booked_service=BOOKED_SERVICE,
    )

    assert log.records, f"the {why} path must say what it did"
    assert PATIENT_NAME not in log.text
    assert "5511999999999" not in log.text


async def test_result_is_frozen_dataclass() -> None:
    import dataclasses

    result = HandoffResult(HandoffOutcome.SEEDED)
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.outcome = HandoffOutcome.UNAVAILABLE  # type: ignore[misc]
