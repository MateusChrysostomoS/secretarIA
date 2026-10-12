"""Asaas behind the `PaymentProvider` contract (services/payments/provider.py).

The wire lives in `asaas.py` (`AsaasClient`); this module translates Asaas' vocabulary to the
provider-neutral types and back. Nothing outside this module and `asaas.py` may read an
Asaas payload.

`client_for` is the ONE construction seam for `AsaasClient` (tests monkeypatch it): it is
looked up at CALL time, never captured at import.

Asaas facts this wrapper encodes (docs.asaas.com, verified 2026-09-29, see the spec's [VER]):
  * the webhook is authenticated by a static per-clinic token in the `asaas-access-token`
    header - not an HMAC of the body, so it is a cheap filter and `get_charge` stays the
    authority before any money moves;
  * a Pix charge has a DUE DATE (a day), not a lifetime in minutes, and the QR stays payable
    for months: the slot-hold expiry is ours (`cancel_charge` voids the charge);
  * `RECEIVED` is the paid state of a Pix; `RECEIVED_IN_CASH` (the clinic marked it as cash)
    is deliberately NOT a Pix payment and maps to `OTHER`;
  * `OVERDUE` (the day after the due date) and `DELETED` both mean "no longer payable" here.
"""

import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from secretaria.core.logging import get_logger
from secretaria.services.payments.asaas import AsaasClient, AsaasError
from secretaria.services.payments.provider import (
    ChargeRequest,
    ChargeState,
    CreatedCharge,
    NormalizedPaymentEvent,
    PaymentAccount,
    PaymentStatus,
    ProviderError,
)

logger = get_logger(__name__)

_ASAAS_TZ = ZoneInfo("America/Sao_Paulo")

_EVENT_STATUS = {
    "PAYMENT_RECEIVED": PaymentStatus.PAID,
    "PAYMENT_CONFIRMED": PaymentStatus.PAID,
    "PAYMENT_OVERDUE": PaymentStatus.EXPIRED,
    "PAYMENT_DELETED": PaymentStatus.EXPIRED,
    "PAYMENT_REFUNDED": PaymentStatus.REFUNDED,
    "PAYMENT_PARTIALLY_REFUNDED": PaymentStatus.REFUNDED,
}

_PAYMENT_STATUS = {
    "PENDING": PaymentStatus.PENDING,
    "RECEIVED": PaymentStatus.PAID,
    "CONFIRMED": PaymentStatus.PAID,
    "OVERDUE": PaymentStatus.EXPIRED,
    "REFUNDED": PaymentStatus.REFUNDED,
    "REFUND_REQUESTED": PaymentStatus.REFUNDED,
    "REFUND_IN_PROGRESS": PaymentStatus.REFUNDED,
}


def client_for(api_key: str) -> AsaasClient:
    """Construction seam so tests can install a fake recording client."""
    return AsaasClient(api_key)


def _provider_error(exc: AsaasError, code: str) -> ProviderError:
    retryable = exc.status_code == 0 or exc.status_code == 429 or exc.status_code >= 500
    return ProviderError(code, status_code=exc.status_code or None, retryable=retryable)


def _cents(value: object) -> int | None:
    """Asaas sends reais as a JSON number; go through Decimal so 59.9 is 5990, not 5989."""
    if value is None:
        return None
    try:
        return int((Decimal(str(value)) * 100).to_integral_value())
    except (InvalidOperation, ValueError):
        return None


def _parse_dt(value: object) -> datetime | None:
    """Asaas timestamps are Brasilia time: 'YYYY-MM-DD HH:MM:SS' or a bare 'YYYY-MM-DD'."""
    if not isinstance(value, str) or not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=_ASAAS_TZ).astimezone(UTC)
        except ValueError:
            continue
    return None


def _charge_status(payment: Mapping) -> PaymentStatus:
    if payment.get("deleted") is True:
        return PaymentStatus.EXPIRED
    return _PAYMENT_STATUS.get(str(payment.get("status") or ""), PaymentStatus.OTHER)


def _refunds(payment: Mapping) -> str:
    """The refund entries Asaas lists on the payment, if any (TASK-052). The exact shape is NOT
    PROVEN until the sandbox: anything unexpected is "unreadable", which never reuses a deposit."""
    if payment.get("refunds") is None:
        return "absent"
    refunds = payment.get("refunds")
    if not isinstance(refunds, list):
        return "unreadable"
    return "some" if refunds else "none"


