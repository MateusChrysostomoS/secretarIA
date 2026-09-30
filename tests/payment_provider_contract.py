"""The behaviour EVERY `PaymentProvider` must show (spec section 16: "a provider is only
accepted if it passes the suite").

Not collected by itself (no `Test` prefix). A provider's test module subclasses
`ProviderContract` as `class TestXxxProvider(ProviderContract)`, sets `provider` / `account`
and implements the hooks below; every test in this class then runs against it. Mercado Pago
(F3) reuses this file unchanged.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from secretaria.services.payments.provider import (
    PaymentAccount,
    PaymentProvider,
    PaymentStatus,
    ProviderError,
)

CHARGE_ID = "pay_1"
REFERENCE = "ref-1"
AMOUNT = 6000
SECRET_MARKERS = ("api-key-secret", "webhook-token-secret", "access-token-secret")


class ProviderContract:
    provider: PaymentProvider
    account: PaymentAccount

    # ---- hooks: the provider's own wire format --------------------------------------------

    def webhook_request(
        self,
        kind: str,
        *,
        charge_id: str = CHARGE_ID,
        amount_cents: int = AMOUNT,
        reference: str = REFERENCE,
        event_id: str = "evt_1",
    ) -> tuple[dict, bytes, dict]:
        """An AUTHENTIC webhook (headers, raw body, query) of `kind`:
        paid | expired | refunded | other."""
        raise NotImplementedError

    def tampered_headers(self, headers: dict) -> dict:
        """The same headers with the authenticity proof made wrong."""
        raise NotImplementedError

    def stage_charge(
        self, monkeypatch, *, state: str, amount_cents: int = AMOUNT, reference: str = REFERENCE
    ) -> None:
        """Make the provider's network say the charge is `state`: open|paid|expired|refunded."""
        raise NotImplementedError

    def stage_http_failure(self, monkeypatch, status: int) -> None:
        """Make every provider call answer HTTP `status`."""
        raise NotImplementedError

    def stage_cancel(self, monkeypatch, *, outcome: str) -> None:
        """Make cancelling answer `outcome`: ok | already_gone | fails."""
        raise NotImplementedError

    # ---- the contract ----------------------------------------------------------------------

    def test_verify_webhook_accepts_the_authentic_request_and_rejects_everything_else(self):
        headers, body, query = self.webhook_request("paid")
        assert self.provider.verify_webhook(self.account, headers, body, query) is True
        forged = self.tampered_headers(headers)
        assert self.provider.verify_webhook(self.account, forged, body, query) is False
        assert self.provider.verify_webhook(self.account, {}, body, query) is False
        no_secret = replace(self.account, webhook_token=None, webhook_secret=None)
        assert self.provider.verify_webhook(no_secret, headers, body, query) is False

    def test_parse_a_paid_webhook(self):
        headers, body, _ = self.webhook_request("paid")
        events = self.provider.parse_webhook(body, headers)
        assert len(events) == 1
        event = events[0]
        assert event.provider == self.provider.name
        assert event.status is PaymentStatus.PAID
        assert event.provider_charge_id == CHARGE_ID
        assert event.paid_amount_cents == AMOUNT
        assert event.external_reference == REFERENCE
        assert event.event_id
        again = self.provider.parse_webhook(body, headers)[0]
        assert again.event_id == event.event_id  # a replay yields the SAME idempotency key

    @pytest.mark.parametrize(
        ("kind", "status"),
        [("expired", PaymentStatus.EXPIRED), ("refunded", PaymentStatus.REFUNDED)],
    )
    def test_parse_expired_and_refunded_webhooks(self, kind, status):
        headers, body, _ = self.webhook_request(kind)
        events = self.provider.parse_webhook(body, headers)
        assert [e.status for e in events] == [status]

    def test_parse_an_event_the_system_does_not_act_on_is_other_or_nothing(self):
        headers, body, _ = self.webhook_request("other")
        events = self.provider.parse_webhook(body, headers)
        assert all(e.status is PaymentStatus.OTHER for e in events)

    def test_parse_garbage_is_a_value_error(self):
        with pytest.raises(ValueError):
            self.provider.parse_webhook(b"not json at all", {})
        with pytest.raises(ValueError):
            self.provider.parse_webhook(b"[1, 2, 3]", {})

    @pytest.mark.parametrize(
        ("state", "status"),
        [
            ("open", PaymentStatus.PENDING),
            ("paid", PaymentStatus.PAID),
            ("expired", PaymentStatus.EXPIRED),
            ("refunded", PaymentStatus.REFUNDED),
        ],
    )
    async def test_get_charge_maps_every_state(self, monkeypatch, state, status):
        self.stage_charge(monkeypatch, state=state)
        charge = await self.provider.get_charge(self.account, CHARGE_ID)
        assert charge.status is status
        assert charge.external_reference == REFERENCE
        if status is PaymentStatus.PAID:
            assert charge.paid_amount_cents == AMOUNT

    async def test_get_charge_provider_failure_is_a_provider_error_with_retry_semantics(
        self, monkeypatch
    ):
        self.stage_http_failure(monkeypatch, 503)
        with pytest.raises(ProviderError) as server_error:
            await self.provider.get_charge(self.account, CHARGE_ID)
        assert server_error.value.retryable is True

        self.stage_http_failure(monkeypatch, 404)
        with pytest.raises(ProviderError) as not_found:
            await self.provider.get_charge(self.account, CHARGE_ID)
        assert not_found.value.retryable is False
        assert not_found.value.status_code == 404
        for error in (server_error.value, not_found.value):
            assert not any(marker in str(error) for marker in SECRET_MARKERS)

    async def test_cancel_charge_is_idempotent_and_reports_real_failures(self, monkeypatch):
        self.stage_cancel(monkeypatch, outcome="ok")
        assert await self.provider.cancel_charge(self.account, CHARGE_ID) is None
        self.stage_cancel(monkeypatch, outcome="already_gone")
        assert await self.provider.cancel_charge(self.account, CHARGE_ID) is None
        self.stage_cancel(monkeypatch, outcome="fails")
        with pytest.raises(ProviderError) as failure:
            await self.provider.cancel_charge(self.account, CHARGE_ID)
        assert failure.value.retryable is True

    def test_the_provider_has_no_refund_surface(self):
        assert not any("refund" in name.lower() for name in dir(self.provider))

    def test_no_secret_is_visible_in_a_repr(self):
        for marker in SECRET_MARKERS:
            assert marker not in repr(self.account)
            assert marker not in repr(self.provider)
