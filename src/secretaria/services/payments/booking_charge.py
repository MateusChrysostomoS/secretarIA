"""Starting and abandoning the Pix of a `before` booking (spec sections 5.2, 7 and 9).

`begin_payment` is the one place that turns "the patient confirmed a slot and a deposit is due"
into a live charge. Its contract is the heart of decision D3 ("never confirm without the
deposit, never leave the slot dangling"):

* the slot is held FIRST (payment phase, the clinic's TTL) and the hold points at the charge
  that is about to exist (`hold.charge_id = deposit id`), so the reservation and the money are
  one unit;
* the provider is called with OUR reference (`external_reference = deposit id`), never the
  appointment's (there is none yet);
* whatever fails - the customer, the charge, an answer with no Pix code - the charge is voided
  when it was created and the hold is released BEFORE the error is raised. There is no path out
  of this function that leaves a hold behind without a live, recorded charge;
* nothing is sent to the patient here: the caller delivers the Pix (worker turn, on the turn's
  channel) and, if that fails, calls `abandon_charge`.

`abandon_charge` is the patient-initiated way out ("Trocar horário", "Cancelar reserva", /menu):
it voids an UNPAID charge and frees the slot, and refuses to touch a charge the provider says is
already paid (that money is then settled by the normal paid path, never deleted).
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import delete, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import BookingHold, Patient, PixDeposit, Tenant
from secretaria.models.clinic_payment_account import PROVIDER_ASAAS
from secretaria.models.pix_deposit import DEPOSIT_TIMING_BEFORE, PixDepositStatus
from secretaria.services import booking_hold, tenant_config as cfg
from secretaria.services.booking_hold import HeldSlot, LiveChargeHoldError
from secretaria.services.payments import accounts, registry
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.failure_reason import FailureReason
from secretaria.services.payments.provider import ChargeRequest, PaymentStatus, ProviderError

logger = get_logger(__name__)


class ChargeStartError(Exception):
    """The Pix could not be started. `code` is symbolic (a provider code, `no_copy_paste`,
    `no_account`, `no_provider`, `no_patient`, `charge_pending` or `persist_failed`); by the time
    this is raised the hold is released and no live charge is left behind.

    `reason` (a `FailureReason`), `stage` ("customer" / "charge" / "qr" / "persist" / "config"),
    `status_code` and `code_hint` are the SAFE record of why: no free text, no personal data.
    They stay None for the outcomes that are not a failed charge (`no_patient`,
    `charge_pending`)."""

    def __init__(
        self,
        code: str,
        *,
        reason: FailureReason | None = None,
        stage: str | None = None,
        status_code: int | None = None,
        code_hint: str | None = None,
    ) -> None:
        self.code = code
        self.reason = reason
        self.stage = stage
        self.status_code = status_code
        self.code_hint = code_hint
        super().__init__(f"could not start the deposit: {code}")


@dataclass(frozen=True)
class PaymentStart:
    """What the caller needs to ask the patient to pay."""

    deposit_id: UUID
    amount_cents: int
    copy_paste: str
    expires_at: datetime
    hold_id: UUID
    start_at: datetime
    end_at: datetime
    service: str
    minutes: int


def _tenant_tz(tenant: Tenant) -> ZoneInfo:
    try:
        return ZoneInfo(tenant.timezone or "UTC")
    except Exception:  # noqa: BLE001
        return ZoneInfo("UTC")


async def _create_with_one_retry(provider, account, request):
    """One retry on a retryable provider failure (network, 429, 5xx), none otherwise."""
    try:
        return await provider.create_pix_charge(account, request)
    except ProviderError as exc:
        if not exc.retryable:
            raise
        logger.info("payment_charge_retry", code=exc.code)
        return await provider.create_pix_charge(account, request)


def _known_reason(value: str | None) -> FailureReason:
    """A provider's reason, or `unknown` when it gave none (or one outside the vocabulary)."""
    return FailureReason(value) if value in set(FailureReason) else FailureReason.unknown


async def _release_hold(hold_id: UUID) -> None:
    await booking_hold.release(hold_id)


