"""Tests for services/payments/asaas.py using httpx.MockTransport — no real
network call is ever made. Every httpx.AsyncClient the module constructs is
routed through a MockTransport by monkeypatching `httpx.AsyncClient` inside
the module's own namespace (which IS the shared `httpx` module object) to
inject `transport=`, so production code paths (headers, JSON body
serialization, status handling) run for real against a fake wire.
"""

import json as jsonlib

import httpx
import pytest

from secretaria.services.payments import asaas as asaas_module
from secretaria.services.payments.asaas import AsaasClient, AsaasError


def _install_transport(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    transport = httpx.MockTransport(handler)
    real_async_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(asaas_module.httpx, "AsyncClient", _factory)


# --------------------------------------------------------------------------
# Header injection
# --------------------------------------------------------------------------


async def test_access_token_header_injected(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["access_token"] = request.headers.get("access_token")
        return httpx.Response(200, json={"id": "cus_123"})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("my-secret-asaas-key")
    customer_id = await client.create_customer("Maria", "5511999999999")

    assert customer_id == "cus_123"
    assert captured["access_token"] == "my-secret-asaas-key"


async def test_create_customer_omits_mobile_phone_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"id": "cus_456"})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    await client.create_customer("Maria", None)

    assert captured["body"] == {"name": "Maria"}


# --------------------------------------------------------------------------
# Cents -> value conversion
# --------------------------------------------------------------------------


async def test_create_pix_payment_converts_cents_to_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"id": "pay_1", "status": "PENDING"})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    result = await client.create_pix_payment("cus_1", 25050, "appt-1", "Sinal", "2026-07-21")

    assert captured["body"]["value"] == 250.5
    assert captured["body"]["billingType"] == "PIX"
    assert captured["body"]["customer"] == "cus_1"
    assert captured["body"]["externalReference"] == "appt-1"
    assert captured["body"]["dueDate"] == "2026-07-21"
    assert result == {"id": "pay_1", "status": "PENDING"}


async def test_create_pix_payment_rounds_to_two_decimals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"id": "pay_2"})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    await client.create_pix_payment("cus_1", 3333, "appt-2", "Sinal", "2026-07-21")

    assert captured["body"]["value"] == 33.33


# --------------------------------------------------------------------------
# Refund with / without value
# --------------------------------------------------------------------------


async def test_refund_with_value(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content)
        return httpx.Response(200, json={"id": "pay_1", "status": "REFUNDED"})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    await client.refund_payment("pay_1", 5000)

    assert captured["body"] == {"value": 50.0}


async def test_refund_without_value_omits_key_for_full_refund(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = jsonlib.loads(request.content) if request.content else {}
        return httpx.Response(200, json={"id": "pay_1", "status": "REFUNDED"})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    await client.refund_payment("pay_1", None)

    assert captured["body"] == {}
    assert "value" not in captured["body"]


# --------------------------------------------------------------------------
# get_pix_qr tolerates absent fields; delete_payment uses DELETE
# --------------------------------------------------------------------------


async def test_get_pix_qr_tolerates_missing_fields(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        return httpx.Response(200, json={})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    result = await client.get_pix_qr("pay_1")

    assert result == {}


async def test_get_pix_qr_returns_unknown_extra_fields_untouched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "payload": "00020126...copiaecola...6304ABCD",
                "encodedImage": "base64data",
                "expirationDate": "2026-07-21 23:59:59",
                "someBrandNewFieldAsaasAddedLater": {"nested": True},
            },
        )

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    result = await client.get_pix_qr("pay_1")

    assert result["payload"] == "00020126...copiaecola...6304ABCD"
    assert result["someBrandNewFieldAsaasAddedLater"] == {"nested": True}


async def test_delete_payment_uses_delete_method(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path.endswith("/payments/pay_1")
        return httpx.Response(200, json={"deleted": True, "id": "pay_1"})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    result = await client.delete_payment("pay_1")

    assert result == {"deleted": True, "id": "pay_1"}


# --------------------------------------------------------------------------
# Non-2xx -> AsaasError WITHOUT body text leakage
# --------------------------------------------------------------------------


async def test_non_2xx_raises_asaas_error_with_status_code(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"errors": [{"code": "not_found"}]})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    with pytest.raises(AsaasError) as exc_info:
        await client.get_pix_qr("pay_missing")

    assert exc_info.value.status_code == 404
    assert exc_info.value.code_hint == "not_found"


async def test_non_2xx_never_leaks_response_body_text(monkeypatch: pytest.MonkeyPatch) -> None:
    secret_marker = "SUPER_SENSITIVE_INTERNAL_DETAIL_98765"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "errors": [
                    {
                        "code": "invalid_action",
                        "description": secret_marker,
                        "message": secret_marker,
                    }
                ]
            },
        )

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    with pytest.raises(AsaasError) as exc_info:
        await client.create_customer("Maria", None)

    assert exc_info.value.status_code == 400
    assert secret_marker not in str(exc_info.value)
    assert secret_marker not in repr(exc_info.value)
    # The short symbolic code IS surfaced (not the free-text description).
    assert exc_info.value.code_hint == "invalid_action"


async def test_non_2xx_with_unparseable_body_still_raises_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"<html>Internal Server Error</html>")

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    with pytest.raises(AsaasError) as exc_info:
        await client.delete_payment("pay_1")

    assert exc_info.value.status_code == 500
    assert exc_info.value.code_hint is None
    assert "<html>" not in str(exc_info.value)
