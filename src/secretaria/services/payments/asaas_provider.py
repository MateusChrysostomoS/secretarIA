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

import asyncio
import hmac
import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.services.payments.asaas import AsaasClient, AsaasError
from secretaria.services.payments.failure_reason import FailureReason, classify
from secretaria.services.payments.money import ASAAS_MIN_PIX_CENTS
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


def _provider_error(exc: AsaasError, code: str, stage: str | None = None) -> ProviderError:
    """`code` is the old symbolic code (callers depend on it); with a `stage` the error also
    carries the closed-vocabulary reason, from status + code hint + fixed keyword only."""
    retryable = exc.status_code == 0 or exc.status_code == 429 or exc.status_code >= 500
    reason = classify(stage, exc.status_code, exc.code_hint, exc.text_reason) if stage else None
    return ProviderError(
        code,
        status_code=exc.status_code or None,
        retryable=retryable,
        reason=reason,
        stage=stage,
        code_hint=exc.code_hint,
    )


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


def _paid_moment(value: object) -> datetime | None:
    """`_parse_dt` for a payment date: a bare 'YYYY-MM-DD' carries no time, so it would read as
    midnight. Today's date is the moment we observe it (now); an older one is noon of that day."""
    parsed = _parse_dt(value)
    if parsed is None or not isinstance(value, str) or len(value) != 10:
        return parsed
    now = datetime.now(UTC)
    if parsed.astimezone(_ASAAS_TZ).date() == now.astimezone(_ASAAS_TZ).date():
        return now
    return parsed + timedelta(hours=12)


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
            raise ProviderError("no_api_key", reason=FailureReason.api_key_invalid, stage="config")
        return client_for(account.api_key)

    async def create_pix_charge(
        self, account: PaymentAccount, request: ChargeRequest
    ) -> CreatedCharge:
        client = self._client(account)

        customer_id = request.provider_customer_id
        # Only a customer created by THIS call may ever be deleted on failure; a stored one
        # (from an earlier charge of the same patient) is never touched.
        created_here = not customer_id
        if created_here:
            try:
                if request.payer_document:
                    # Only the clinic's own test charge asks Asaas not to notify the payer.
                    quiet = request.notifications_disabled
                    extra = {"notification_disabled": True} if quiet else {}
                    customer_id = await client.create_customer(
                        request.payer_name,
                        request.payer_phone,
                        cpf_cnpj=request.payer_document,
                        **extra,
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
                    exc,
                    "payer_document_required" if missing_document else "customer_failed",
                    "customer",
                ) from exc
            if not customer_id:
                raise ProviderError(
                    "customer_missing_id", reason=FailureReason.unknown, stage="customer"
                )

        try:
            payment = await client.create_pix_payment(
                customer_id,
                request.amount_cents,
                request.external_reference,
                request.description[:255],
                request.due_date.isoformat(),
            )
        except AsaasError as exc:
            # Sandbox fact: the customer was created WITHOUT cpfCnpj (Asaas allows it) and only
            # the charge is refused for it. Same signal as above: the caller must collect one.
            missing_document = exc.document_missing and not request.payer_document
            failure = _provider_error(
                exc,
                "payer_document_required" if missing_document else "charge_failed",
                "charge",
            )
            # Measured (docs/ASAAS_MEDICOES.md): Asaas refuses a Pix charge under R$ 5,00 with a
            # plain 400. Its text is never read, so the amount we sent is the evidence.
            if (
                exc.status_code == 400
                and request.amount_cents < ASAAS_MIN_PIX_CENTS
                and failure.reason == FailureReason.charge_rejected
            ):
                failure.reason = FailureReason.value_too_low
            # The customer made a moment ago has no charge: do not leave it behind in the
            # clinic's own Asaas account (a retry creates a fresh one, WITH the CPF if asked).
            if created_here:
                await self._discard_customer(client, customer_id)
            raise failure from exc

        charge_id = payment.get("id")
        if not charge_id:
            if created_here:
                await self._discard_customer(client, customer_id)
            raise ProviderError("charge_missing_id", reason=FailureReason.unknown, stage="charge")

        copy_paste, expires_at = await self._fetch_pix_qr(client, str(charge_id))

        return CreatedCharge(
            provider_charge_id=str(charge_id),
            copy_paste=copy_paste,
            expires_at=expires_at,
            provider_customer_id=customer_id,
            customer_created_here=created_here,
        )

    @staticmethod
    async def _fetch_pix_qr(
        client: AsaasClient, charge_id: str
    ) -> tuple[str | None, datetime | None]:
        """The QR is best-effort by contract (the caller turns "no code" into `qr_unavailable`),
        but the charge already exists, so the QR gets ONE second chance: Asaas can lag in
        serving it. A second attempt happens when the first raised OR answered without a
        `payload`; the pause between them is `ASAAS_QR_RETRY_DELAY_SECONDS`.

        Each failed attempt logs `asaas_qr_failed` with the attempt number, the HTTP `status` and
        symbolic `code_hint` (only when it was an AsaasError) and the error type - never the
        body text, the charge id, or anything about the payer."""
        for attempt in (1, 2):
            status: int | None = None
            code_hint: str | None = None
            try:
                qr = await client.get_pix_qr(charge_id)
                payload = qr.get("payload") or None
                if payload:
                    return payload, _parse_dt(qr.get("expirationDate"))
                error_type = "no_payload"
            except Exception as exc:  # noqa: BLE001 - the QR is best-effort by contract
                error_type = type(exc).__name__
                if isinstance(exc, AsaasError):
                    status, code_hint = exc.status_code or None, exc.code_hint
            logger.warning(
                "asaas_qr_failed",
                attempt=attempt,
                status=status,
                code_hint=code_hint,
                error_type=error_type,
            )
            if attempt == 1:
                await asyncio.sleep(max(0.0, get_settings().ASAAS_QR_RETRY_DELAY_SECONDS))
        return None, None

    @staticmethod
    async def _discard_customer(client: AsaasClient, customer_id: str) -> None:
        """Best effort: a failure here is logged by type only and never replaces the error
        the caller is about to raise."""
        try:
            await client.delete_customer(customer_id)
        except Exception as exc:  # noqa: BLE001 - the original failure is what matters
            logger.warning("asaas_customer_cleanup_failed", error_type=type(exc).__name__)

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
            paid_at=_paid_moment(payment.get("paymentDate") or payment.get("confirmedDate")),
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
