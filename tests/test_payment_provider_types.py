"""The provider contract's value types and the registry (TASK-016, spec section 5.1)."""

import inspect
from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from secretaria.services.payments import registry
from secretaria.services.payments.provider import (
    ChargeRequest,
    ChargeState,
    CreatedCharge,
    NormalizedPaymentEvent,
    PaymentAccount,
    PaymentProvider,
    PaymentStatus,
    ProviderError,
)

_SECRET = "SECRET-VALUE-0123456789"


def test_payment_status_values_are_stable():
    assert [s.value for s in PaymentStatus] == ["PAID", "EXPIRED", "REFUNDED", "PENDING", "OTHER"]


def test_account_repr_and_str_never_show_a_secret():
    account = PaymentAccount(
        tenant_id=uuid4(),
        provider="asaas",
        webhook_slug="slug",
        api_key=_SECRET,
        webhook_token=_SECRET + "-w",
        access_token=_SECRET + "-a",
        webhook_secret=_SECRET + "-s",
    )
    for rendered in (repr(account), str(account), f"{account}", f"{account!r}"):
        assert _SECRET not in rendered
    assert account.api_key == _SECRET  # the value itself is still usable by the client


def test_charge_request_repr_hides_the_payer_document_and_email():
    request = ChargeRequest(
        amount_cents=6000,
        external_reference="ref",
        description="Sinal",
        due_date=date(2026, 9, 29),
        payer_name="Maria",
        payer_document="52998224725",
        payer_email="maria@example.test",
    )
    assert "52998224725" not in repr(request)
    assert "maria@example.test" not in repr(request)


def test_provider_error_carries_a_code_and_never_free_text():
    error = ProviderError("charge_failed", status_code=500, retryable=True)
    assert error.code == "charge_failed"
    assert error.status_code == 500
    assert error.retryable is True
    assert str(error) == "payment provider error: charge_failed"
    assert ProviderError("x").retryable is False


def test_value_types_are_frozen():
    state = ChargeState(status=PaymentStatus.PAID, paid_amount_cents=6000)
    with pytest.raises(FrozenInstanceError):
        state.paid_amount_cents = 1
    event = NormalizedPaymentEvent(
        provider="asaas",
        event_id="evt",
        provider_charge_id="pay_1",
        status=PaymentStatus.PAID,
        paid_at=datetime(2026, 9, 29, tzinfo=UTC),
    )
    assert event.paid_amount_cents is None
    assert event.external_reference is None
    assert event.event_type == ""
    created = CreatedCharge(provider_charge_id="pay_1", copy_paste=None)
    assert created.provider_customer_id is None


def test_protocol_has_exactly_the_five_methods_and_no_refund():
    methods = {
        name
        for name, member in inspect.getmembers(PaymentProvider)
        if not name.startswith("_") and callable(member)
    }
    assert methods == {
        "create_pix_charge",
        "verify_webhook",
        "parse_webhook",
        "get_charge",
        "cancel_charge",
    }
    assert not any("refund" in name.lower() for name in dir(PaymentProvider))


class _Dummy:
    name = "dummy"


def test_registry_find_get_and_unknown(monkeypatch):
    monkeypatch.setitem(registry._PROVIDERS, "dummy", _Dummy())
    assert registry.find_provider("dummy").name == "dummy"
    assert registry.get_provider("dummy").name == "dummy"
    assert registry.find_provider("nope") is None
    with pytest.raises(registry.UnknownProviderError):
        registry.get_provider("nope")
    assert registry.find_provider("") is None
    assert registry.find_provider("../etc") is None
