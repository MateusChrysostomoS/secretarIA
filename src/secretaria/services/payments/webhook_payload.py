"""What a webhook row stores: a minimised copy of the provider body plus the normalised event.

LGPD (spec sections 6.3 and 14): the provider's body carries the payer (name, document,
e-mail, phone, address) and receipt URLs. None of it is needed to act on a payment - we match
by charge id, never by who paid - so it is removed BEFORE the row is written. Only ids,
amounts, references and statuses remain, and even those are set to NULL after 30 days by the
daily job. The row also carries the `normalized` event, so processing never has to re-parse a
provider payload (and keeps working for every provider the same way).
"""

from datetime import datetime
from typing import Any

from secretaria.services.payments.provider import NormalizedPaymentEvent, PaymentStatus

# Compared lower-cased, at any depth.
_PERSONAL_KEYS = frozenset(
    {
        "payer",
        "customer",
        "cpfcnpj",
        "cpf",
        "cnpj",
        "email",
        "name",
        "phone",
        "mobilephone",
        "phonenumber",
        "address",
        "addressnumber",
        "complement",
        "province",
        "postalcode",
        "holder",
        "holdername",
        "description",
        "invoiceurl",
        "bankslipurl",
        "transactionreceipturl",
    }
)


def minimize_payload(payload: Any) -> Any:
    """A copy of `payload` without any personal-data key, at any depth."""
    if isinstance(payload, dict):
        return {
            key: minimize_payload(value)
            for key, value in payload.items()
            if str(key).lower() not in _PERSONAL_KEYS
        }
    if isinstance(payload, list):
        return [minimize_payload(item) for item in payload]
    return payload


def event_to_dict(event: NormalizedPaymentEvent) -> dict[str, Any]:
    return {
        "provider": event.provider,
        "event_id": event.event_id,
        "provider_charge_id": event.provider_charge_id,
        "status": event.status.value,
        "paid_amount_cents": event.paid_amount_cents,
        "paid_at": event.paid_at.isoformat() if event.paid_at else None,
        "external_reference": event.external_reference,
        "event_type": event.event_type,
    }


def event_from_dict(data: dict[str, Any]) -> NormalizedPaymentEvent:
    paid_at = data.get("paid_at")
    return NormalizedPaymentEvent(
        provider=data["provider"],
        event_id=data["event_id"],
        provider_charge_id=data["provider_charge_id"],
        status=PaymentStatus(data["status"]),
        paid_amount_cents=data.get("paid_amount_cents"),
        paid_at=datetime.fromisoformat(paid_at) if paid_at else None,
        external_reference=data.get("external_reference"),
        event_type=data.get("event_type", ""),
    )