async def begin_payment(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    patient_id: UUID | None,
    quote: DepositQuote,
    professional_id: UUID | None,
    appointment_type: str,
    insurance: str | None,
    attendee_name: str | None,
    start_at: datetime,
    end_at: datetime,
    existing_hold: HeldSlot | None = None,
    payer_document: str | None = None,
    payer_email: str | None = None,
) -> PaymentStart | None:
    """Hold the slot for the deposit window and create its Pix.

    Returns None when somebody else holds the window or the slot starts within the minute
    (`slot_taken`, nothing was created).
    Raises `ChargeStartError` when the charge could not be started; the hold is gone by then.

    `payer_document` (CPF/CNPJ, digits only) and `payer_email` are the TRANSIENT answers of
    decision D7. They go to the provider (as the customer's `cpfCnpj` / the payer's e-mail) and to
    nobody else: never stored, never logged, and absent from the deposit row and the booking
    snapshot. `ChargeStartError("payer_document_required")` / `("payer_email_required")` mean the
    provider needs one and there was none.
    """
    if patient_id is None:
        raise ChargeStartError("no_patient")
    # The window is the clinic's, but never longer than the time left before the slot starts (a
    # consultation cannot be confirmed in the past); a slot that starts within the minute is not
    # worth a Pix at all, the patient picks another time.
    start_utc = start_at if start_at.tzinfo else start_at.replace(tzinfo=UTC)
    minutes_left = int((start_utc - datetime.now(UTC)).total_seconds() // 60)
    if minutes_left < 1:
        return None
    ttl_minutes = min(quote.ttl_minutes, minutes_left)
    deposit_id = uuid4()

    hold: HeldSlot | None = None
    if existing_hold is not None:
        hold = await booking_hold.extend_to_payment(
            existing_hold.id, ttl_minutes=ttl_minutes, charge_id=deposit_id
        )
    if hold is None:
        try:
            hold = await booking_hold.place_hold(
                tenant_id=tenant_id,
                conversation_id=conversation_id,
                patient_id=patient_id,
                professional_id=professional_id,
                appointment_type=appointment_type,
                insurance=insurance,
                start_at=start_at,
                end_at=end_at,
                ttl_minutes=ttl_minutes,
                attendee_name=attendee_name,
                phase="payment",
                charge_id=deposit_id,
            )
        except LiveChargeHoldError as exc:
            raise ChargeStartError("charge_pending") from exc
    if hold is None:
        return None

    try:
        return await _create_and_record(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            quote=quote,
            ttl_minutes=ttl_minutes,
            deposit_id=deposit_id,
            hold=hold,
            appointment_type=appointment_type,
            insurance=insurance,
            attendee_name=attendee_name,
            professional_id=professional_id,
            payer_document=payer_document,
            payer_email=payer_email,
        )
    except ChargeStartError as exc:
        await _release_hold(hold.id)
        _log_charge_failed(tenant_id, exc)
        raise
    except Exception as exc:  # noqa: BLE001 - whatever it was, the hold must not be left over
        await _release_hold(hold.id)
        logger.error("payment_begin_failed", error_type=type(exc).__name__)
        failure = ChargeStartError(
            "persist_failed", reason=FailureReason.persist_failed, stage="persist"
        )
        _log_charge_failed(tenant_id, failure)
        raise failure from exc


def _log_charge_failed(tenant_id: UUID, exc: ChargeStartError) -> None:
    """The ONE operational record of a failed charge: stage + closed reason + HTTP status +
    the provider's short symbolic code. Nothing else may be added here (no name, document,
    phone, e-mail, key or provider text)."""
    if exc.reason is None:
        return
    logger.warning(
        "payment_charge_failed",
        tenant_id=str(tenant_id),
        stage=exc.stage,
        reason=str(exc.reason),
        status=exc.status_code,
        code_hint=exc.code_hint,
    )


async def _create_and_record(
    *,
    tenant_id: UUID,
    conversation_id: UUID,
    patient_id: UUID,
    quote: DepositQuote,
    ttl_minutes: int,
    deposit_id: UUID,
    hold: HeldSlot,
    appointment_type: str,
    insurance: str | None,
    attendee_name: str | None,
    professional_id: UUID | None,
    payer_document: str | None = None,
    payer_email: str | None = None,
) -> PaymentStart:
    provider_name = quote.provider or PROVIDER_ASAAS
    provider = registry.find_provider(provider_name)
    if provider is None:
        raise ChargeStartError("no_provider", reason=FailureReason.unknown, stage="config")

    async with async_session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        patient = await session.get(Patient, patient_id)
        if tenant is None or patient is None or patient.tenant_id != tenant_id:
            raise ChargeStartError("no_patient")
        account = await accounts.load_charge_account(
            session, tenant_id=tenant_id, provider=provider_name, timing="before"
        )
        if account is None:
            raise ChargeStartError("no_account", reason=FailureReason.no_account, stage="config")
        customer_id = await accounts.get_customer_id(
            session, tenant_id=tenant_id, patient_id=patient_id, provider=provider_name
        )
        description = f"Sinal - {appointment_type} - {tenant.clinic_name}"
        try:
            created = await _create_with_one_retry(
                provider,
                account,
                ChargeRequest(
                    amount_cents=quote.amount_cents,
                    external_reference=str(deposit_id),
                    description=description[:255],
                    due_date=datetime.now(_tenant_tz(tenant)).date(),
                    payer_name=patient.name or "Paciente",
                    payer_phone=patient.wa_id,
                    provider_customer_id=customer_id,
                    payer_document=payer_document,
                    payer_email=payer_email,
                    expires_at=hold.expires_at,
                ),
            )
        except ProviderError as exc:
            raise ChargeStartError(
                exc.code,
                reason=_known_reason(exc.reason),
                stage=exc.stage or "charge",
                status_code=exc.status_code,
                code_hint=exc.code_hint,
            ) from exc

        if not created.provider_charge_id:
            # Path 9 of the nine silent `None`s (spec 7): an answer with no charge id. There is
            # nothing to void and nothing to record: the booking is blocked, never confirmed.
            raise ChargeStartError("no_charge_id", reason=FailureReason.unknown, stage="charge")

        # The charge exists at the provider from here on: every later failure voids it.
        try:
            if not created.copy_paste:
                raise ChargeStartError(
                    "no_copy_paste", reason=FailureReason.qr_unavailable, stage="qr"
                )
            if created.provider_customer_id and created.provider_customer_id != customer_id:
                await accounts.remember_customer_id(
                    session,
                    tenant_id=tenant_id,
                    patient_id=patient_id,
                    provider=provider_name,
                    provider_customer_id=created.provider_customer_id,
                )
            session.add(
                PixDeposit(
                    id=deposit_id,
                    tenant_id=tenant_id,
                    appointment_id=None,
                    patient_id=patient_id,
                    provider=provider_name,
                    provider_charge_id=created.provider_charge_id,
                    asaas_payment_id=(
                        created.provider_charge_id if provider_name == PROVIDER_ASAAS else None
                    ),
                    external_reference=str(deposit_id),
                    amount_cents=quote.amount_cents,
                    percent_applied=tenant.pix_deposit_percent,
                    pix_copy_paste=created.copy_paste,
                    status=PixDepositStatus.AWAITING,
                    deposit_timing=DEPOSIT_TIMING_BEFORE,
                    expires_at=hold.expires_at,
                    hold_id=hold.id,
                    conversation_id=conversation_id,
                    booking_snapshot={
                        "conversation_id": str(conversation_id),
                        "professional_id": str(professional_id) if professional_id else None,
                        "appointment_type": appointment_type,
                        "insurance": insurance,
                        "attendee_name": attendee_name,
                        "start_at": hold.start_at.isoformat(),
                        "end_at": hold.end_at.isoformat(),
                    },
                )
            )
            await session.commit()
        except BaseException:
            await session.rollback()
            try:
                await provider.cancel_charge(account, created.provider_charge_id)
            except Exception as exc:  # noqa: BLE001 - best effort; an unpaid charge is inert
                logger.warning("payment_begin_void_failed", error_type=type(exc).__name__)
            raise

    return PaymentStart(
        deposit_id=deposit_id,
        amount_cents=quote.amount_cents,
        copy_paste=created.copy_paste,
        expires_at=hold.expires_at,
        hold_id=hold.id,
        start_at=hold.start_at,
        end_at=hold.end_at,
        service=appointment_type,
        minutes=ttl_minutes,
    )


async def release_hold_of_charge(session: AsyncSession, deposit: PixDeposit) -> None:
    """Drop the reservation behind `deposit` (by the hold id and by the charge id: either alone
    may already be gone). Does not commit."""
    conditions = [BookingHold.charge_id == deposit.id]
    if deposit.hold_id is not None:
        conditions.append(BookingHold.id == deposit.hold_id)
    await session.execute(
        delete(BookingHold).where(BookingHold.tenant_id == deposit.tenant_id, or_(*conditions))
    )


async def abandon_charge(
    session: AsyncSession, tenant: Tenant, deposit_id: UUID, *, now: datetime | None = None
) -> str:
    """Void an UNPAID `before` charge on the patient's initiative and free the slot.

    Returns `"voided"`, `"paid"` (the provider says it was paid: NOTHING is deleted, the
    normal paid path settles it) or `"gone"` (already resolved by another path). A provider
    outage while voiding still frees the slot and leaves the charge for the expiry job to void
    (it stays `aguardando_sinal` until then); a payment that lands in between follows the late
    payment rules. Commits.
    """
    now = now or datetime.now(UTC)
    deposit = await session.get(PixDeposit, deposit_id)
    if (
        deposit is None
        or deposit.tenant_id != tenant.id
        or deposit.status != PixDepositStatus.AWAITING
    ):
        return "gone"

    provider = registry.find_provider(deposit.provider)
    account = await cfg.get_payment_account(session, tenant.id, deposit.provider)
    charge_id = deposit.provider_charge_id
    voided = False
    if provider is not None and account is not None and charge_id:
        try:
            state = await provider.get_charge(account, charge_id)
        except Exception as exc:  # noqa: BLE001 - cannot tell: free the slot, leave the void
            state = None
            logger.warning("payment_abandon_state_unknown", error_type=type(exc).__name__)
        if state is not None and state.status is PaymentStatus.PAID:
            return "paid"
        if state is not None:
            try:
                await provider.cancel_charge(account, charge_id)
                voided = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("payment_abandon_void_failed", error_type=type(exc).__name__)

    if voided:
        # Marked expired by the patient's own choice: no expiry notice is owed.
        await session.execute(
            update(PixDeposit)
            .where(
                PixDeposit.id == deposit.id,
                PixDeposit.tenant_id == tenant.id,
                PixDeposit.status == PixDepositStatus.AWAITING,
            )
            .values(
                status=PixDepositStatus.EXPIRED, resolved_at=now, expired_notice_sent_at=now
            )
            .execution_options(synchronize_session=False)
        )
    await release_hold_of_charge(session, deposit)
    # The conversation stops waiting for this charge (the caller decides where it goes next).
    deposit.conversation_id = None
    await session.commit()
    return "voided"
