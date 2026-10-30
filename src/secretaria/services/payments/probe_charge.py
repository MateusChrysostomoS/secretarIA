"""The clinic's "Testar cobrança": a real Pix charge, voided right away (TASK-071, Plan 4, task 4).

It runs through the SAME provider path as a patient's charge (`provider.create_pix_charge`), so
a success is real proof that the clinic's Asaas account can charge - which nothing that merely
reads the account status can prove. Rules:

* one test at a time per clinic: a Redis `SET NX` lock for 60 seconds; a second click (or a second
  manager) gets `busy` and never reaches Asaas;
* the test customer and charge are ALWAYS removed (`finally`), even when the result was a failure
  or the clean-up itself fails half way (`cleaned=False` then tells the manager to remove them in
  the Asaas panel);
* the test cannot pass for a patient's charge: its reference starts with `probe-` (no deposit has
  it, so any Asaas webhook about it is ignored as an unknown charge), the customer carries a name
  that says "pode apagar" and neither e-mail nor phone, so Asaas has nobody to notify;
* the result is written with the same vocabulary as a patient's failure
  (`record_charge_success` / `record_charge_failure`), so the badge and the checklist read it;
* the document the manager types is used in memory only - never stored, never logged, never in a
  returned value (decision D7).
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.clinic_payment_account import PROVIDER_ASAAS
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, payer_document as payer_doc, registry
from secretaria.services.payments.failure_reason import FailureReason, coerce
from secretaria.services.payments.provider import ChargeRequest, ProviderError

logger = get_logger(__name__)

# R$ 5,00: the smallest value Asaas documents for a charge. Revisit with docs/ASAAS_MEDICOES.md.
PROBE_AMOUNT_CENTS = 500
PROBE_LOCK_SECONDS = 60
PROBE_NAME = "Teste Brain - pode apagar"
PROBE_REFERENCE_PREFIX = "probe-"


@dataclass(frozen=True)
class ProbeResult:
    """`ok` - Asaas created the charge AND returned the Pix code. `reason` / `stage` - why not
    (closed vocabulary, never provider text). `cleaned` - the test charge and customer were
    removed. `busy` - another test is running for this clinic; nothing was done."""

    ok: bool
    reason: FailureReason | None = None
    stage: str | None = None
    cleaned: bool = True
    busy: bool = False


def document_digits(text: str | None) -> str | None:
    """The CPF/CNPJ digits of what the manager typed, or None when it is not a valid one."""
    digits = payer_doc.digits_only(text)
    if len(digits) == 11 and payer_doc.is_valid_cpf(digits):
        return digits
    if len(digits) == 14 and payer_doc.is_valid_cnpj(digits):
        return digits
    return None


def _tenant_today(tenant: Tenant | None) -> date:
    try:
        zone = ZoneInfo((tenant.timezone if tenant else None) or "UTC")
    except Exception:  # noqa: BLE001 - a bad stored zone reads as UTC
        zone = ZoneInfo("UTC")
    return datetime.now(zone).date()


async def _cleanup(provider, account, created) -> bool:
    """Remove the test charge, then the test customer. Each is tried even when the other fails;
    the answer is whether both are gone. Only error types are logged."""
    clean = True
    try:
        await provider.cancel_charge(account, created.provider_charge_id)
    except Exception as exc:  # noqa: BLE001 - reported through `cleaned`, never raised
        clean = False
        logger.warning("probe_charge_cancel_failed", error_type=type(exc).__name__)
    customer_id = created.provider_customer_id
    delete_customer = getattr(provider, "delete_customer", None)
    if customer_id and created.customer_created_here and delete_customer is not None:
        try:
            await delete_customer(account, customer_id)
        except Exception as exc:  # noqa: BLE001
            clean = False
            logger.warning("probe_charge_customer_delete_failed", error_type=type(exc).__name__)
    return clean


async def run_probe_charge(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    payer_document: str,
    redis,
    amount_cents: int = PROBE_AMOUNT_CENTS,
) -> ProbeResult:
    """Create a Pix charge on the clinic's own Asaas account and void it. Commits the recorded
    result. `redis` is required for the one-at-a-time lock (callers fail closed without it)."""
    digits = document_digits(payer_document)
    del payer_document
    if digits is None:
        # A typo of the manager says nothing about the account: nothing recorded.
        return ProbeResult(False, FailureReason.customer_document_invalid, "input")

    locked = await redis.set(f"probe_charge:{tenant_id}", "1", ex=PROBE_LOCK_SECONDS, nx=True)
    if not locked:
        return ProbeResult(False, busy=True)

    account = await cfg.get_payment_account(session, tenant_id, PROVIDER_ASAAS)
    provider = registry.find_provider(PROVIDER_ASAAS)
    if account is None or not account.api_key or provider is None:
        return ProbeResult(False, FailureReason.no_account, "config")

    tenant = await session.get(Tenant, tenant_id)
    request = ChargeRequest(
        amount_cents=amount_cents,
        external_reference=f"{PROBE_REFERENCE_PREFIX}{uuid4()}",
        description="Teste Brain - pode apagar",
        due_date=_tenant_today(tenant),
        payer_name=PROBE_NAME,
        payer_document=digits,
    )
    del digits

    created = None
    reason: FailureReason | None = None
    stage: str | None = None
    cleaned = True
    try:
        try:
            created = await provider.create_pix_charge(account, request)
        except ProviderError as exc:
            reason = coerce(exc.reason) or FailureReason.unknown
            stage = exc.stage or "charge"
            logger.warning(
                "probe_charge_failed",
                stage=stage,
                reason=reason.value,
                status_code=exc.status_code,
                code_hint=exc.code_hint,
            )
        except Exception as exc:  # noqa: BLE001 - an unexpected error is a result, not a 500
            reason, stage = FailureReason.unknown, "charge"
            logger.warning("probe_charge_failed", stage=stage, error_type=type(exc).__name__)
        else:
            if not created.copy_paste:
                reason, stage = FailureReason.qr_unavailable, "qr"
                logger.warning("probe_charge_failed", stage=stage, reason=reason.value)
    finally:
        if created is not None:
            cleaned = await _cleanup(provider, account, created)

    if reason is None:
        await accounts.record_charge_success(session, tenant_id=tenant_id, provider=PROVIDER_ASAAS)
    else:
        await accounts.record_charge_failure(
            session, tenant_id=tenant_id, provider=PROVIDER_ASAAS, reason=reason, stage=stage or ""
        )
    await session.commit()
    return ProbeResult(reason is None, reason, stage, cleaned)
