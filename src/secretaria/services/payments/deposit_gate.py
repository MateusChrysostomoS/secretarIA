"""The `before` gate: does THIS booking need a Pix deposit first? (spec sections 5.3 and 7)

`quote_deposit` is the decision the flow makes BEFORE it reserves a slot. It answers one of
three things and never touches the provider:

* `no_deposit`  - the feature is off for this booking (timing `after`, add-on cancelled, plan
                  or service that carries no deposit). The booking commits exactly as it always
                  did: cancelling the add-on must never stop a clinic's agenda.
* `required`    - a deposit is due; carries the amount, the provider and the hold TTL.
* `blocked`     - a deposit IS due but the system cannot ask for it (no patient, no usable
                  credential, provider unknown, entitlement unresolvable). The booking is NOT
                  made: "never confirm without the deposit" is the owner's rule (decision D3).

Those are the first six of the nine silent `None` paths of `maybe_create_deposit` (spec section
7); the last three (customer / charge / no id) and the two extras (no QR, send failed) happen
when the charge is actually created and are decided in `booking_charge.py`.

The function is a pure decision over rows and one entitlement read: no I/O against a PSP, no
message, no state change. It reuses `deposit_lifecycle`'s own price and convenio resolution so
the amount the gate quotes is byte for byte the one `maybe_create_deposit` computes for `after`.
"""

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.clinic_payment_account import PROVIDER_ASAAS
from secretaria.models.pix_deposit import DEPOSIT_TIMING_BEFORE
from secretaria.services.entitlements_client import get_entitlements, is_entitled
from secretaria.services.insurance_catalog import resolve_booking_plan_ids
from secretaria.services.payments import accounts, deposit_lifecycle, registry
from secretaria.services.payments.money import parse_brl_to_cents

logger = get_logger(__name__)

ADDON_KEY = "pix_deposit"

# The hold (and the Pix window) a clinic can configure: the Mercado Pago floor is 30 minutes
# (spec section 12.1), the ceiling keeps a slot from being parked for hours.
HOLD_TTL_MIN_MINUTES = 30
HOLD_TTL_MAX_MINUTES = 120
HOLD_TTL_DEFAULT_MINUTES = 30

# Reasons. `no_deposit` reasons keep the vocabulary of `deposit_lifecycle.DEPOSIT_SKIP_*`.
REASON_TIMING_AFTER = "timing_after"
REASON_DISABLED = "disabled"
REASON_NO_ENTITLEMENT = "no_entitlement"
REASON_INSURANCE_NO_CHARGE = "insurance_no_charge"
REASON_NO_SERVICE = "no_service"
REASON_UNPARSEABLE_PRICE = "unparseable_price"
REASON_ZERO_AMOUNT = "zero_amount"
REASON_REQUIRED = "required"
REASON_ENTITLEMENT_UNRESOLVABLE = "entitlement_unresolvable"
REASON_NO_PATIENT = "no_patient"
REASON_NO_PROVIDER = "no_provider"
REASON_NO_CREDENTIAL = "no_credential"

Outcome = Literal["no_deposit", "required", "blocked"]


def clamp_ttl(minutes: int | None) -> int:
    """The clinic's hold TTL, forced into the supported range (a bad stored value never
    produces a zero-minute or day-long hold)."""
    if not minutes:
        return HOLD_TTL_DEFAULT_MINUTES
    return max(HOLD_TTL_MIN_MINUTES, min(HOLD_TTL_MAX_MINUTES, int(minutes)))


@dataclass(frozen=True)
class DepositQuote:
    outcome: Outcome
    reason: str
    amount_cents: int = 0
    provider: str | None = None
    ttl_minutes: int = HOLD_TTL_DEFAULT_MINUTES
    service_label: str | None = None

    @property
    def required(self) -> bool:
        return self.outcome == "required"

    @property
    def blocked(self) -> bool:
        return self.outcome == "blocked"


