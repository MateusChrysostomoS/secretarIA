"""The payment-provider contract: what the rest of the system may know about a PSP.

Everything outside `services/payments/<provider>*.py` reads only the types below. A
provider wrapper (Asaas today, Mercado Pago in F3) translates its own wire format to and
from them; nothing else in the codebase touches a provider payload.

There is NO refund method, on purpose (decision D15): the system never returns money -
not manually, not automatically, on any provider. A paid charge that has no consultation
becomes an ATTENTION ITEM the clinic resolves in the provider's own panel
(`services/payments/attention.py`). `cancel_charge` only ever voids a charge that was NOT
paid; callers `get_charge` first.

Secrets: `PaymentAccount` holds decrypted credentials IN MEMORY only, for the duration of
one call. Secret fields are `repr=False` so an accidental `logger.info(account=account)`
or a traceback cannot print them (the structlog redactor is the second line of defence).
`ProviderError` carries a short symbolic `code`, never a response body.
"""

import enum
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Protocol, runtime_checkable
from uuid import UUID


class PaymentStatus(str, enum.Enum):  # noqa: UP042 - StrEnum would change str(member)
    """Provider-neutral state of a charge or of a webhook event.

    `PENDING` exists for `get_charge` (an open, unpaid charge); a webhook event is never
    `PENDING`. `OTHER` is everything the system does not act on (chargeback, cash receipt,
    a restored charge, ...): it is logged and ignored, never guessed at.
    """

    PAID = "PAID"
    EXPIRED = "EXPIRED"
    REFUNDED = "REFUNDED"
    PENDING = "PENDING"
    OTHER = "OTHER"


class ProviderError(Exception):
    """A provider call failed. `code` is symbolic; `retryable` says a retry may succeed
    (network error, HTTP 429, HTTP 5xx). The message never includes a response body."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retryable: bool = False,
        reason: str | None = None,
        stage: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.retryable = retryable
        # Closed-vocabulary failure reason (`failure_reason.FailureReason` value) and the step
        # that failed ("customer" / "charge" / ...). Both stay None until the provider classifies.
        self.reason = reason
        self.stage = stage
        super().__init__(f"payment provider error: {code}")


@dataclass(frozen=True)
class PaymentAccount:
    """One clinic's decrypted credentials for one provider. Build ONLY through
    `services/tenant_config.py::get_payment_account` (the single decrypt point)."""

    tenant_id: UUID
    provider: str
    webhook_slug: str | None = None
    external_account_id: str | None = None
    api_key: str | None = field(default=None, repr=False)
    webhook_token: str | None = field(default=None, repr=False)
    access_token: str | None = field(default=None, repr=False)
    webhook_secret: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ChargeRequest:
    """What `create_pix_charge` needs. `payer_document` / `payer_email` are TRANSIENT: they
    are sent to the provider and never persisted by us (decision D7)."""

    amount_cents: int
    external_reference: str
    description: str
    due_date: date
    payer_name: str
    payer_phone: str | None = None
    payer_document: str | None = field(default=None, repr=False)
    payer_email: str | None = field(default=None, repr=False)
    # The provider's customer id for this patient when we already have one.
    provider_customer_id: str | None = None
    # Provider-side expiry, honoured only by providers that support it (Mercado Pago);
    # Asaas expires per DAY, so its expiry is enforced by our own job.
    expires_at: datetime | None = None


@dataclass(frozen=True)
class CreatedCharge:
    provider_charge_id: str
    # The Pix "copia e cola". None when the provider answered but the code could not be
    # fetched: `before` timing must treat that as a failure, `after` is best-effort.
    copy_paste: str | None
    expires_at: datetime | None = None
    # The customer id used (new or reused) so the caller can remember it per patient.
    provider_customer_id: str | None = None


@dataclass(frozen=True)
class ChargeState:
    status: PaymentStatus
    paid_amount_cents: int | None = None
    external_reference: str | None = None
    paid_at: datetime | None = None
    expires_at: datetime | None = None
    # TASK-052: the charge's refund entries when the provider lists them - "absent" (not
    # reported), "none" (empty list), "some" (at least one) or "unreadable" (unexpected shape).
    refunds: str = "absent"


@dataclass(frozen=True)
class NormalizedPaymentEvent:
    """A webhook event, provider-neutral. `event_id` is the provider's own id when it has
    one, else a deterministic synthetic one (`<event>:<charge>`), so it is always a valid
    idempotency key. `external_reference` and `event_type` are additive to the spec's
    minimal shape: the reference check and the audit trail need them."""

    provider: str
    event_id: str
    provider_charge_id: str
    status: PaymentStatus
    paid_amount_cents: int | None = None
    paid_at: datetime | None = None
    external_reference: str | None = None
    event_type: str = ""


@runtime_checkable
class PaymentProvider(Protocol):
    """A payment provider wrapper. Implementations are stateless singletons: all per-clinic
    state travels in the `PaymentAccount` argument."""

    name: str

    async def create_pix_charge(
        self, account: PaymentAccount, request: ChargeRequest
    ) -> CreatedCharge: ...

    def verify_webhook(
        self,
        account: PaymentAccount,
        headers: Mapping[str, str],
        raw_body: bytes,
        query: Mapping[str, str] | None = None,
    ) -> bool:
        """Cheap, constant-time authenticity filter. NOT the final authority: before any
        money moves, the event processor asks `get_charge`."""
        ...

    def parse_webhook(
        self, raw_body: bytes, headers: Mapping[str, str] | None = None
    ) -> list[NormalizedPaymentEvent]:
        """Raises ValueError for a body that is not a webhook of this provider."""
        ...

    async def get_charge(self, account: PaymentAccount, provider_charge_id: str) -> ChargeState: ...

    async def cancel_charge(self, account: PaymentAccount, provider_charge_id: str) -> None:
        """Void a charge that was NOT paid. Idempotent: a charge that is already gone is
        success. Never call for a paid charge (`get_charge` first)."""
        ...
