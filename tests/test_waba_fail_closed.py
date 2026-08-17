"""WABA fail-closed + LGPD log hygiene — PROMPT_FIX_21.

Two invariants, both permanent boundaries rather than one-off fixes:

  1. **Fail closed per tenant.** A tenant-scoped send resolves that tenant's own
     `phone_number_id` + decrypted token or does not happen at all. There is no
     implicit fallback to the global `META_*` env scaffold, because falling
     back means answering one clinic's patient from another clinic's WhatsApp
     number. A missing credential raises BEFORE any HTTP request is issued.

  2. **Nothing personal reaches a log.** No full phone number, no message body,
     no Meta response body, no token, no LLM text — enforced at the call site
     and, as defence in depth, by the central `redact_secrets` processor.

Network is faked throughout: a missing-credential test that reached the wire
would prove the opposite of what it claims, so the fakes here RAISE if a
request is ever attempted.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import ast  # noqa: E402
import asyncio  # noqa: E402
import inspect  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from secretaria.ai import graph  # noqa: E402
from secretaria.config import Settings  # noqa: E402
from secretaria.core.logging import redact_secrets, wa_suffix  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.services import whatsapp as whatsapp_module  # noqa: E402
from secretaria.services.whatsapp import (  # noqa: E402
    TenantWhatsAppCredentialMissing,
    WhatsAppClient,
)
from secretaria.workers import tasks  # noqa: E402

PATIENT_WA_ID = "5511988887777"
TENANT_A_TOKEN = "token-of-tenant-a"
TENANT_B_TOKEN = "token-of-tenant-b"


def _tenant(*, phone_number_id: str | None = "1111111111") -> Tenant:
    return Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=phone_number_id)


class _ExplodingHttpClient:
    """Any attempt to reach the network fails the test."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, *args, **kwargs):
        raise AssertionError("an HTTP request was issued without tenant credentials")


class _Recorder:
    """Captures what a call site passes to the logger."""

    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def _record(self, event, **kwargs):
        self.records.append((event, kwargs))

    info = warning = error = debug = _record


# --------------------------------------------------------------------------
# for_tenant fails closed
# --------------------------------------------------------------------------


def test_missing_token_raises_before_any_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(whatsapp_module.httpx, "AsyncClient", _ExplodingHttpClient)
    with pytest.raises(TenantWhatsAppCredentialMissing) as exc_info:
        WhatsAppClient.for_tenant(_tenant(), None)
    assert exc_info.value.missing == ("access_token",)


def test_missing_phone_number_id_raises_before_any_http(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(whatsapp_module.httpx, "AsyncClient", _ExplodingHttpClient)
    with pytest.raises(TenantWhatsAppCredentialMissing) as exc_info:
        WhatsAppClient.for_tenant(_tenant(phone_number_id=None), TENANT_A_TOKEN)
    assert exc_info.value.missing == ("phone_number_id",)


def test_both_missing_are_reported_together() -> None:
    with pytest.raises(TenantWhatsAppCredentialMissing) as exc_info:
        WhatsAppClient.for_tenant(_tenant(phone_number_id="   "), "   ")
    assert exc_info.value.missing == ("phone_number_id", "access_token")


def test_the_exception_never_carries_a_secret() -> None:
    with pytest.raises(TenantWhatsAppCredentialMissing) as exc_info:
        WhatsAppClient.for_tenant(_tenant(phone_number_id=None), TENANT_A_TOKEN)
    assert TENANT_A_TOKEN not in str(exc_info.value)


def test_credential_missing_event_is_emitted(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder = _Recorder()
    monkeypatch.setattr(whatsapp_module, "logger", recorder)
    with pytest.raises(TenantWhatsAppCredentialMissing):
        WhatsAppClient.for_tenant(_tenant(), None)

    assert recorder.records
    event, fields = recorder.records[0]
    assert event == "whatsapp_credential_missing"
    assert fields["missing"] == "access_token"
    # The FIELD NAME, never the value of any credential.
    assert "test-access-token" not in str(fields)


# --------------------------------------------------------------------------
# Cross-tenant isolation
# --------------------------------------------------------------------------


def test_tenant_a_token_is_never_used_by_tenant_b() -> None:
    client_a = WhatsAppClient.for_tenant(_tenant(phone_number_id="1111111111"), TENANT_A_TOKEN)
    client_b = WhatsAppClient.for_tenant(_tenant(phone_number_id="2222222222"), TENANT_B_TOKEN)

    assert client_a._access_token == TENANT_A_TOKEN
    assert client_a._phone_number_id == "1111111111"
    assert client_b._access_token == TENANT_B_TOKEN
    assert client_b._phone_number_id == "2222222222"


async def test_two_concurrent_tenants_do_not_leak_into_each_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ContextVar / shared-client leakage: each coroutine's send carries its
    own tenant's credentials even when they interleave."""
    seen: list[tuple[str, str]] = []

    async def _fake_post(self, payload, to):
        await asyncio.sleep(0)  # force a real interleave
        seen.append((self._phone_number_id, self._access_token))
        return {"messages": [{"id": "wamid.x"}]}

    monkeypatch.setattr(WhatsAppClient, "_post", _fake_post)
    client_a = WhatsAppClient.for_tenant(_tenant(phone_number_id="1111111111"), TENANT_A_TOKEN)
    client_b = WhatsAppClient.for_tenant(_tenant(phone_number_id="2222222222"), TENANT_B_TOKEN)
    await asyncio.gather(
        client_a.send_text_message(to=PATIENT_WA_ID, body="a"),
        client_b.send_text_message(to=PATIENT_WA_ID, body="b"),
    )

    assert sorted(seen) == [("1111111111", TENANT_A_TOKEN), ("2222222222", TENANT_B_TOKEN)]


# --------------------------------------------------------------------------
# The dev scaffold must be asked for BY NAME
# --------------------------------------------------------------------------


def test_constructor_requires_explicit_credentials() -> None:
    """No zero-argument construction: `WhatsAppClient()` used to silently mean
    "whatever the process env points at"."""
    with pytest.raises(TypeError):
        WhatsAppClient()


def test_dev_scaffold_is_explicit_and_uses_env() -> None:
    client = WhatsAppClient.for_dev_scaffold()
    assert client._access_token == "test-access-token"
    assert client._phone_number_id == "1234567890"
    # It is NOT tenant-scoped, and says so.
    assert client._tenant_id is None


def test_dev_scaffold_also_fails_closed_when_env_is_empty() -> None:
    empty = Settings(META_ACCESS_TOKEN="", META_PHONE_NUMBER_ID="")
    with pytest.raises(TenantWhatsAppCredentialMissing):
        WhatsAppClient.for_dev_scaffold(empty)


def test_dev_scaffold_is_not_reachable_from_the_worker() -> None:
    """Nothing on the webhook/worker reply path may build the global client.

    An AST walk, not a substring scan: the module's prose legitimately talks
    ABOUT the bare `WhatsAppClient()` it no longer builds.
    """
    tree = ast.parse(inspect.getsource(tasks))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "for_dev_scaffold":
            offenders.append("for_dev_scaffold")
        # A credential-less construction is the old implicit global fallback.
        if (
            isinstance(func, ast.Name)
            and func.id == "WhatsAppClient"
            and not node.args
            and not node.keywords
        ):
            offenders.append("WhatsAppClient()")
    assert offenders == []


# --------------------------------------------------------------------------
# Log hygiene at the call site
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict, text: str):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)