def timing_is_before(tenant: Tenant) -> bool:
    return (tenant.deposit_timing or "after") == DEPOSIT_TIMING_BEFORE


async def is_addon_active(tenant_id: UUID, redis=None) -> bool | None:
    """The `pix_deposit` add-on state: True/False, or None when brain-api cannot say (and no
    stale copy exists). Callers treat None as "unknown", never as "off"."""
    summary = await get_entitlements(tenant_id, redis)
    if summary is None:
        return None
    return is_entitled(summary, ADDON_KEY)


async def before_gate_active(tenant: Tenant, redis=None) -> bool:
    """Whether bookings of this clinic must go through the deposit gate at all.

    True when the timing is `before`, the clinic switched deposits on, and the add-on is active
    OR unknown: the LLM tools use this as their fail-closed guard, so an unreadable entitlement
    refuses a direct booking instead of letting one slip past the deposit.
    """
    if not timing_is_before(tenant) or not tenant.pix_deposit_enabled:
        return False
    return await is_addon_active(tenant.id, redis) is not False


async def quote_deposit(
    session: AsyncSession,
    tenant: Tenant,
    *,
    patient_id: UUID | None,
    appointment_type: str | None,
    professional_id: UUID | None,
    insurance: str | None,
    redis=None,
) -> DepositQuote:
    """Decide whether a booking of `appointment_type` needs a deposit before it exists."""
    if not timing_is_before(tenant):
        return DepositQuote("no_deposit", REASON_TIMING_AFTER)
    if not tenant.pix_deposit_enabled:
        return DepositQuote("no_deposit", REASON_DISABLED)

    active = await is_addon_active(tenant.id, redis)
    if active is False:
        return DepositQuote("no_deposit", REASON_NO_ENTITLEMENT)
    if active is None:
        return DepositQuote("blocked", REASON_ENTITLEMENT_UNRESOLVABLE)

    # A stand-in for the appointment that does not exist yet: the lifecycle helpers below read
    # only these four attributes, so the gate and the `after` hook can never disagree.
    plan_id, professional_plan_id = await resolve_booking_plan_ids(
        session, tenant.id, insurance, professional_id
    )
    booking = SimpleNamespace(
        appointment_type=appointment_type,
        professional_id=professional_id,
        insurance_plan_id=plan_id,
        insurance_professional_plan_id=professional_plan_id,
    )
    if not await deposit_lifecycle._insurance_charges_deposit(session, tenant, booking):
        return DepositQuote("no_deposit", REASON_INSURANCE_NO_CHARGE)

    service_name, price_text = await deposit_lifecycle._resolve_service_and_price(
        session, tenant, booking
    )
    price_cents = parse_brl_to_cents(price_text)
    if price_cents is None:
        # "No price, no deposit" is the owner's locked rule (CHECKPOINT_pix_deposit.md).
        return DepositQuote(
            "no_deposit", REASON_UNPARSEABLE_PRICE if service_name else REASON_NO_SERVICE
        )
    amount_cents = round(price_cents * tenant.pix_deposit_percent / 100)
    if amount_cents <= 0:
        return DepositQuote("no_deposit", REASON_ZERO_AMOUNT)

    # From here on a deposit IS due, so anything missing is an operational error, not a choice.
    if patient_id is None:
        return DepositQuote("blocked", REASON_NO_PATIENT)
    provider_name = tenant.payment_provider or PROVIDER_ASAAS
    if registry.find_provider(provider_name) is None:
        return DepositQuote("blocked", REASON_NO_PROVIDER)
    account = await accounts.load_charge_account(
        session, tenant_id=tenant.id, provider=provider_name, timing="before"
    )
    if account is None:
        return DepositQuote("blocked", REASON_NO_CREDENTIAL)

    return DepositQuote(
        "required",
        REASON_REQUIRED,
        amount_cents=amount_cents,
        provider=provider_name,
        ttl_minutes=clamp_ttl(tenant.deposit_hold_ttl_minutes),
        service_label=service_name,
    )
