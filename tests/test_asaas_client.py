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


async def test_a_missing_document_answer_is_flagged_without_leaking_the_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The real sandbox answer when a customer without cpfCnpj is charged (TASK-067).
    text = "Para criar esta cobrança é necessário preencher o CPF ou CNPJ do cliente."

    def handler(request: httpx.Request) -> httpx.Response:
        body = {"errors": [{"code": "invalid_object", "description": text}]}
        return httpx.Response(400, json=body)

    _install_transport(monkeypatch, handler)
    with pytest.raises(AsaasError) as exc_info:
        await AsaasClient("key").create_pix_payment("cus_1", 6000, "ref", "Sinal", "2026-10-26")
    assert exc_info.value.document_missing is True
    assert exc_info.value.code_hint == "invalid_object"
    assert text not in str(exc_info.value) and text not in repr(exc_info.value)


async def test_an_unrelated_400_is_not_flagged_as_a_missing_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def other(request: httpx.Request) -> httpx.Response:
        body = {"errors": [{"code": "invalid_object", "description": "Valor mínimo é R$ 5,00."}]}
        return httpx.Response(400, json=body)

    _install_transport(monkeypatch, other)
    with pytest.raises(AsaasError) as unrelated:
        await AsaasClient("key").create_pix_payment("cus_1", 100, "ref", "Sinal", "2026-10-26")
    assert unrelated.value.document_missing is False


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


# --------------------------------------------------------------------------
# TASK-016: no refund, cpfCnpj, get_payment, webhook management, probe
# --------------------------------------------------------------------------


def test_client_has_no_refund_method_decision_d15() -> None:
    assert not any("refund" in name.lower() for name in dir(AsaasClient))


async def test_create_customer_sends_cpf_cnpj_only_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(jsonlib.loads(request.content))
        return httpx.Response(200, json={"id": "cus_1"})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    await client.create_customer("Maria", "5511999999999")
    await client.create_customer("Maria", "5511999999999", cpf_cnpj="52998224725")

    assert "cpfCnpj" not in bodies[0]
    assert bodies[1]["cpfCnpj"] == "52998224725"


async def test_get_payment_uses_get_and_returns_the_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"], captured["path"] = request.method, request.url.path
        return httpx.Response(
            200, json={"id": "pay_1", "status": "RECEIVED", "value": 60.0, "externalReference": "r"}
        )

    _install_transport(monkeypatch, handler)
    data = await AsaasClient("key").get_payment("pay_1")

    assert (captured["method"], captured["path"]) == ("GET", "/v3/payments/pay_1")
    assert data["status"] == "RECEIVED"


async def test_webhook_management_paths_and_bodies(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = jsonlib.loads(request.content) if request.content else None
        seen.append((request.method, request.url.path, dict(request.url.params), body))
        return httpx.Response(200, json={"id": "wh_1", "data": []})

    _install_transport(monkeypatch, handler)
    client = AsaasClient("key")
    await client.create_webhook({"name": "n", "url": "https://x/y"})
    await client.list_webhooks()
    await client.update_webhook("wh_1", {"interrupted": False})
    await client.delete_webhook("wh_1")
    await client.delete_customer("cus_1")

    assert seen == [
        ("POST", "/v3/webhooks", {}, {"name": "n", "url": "https://x/y"}),
        ("GET", "/v3/webhooks", {"limit": "100"}, None),
        ("PUT", "/v3/webhooks/wh_1", {}, {"interrupted": False}),
        ("DELETE", "/v3/webhooks/wh_1", {}, None),
        ("DELETE", "/v3/customers/cus_1", {}, None),
    ]


async def test_probe_returns_classification_inputs_and_never_raises_on_http_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = iter(
        [
            httpx.Response(200, json={"data": [], "hasMore": False}),
            httpx.Response(
                403, json={"errors": [{"code": "insufficient_permission", "description": "TEXT"}]}
            ),
            httpx.Response(401, json={}),
            httpx.Response(429, headers={"Retry-After": "2"}, json={}),
            httpx.Response(503, text="<html>down</html>"),
            httpx.Response(200, text="not json"),
        ]
    )
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, dict(request.url.params)))
        return next(replies)

    _install_transport(monkeypatch, handler)
    client = AsaasClient("secret-key-value")
    results = [await client.probe("/transfers", expect_list=True) for _ in range(6)]

    assert [(r.status_code, r.code_hint, r.json_ok, r.retry_after) for r in results] == [
        (200, None, True, None),
        (403, "insufficient_permission", False, None),
        (401, None, False, None),
        (429, None, False, 2.0),
        (503, None, False, None),
        (200, None, False, None),
    ]
    assert requests[0] == ("GET", "/v3/transfers", {"limit": "1"})
    assert "TEXT" not in repr(results)


async def test_probe_maps_a_connection_error_to_status_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("boom", request=request)

    _install_transport(monkeypatch, handler)
    result = await AsaasClient("key").probe("/customers")

    assert (result.status_code, result.code_hint, result.json_ok) == (0, "connection_error", False)