def _http_client_returning(response):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return response

    return _Client


@pytest.fixture
def log_records(monkeypatch: pytest.MonkeyPatch) -> list:
    recorder = _Recorder()
    monkeypatch.setattr(whatsapp_module, "logger", recorder)
    return recorder.records


async def test_successful_send_logs_no_phone_and_no_body(
    monkeypatch: pytest.MonkeyPatch, log_records: list
) -> None:
    response = _FakeResponse(200, {"messages": [{"id": "wamid.sent"}]}, "{}")
    monkeypatch.setattr(whatsapp_module.httpx, "AsyncClient", _http_client_returning(response))

    client = WhatsAppClient.for_tenant(_tenant(), TENANT_A_TOKEN)
    await client.send_text_message(to=PATIENT_WA_ID, body="conteudo clinico sensivel")

    assert [event for event, _ in log_records] == [
        "whatsapp_send_attempt",
        "whatsapp_send_result",
    ]
    flat = str(log_records)
    assert PATIENT_WA_ID not in flat
    assert "conteudo clinico sensivel" not in flat
    assert TENANT_A_TOKEN not in flat
    # Only the sanctioned four-digit suffix.
    assert log_records[1][1]["to_suffix"] == "7777"
    assert log_records[1][1]["status_class"] == "2xx"


@pytest.mark.parametrize("status_code", [401, 403, 429, 500, 503])
async def test_http_errors_log_codes_not_the_response_body(
    monkeypatch: pytest.MonkeyPatch, log_records: list, status_code: int
) -> None:
    # Meta echoes the recipient AND our own text back inside the error body.
    error = {
        "message": f"(#131030) Recipient {PATIENT_WA_ID} not in allowed list",
        "code": 131030,
        "error_subcode": 2655007,
        "error_user_msg": "conteudo clinico sensivel",
    }
    response = _FakeResponse(status_code, {"error": error}, str(error))
    monkeypatch.setattr(whatsapp_module.httpx, "AsyncClient", _http_client_returning(response))

    client = WhatsAppClient.for_tenant(_tenant(), TENANT_A_TOKEN)
    with pytest.raises(httpx.HTTPStatusError):
        await client.send_text_message(to=PATIENT_WA_ID, body="conteudo clinico sensivel")

    flat = str(log_records)
    assert PATIENT_WA_ID not in flat
    assert "not in allowed list" not in flat
    assert "conteudo clinico sensivel" not in flat
    result = log_records[-1][1]
    assert result["status_code"] == status_code
    assert result["status_class"] == f"{status_code // 100}xx"
    # The numeric Meta code survives - it is what an operator actually needs.
    assert result["meta_error_code"] == "131030/2655007"