class AsaasProvider:
    name = "asaas"

    @staticmethod
    def _client(account: PaymentAccount) -> AsaasClient:
        if not account.api_key:
            raise ProviderError("no_api_key")
        return client_for(account.api_key)

    async def create_pix_charge(
        self, account: PaymentAccount, request: ChargeRequest
    ) -> CreatedCharge:
        client = self._client(account)

        customer_id = request.provider_customer_id
        if not customer_id:
            try:
                if request.payer_document:
                    customer_id = await client.create_customer(
                        request.payer_name, request.payer_phone, cpf_cnpj=request.payer_document
                    )
                else:
                    customer_id = await client.create_customer(
                        request.payer_name, request.payer_phone
                    )
            except AsaasError as exc:
                # Asaas' reference lists cpfCnpj as required for a customer: a 400 on a request
                # that carried no document is the signal the caller must collect one (F2).
                missing_document = exc.status_code == 400 and not request.payer_document
                raise _provider_error(
                    exc, "payer_document_required" if missing_document else "customer_failed"
                ) from exc
            if not customer_id:
                raise ProviderError("customer_missing_id")

        try:
            payment = await client.create_pix_payment(
                customer_id,
                request.amount_cents,
                request.external_reference,
                request.description[:255],
                request.due_date.isoformat(),
            )
        except AsaasError as exc:
            raise _provider_error(exc, "charge_failed") from exc

        charge_id = payment.get("id")
        if not charge_id:
            raise ProviderError("charge_missing_id")

        copy_paste: str | None = None
        expires_at: datetime | None = None
        try:
            qr = await client.get_pix_qr(str(charge_id))
            copy_paste = qr.get("payload") or None
            expires_at = _parse_dt(qr.get("expirationDate"))
        except Exception as exc:  # noqa: BLE001 - the QR is best-effort by contract
            logger.warning("asaas_qr_failed", error_type=type(exc).__name__)

        return CreatedCharge(
            provider_charge_id=str(charge_id),
            copy_paste=copy_paste,
            expires_at=expires_at,
            provider_customer_id=customer_id,
        )

    def verify_webhook(
        self,
        account: PaymentAccount,
        headers: Mapping[str, str],
        raw_body: bytes,
        query: Mapping[str, str] | None = None,
    ) -> bool:
        expected = account.webhook_token
        if not expected:
            return False
        received = {key.lower(): value for key, value in headers.items()}.get("asaas-access-token")
        if not received:
            return False
        return hmac.compare_digest(received.encode("utf-8"), expected.encode("utf-8"))

    def parse_webhook(
        self, raw_body: bytes, headers: Mapping[str, str] | None = None
    ) -> list[NormalizedPaymentEvent]:
        try:
            payload = json.loads(raw_body)
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValueError("not a JSON body") from exc
        if not isinstance(payload, dict):
            raise ValueError("not a JSON object")

        payment = payload.get("payment")
        event_type = str(payload.get("event") or "")
        if not event_type or not isinstance(payment, dict) or payment.get("id") is None:
            return []  # some other Asaas event on the same URL: nothing for the payments core

        charge_id = str(payment["id"])
        status = _EVENT_STATUS.get(event_type, PaymentStatus.OTHER)
        paid_at = _parse_dt(payload.get("dateCreated")) if status is PaymentStatus.PAID else None
        return [
            NormalizedPaymentEvent(
                provider=self.name,
                event_id=str(payload.get("id") or f"{event_type}:{charge_id}"),
                provider_charge_id=charge_id,
                status=status,
                paid_amount_cents=_cents(payment.get("value")),
                paid_at=paid_at,
                external_reference=payment.get("externalReference") or None,
                event_type=event_type,
            )
        ]

    async def get_charge(self, account: PaymentAccount, provider_charge_id: str) -> ChargeState:
        try:
            payment = await self._client(account).get_payment(provider_charge_id)
        except AsaasError as exc:
            raise _provider_error(exc, "get_charge_failed") from exc
        return ChargeState(
            status=_charge_status(payment),
            paid_amount_cents=_cents(payment.get("value")),
            external_reference=payment.get("externalReference") or None,
            paid_at=_parse_dt(payment.get("paymentDate") or payment.get("confirmedDate")),
            refunds=_refunds(payment),
        )

    async def delete_customer(self, account: PaymentAccount, provider_customer_id: str) -> None:
        """LGPD erasure of the clinic-side customer record. Idempotent (404 = already gone).
        An optional capability, not part of the `PaymentProvider` protocol: the erasure job
        calls it only when the provider has it. (Endpoint proved in sandbox check S16.)"""
        try:
            await self._client(account).delete_customer(provider_customer_id)
        except AsaasError as exc:
            if exc.status_code == 404:
                return
            raise _provider_error(exc, "delete_customer_failed") from exc

    async def cancel_charge(self, account: PaymentAccount, provider_charge_id: str) -> None:
        client = self._client(account)
        try:
            await client.delete_payment(provider_charge_id)
            return
        except AsaasError as exc:
            if exc.status_code == 404:
                return  # already gone: DELETE is idempotent from our side
            failure = _provider_error(exc, "cancel_failed")
            original = exc

        # Any other refusal: it may just be a repeated DELETE of a charge that is already
        # deleted (Asaas' answer for that is not documented). Ask before failing.
        try:
            payment = await client.get_payment(provider_charge_id)
        except AsaasError:
            raise failure from original
        if payment.get("deleted") is True:
            return
        raise failure from original
