"""Closed vocabulary of Pix charge-failure reasons (Plano 2, Task 1).

Every failed charge is recorded as stage + reason + HTTP status, never free text. The reason is a
member of `FailureReason`; whatever does not match becomes `unknown`.
"""

import re

import httpx
import pytest

from secretaria.services.payments import asaas as asaas_module
from secretaria.services.payments.asaas import AsaasClient, AsaasError, _classify_text
from secretaria.services.payments.failure_reason import FailureReason, classify, user_text
from secretaria.services.payments.provider import ProviderError

R = FailureReason


@pytest.mark.parametrize(
    ("stage", "status", "code_hint", "text_reason", "expected"),
    [
        ("charge", 401, None, None, R.api_key_invalid),
        ("customer", 401, "invalid_access_token", None, R.api_key_invalid),
        ("charge", 403, None, None, R.permission_denied),
        ("charge", 429, None, None, R.rate_limited),
        ("charge", 503, None, None, R.provider_unavailable),
        ("customer", 500, None, None, R.provider_unavailable),
        ("charge", 0, "connection_error", None, R.provider_unavailable),
        ("customer", 400, "invalid_object", None, R.customer_rejected),
        ("customer", 400, None, "document", R.customer_document_invalid),
        ("charge", 400, "invalid_object", None, R.charge_rejected),
        ("charge", 400, None, "value", R.value_too_low),
        ("charge", 400, None, "pix_key", R.pix_key_missing),
        ("charge", 400, None, "account", R.account_not_enabled),
        ("charge", 400, None, "something-else", R.charge_rejected),
        ("charge", 404, None, None, R.unknown),
        ("charge", 422, None, None, R.unknown),
        ("qr", 400, None, None, R.unknown),
    ],
)
def test_classify_by_status(stage, status, code_hint, text_reason, expected) -> None:
    assert classify(stage, status, code_hint, text_reason) is expected


def test_status_rules_win_over_text_reason() -> None:
    assert classify("charge", 401, None, "pix_key") is R.api_key_invalid
    assert classify("charge", 503, None, "value") is R.provider_unavailable


def test_classify_never_returns_free_text() -> None:
    stages = ["customer", "charge", "qr", "", "free text with CPF 123.456.789-09"]
    statuses = [-1, 0, 200, 400, 401, 403, 404, 429, 500, 599]
    hints = [None, "", "invalid_object", "Maria da Silva maria@example.com"]
    texts = [None, "", "document", "value", "pix_key", "account", "Maria 12345678909"]
    for stage in stages:
        for status in statuses:
            for hint in hints:
                for text in texts:
                    result = classify(stage, status, hint, text)
                    assert isinstance(result, FailureReason)


def test_user_text_covers_every_reason() -> None:
    for reason in FailureReason:
        text = user_text(reason)
        assert isinstance(text, str) and text.strip()
        assert "@" not in text
        assert not re.search(r"\d", text)


def test_user_text_examples() -> None:
    assert user_text(R.pix_key_missing) == (
        "A conta do Asaas ainda não tem uma chave Pix cadastrada."
    )
    assert user_text(R.qr_unavailable) == (
        "O Asaas criou a cobrança, mas não devolveu o código Pix. "
        "Normalmente falta cadastrar uma chave Pix na conta do Asaas."
    )


def test_reason_values_are_the_agreed_symbols() -> None:
    assert {r.value for r in FailureReason} == {
        "customer_rejected",
        "customer_document_invalid",
        "charge_rejected",
        "value_too_low",
        "account_not_enabled",
        "pix_key_missing",
        "qr_unavailable",
        "api_key_invalid",
        "permission_denied",
        "rate_limited",
        "provider_unavailable",
        "persist_failed",
        "no_account",
        "unknown",
    }


def test_provider_error_carries_reason_and_stage_defaulting_to_none() -> None:
    plain = ProviderError("create_failed", status_code=400)
    assert plain.reason is None and plain.stage is None
    rich = ProviderError("create_failed", status_code=400, reason=R.pix_key_missing, stage="charge")
    assert rich.reason is R.pix_key_missing and rich.stage == "charge"
    assert str(rich) == "payment provider error: create_failed"


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(asaas_module.httpx, "AsyncClient", _factory)


def test_classify_text_detects_only_the_document_for_now() -> None:
    doc = httpx.Response(
        400, json={"errors": [{"code": "invalid_object", "description": "Informe o CPF ou CNPJ."}]}
    )
    other = httpx.Response(
        400, json={"errors": [{"code": "invalid_object", "description": "Valor mínimo é 5."}]}
    )
    assert _classify_text(doc) == "document"
    assert _classify_text(other) is None
    assert _classify_text(httpx.Response(400, content=b"<html>")) is None


async def test_asaas_error_does_not_store_description(monkeypatch: pytest.MonkeyPatch) -> None:
    cpf = "123.456.789-09"
    description = f"O CPF {cpf} do cliente Maria é inválido; informe o CPF ou CNPJ."

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400, json={"errors": [{"code": "invalid_object", "description": description}]}
        )

    _install_transport(monkeypatch, handler)
    with pytest.raises(AsaasError) as exc_info:
        await AsaasClient("key").create_pix_payment("cus_1", 6000, "ref", "Sinal", "2026-10-26")
    error = exc_info.value
    assert error.text_reason == "document"
    assert error.document_missing is True
    for needle in (cpf, "123", "Maria", description):
        assert needle not in str(error) and needle not in repr(error)
        assert needle not in repr(vars(error))


async def test_asaas_error_text_reason_is_none_without_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    _install_transport(monkeypatch, handler)
    with pytest.raises(AsaasError) as exc_info:
        await AsaasClient("key").delete_payment("pay_1")
    assert exc_info.value.text_reason is None