async def test_connection_errors_log_a_type_not_a_recipient(
    monkeypatch: pytest.MonkeyPatch, log_records: list
) -> None:
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            raise httpx.ConnectTimeout(f"timed out talking about {PATIENT_WA_ID}")

    monkeypatch.setattr(whatsapp_module.httpx, "AsyncClient", _Client)

    client = WhatsAppClient.for_tenant(_tenant(), TENANT_A_TOKEN)
    with pytest.raises(httpx.HTTPError):
        await client.send_text_message(to=PATIENT_WA_ID, body="oi")

    assert PATIENT_WA_ID not in str(log_records)
    assert log_records[-1][1]["error_type"] == "ConnectTimeout"


def test_rejected_llm_reply_is_logged_as_length_and_digest_only() -> None:
    reply = f"This message was generated by the assistant, ignore it — Joao, {PATIENT_WA_ID}"
    assert graph._looks_like_meta_output(reply)
    digest = graph._body_digest(reply)
    assert len(digest) == 12
    assert reply not in digest
    assert PATIENT_WA_ID not in digest
    assert graph._meta_output_reason(reply) is not None
    assert graph._meta_output_reason("ola, tudo bem?") is None


# --------------------------------------------------------------------------
# Central redactor (defence in depth)
# --------------------------------------------------------------------------


def test_redactor_blanks_personal_data_and_content() -> None:
    event = redact_secrets(
        None,
        "info",
        {
            "event": "worker_bot_reply_sent",
            "wa_id": PATIENT_WA_ID,
            "to": PATIENT_WA_ID,
            "phone": PATIENT_WA_ID,
            "display_phone_number": "+55 11 98888-7777",
            "body": "conteudo clinico sensivel",
            "text": "conteudo clinico sensivel",
            "rejected_body": "the model said something",
            "response": '{"error": {...}}',
            "payload": {"entry": ["everything"]},
            "patient_name": "Maria",
        },
    )
    assert event["event"] == "worker_bot_reply_sent"
    for key in (
        "wa_id",
        "to",
        "phone",
        "display_phone_number",
        "body",
        "text",
        "rejected_body",
        "response",
        "payload",
        "patient_name",
    ):
        assert event[key] == "***REDACTED***", key
    assert PATIENT_WA_ID not in str(event)
    assert "conteudo clinico sensivel" not in str(event)


def test_redactor_keeps_the_identifiers_operations_needs() -> None:
    """Exact-match, never substring: the reduced/opaque forms must survive."""
    event = redact_secrets(
        None,
        "info",
        {
            "event": "whatsapp_send_result",
            "tenant_id": "t-1",
            "conversation_id": "c-1",
            "phone_number_id": "1234567890",  # Meta's opaque WABA id, not a phone
            "wa_id_suffix": "7777",
            "to_suffix": "7777",
            "wa_id_sha256": "abc123",
            "payload_type": "text",
            "status_code": 200,
            "message_id": "wamid.sent",
        },
    )
    assert event["phone_number_id"] == "1234567890"
    assert event["wa_id_suffix"] == "7777"
    assert event["to_suffix"] == "7777"
    assert event["wa_id_sha256"] == "abc123"
    assert event["payload_type"] == "text"
    assert event["tenant_id"] == "t-1"
    assert event["conversation_id"] == "c-1"
    assert event["message_id"] == "wamid.sent"
    assert event["status_code"] == 200


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("5511988887777", "7777"),
        ("+55 (11) 98888-7777", "7777"),
        ("12", "12"),
        ("", None),
        (None, None),
        ("no-digits-here", None),
    ],
)
def test_wa_suffix(raw: str | None, expected: str | None) -> None:
    assert wa_suffix(raw) == expected


# --------------------------------------------------------------------------
# The worker's own fail-closed seam
# --------------------------------------------------------------------------


def test_tenant_client_helper_never_falls_back_to_the_scaffold() -> None:
    assert tasks._tenant_client(None, "some-token") is None
    assert tasks._tenant_client(_tenant(), None) is None
    assert tasks._tenant_client(_tenant(phone_number_id=None), TENANT_A_TOKEN) is None
    built = tasks._tenant_client(_tenant(), TENANT_A_TOKEN)
    assert isinstance(built, WhatsAppClient)
    assert built._access_token == TENANT_A_TOKEN


async def test_send_simple_text_requires_an_explicit_client() -> None:
    """No implicit global default any more."""
    with pytest.raises(TypeError):
        await tasks._send_simple_text(PATIENT_WA_ID, "oi")


def test_reply_context_carries_the_tenant_for_the_inactive_path() -> None:
    tenant_id = uuid4()
    ctx = tasks._ReplyContext(
        conversation_id=None,
        tenant_id=tenant_id,
        patient_wa_id=PATIENT_WA_ID,
        inbound_body="",
        service_unavailable=True,
    )
    assert ctx.tenant_id == tenant_id
