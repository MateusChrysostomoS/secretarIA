"""The payment event core: what one normalised event does to money state.

Input is a durable `payment_webhook_events` row (persisted by the webhook handler, or injected
by the reconciliation job); output is an `EventResult` describing the outcome and what the
CALLER must do AFTER it commits (notify the patient, alert the clinic, hand a paid `before`
charge to fulfillment). This module never sends anything itself and never commits.

Rules it enforces (spec sections 8.2-8.6, 10, 10A):

* the row's `tenant_id` is the only tenant; every lookup filters by it - a charge of another
  clinic is never matched, an `event_id` of another clinic never collides;
* a PAID event is a HINT: `provider.get_charge` decides. Status, amount and reference must all
  check out at the provider before any money state moves (a forged or stale webhook, or a leaked
  token, cannot mark a deposit paid);
* "paid" happens ONCE: `UPDATE ... WHERE status = 'aguardando_sinal' RETURNING id`, so RECEIVED
  and CONFIRMED of the same payment (or two workers) can never both win;
* OVERDUE/DELETED never cancel a consultation that was confirmed (only `SCHEDULED` ones), and
  never touch a paid deposit;
* nothing here refunds. Every "paid but no consultation" situation opens ONE attention item
  (`attention.open_attention_item`) and the clinic decides.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Tenant
from secretaria.models.booking_hold import BookingHold
from secretaria.models.payment_attention_item import (
    REASON_AMOUNT_OVER,
    REASON_AMOUNT_UNDER,
    REASON_ORPHAN_PAYMENT,
    REASON_PAID_THEN_CANCELLED,
    REASON_REFERENCE_MISMATCH,
    REASON_REFUNDED_EXTERNALLY,
)
from secretaria.models.payment_webhook_event import (
    EVENT_FAILED,
    EVENT_IGNORED,
    EVENT_PROCESSED,
    PaymentWebhookEvent,
)
from secretaria.models.pix_deposit import (
    DEPOSIT_TIMING_BEFORE,
    FULFILLMENT_CONFIRMED,
    FULFILLMENT_PROMOTING,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.appointment_status import SOURCE_SYSTEM, log_status_transition
from secretaria.services.payments import alerts, attention, registry
from secretaria.services.payments.provider import (
    NormalizedPaymentEvent,
    PaymentAccount,
    PaymentProvider,
    PaymentStatus,
    ProviderError,
)
from secretaria.services.payments.webhook_payload import event_from_dict

logger = get_logger(__name__)

MAX_EVENT_ATTEMPTS = 5


@dataclass(frozen=True)
class EventResult:
    """The outcome of one event. `outcome` is a short stable string (logged and tested); the
    rest tells the caller what to do AFTER commit."""

    outcome: str
    fulfill_deposit_id: UUID | None = None
    notify_paid_deposit_id: UUID | None = None
    notify_expired_appointment_id: UUID | None = None
    alert_kind: str | None = None


def expected_reference(deposit: PixDeposit) -> str | None:
    """The reference we sent the provider for this charge. Rows that predate the payment core
    sent the APPOINTMENT id as `externalReference` and have no `external_reference` column
    value (or lost the backfill race), so fall back to it."""
    if deposit.external_reference:
        return deposit.external_reference
    return str(deposit.appointment_id) if deposit.appointment_id else None


async def find_deposit(
    session: AsyncSession, *, tenant_id: UUID, provider: str, charge_id: str
) -> PixDeposit | None:
    """The tenant's deposit for a provider charge id. A row written by pre-core code has only
    the legacy `asaas_payment_id`, so an Asaas lookup accepts either column."""
    matches = PixDeposit.provider_charge_id == charge_id
    if provider == "asaas":
        matches = or_(matches, PixDeposit.asaas_payment_id == charge_id)
    return await session.scalar(
        select(PixDeposit).where(
            PixDeposit.tenant_id == tenant_id, PixDeposit.provider == provider, matches
        )
    )


def _slot(appointment: Appointment | None) -> dict:
    if appointment is None:
        return {}
    return {
        "slot_start": appointment.start_at,
        "slot_end": appointment.end_at,
        "service_label": appointment.appointment_type,
    }


async def process_payment_event_row(
    session: AsyncSession, event_row_id: UUID, *, now: datetime | None = None
) -> EventResult:
    """Apply one stored event. Idempotent: a row already processed/ignored is a no-op that
    never calls the provider. Commits (the row status and the mutation ride one transaction);
    an exception rolls the whole thing back for the arq retry."""
    now = now or datetime.now(UTC)
    row = await session.get(PaymentWebhookEvent, event_row_id)
    if row is None:
        return EventResult("missing_event")
    if row.status in (EVENT_PROCESSED, EVENT_IGNORED):
        return EventResult("replay_noop")

    tenant = await session.get(Tenant, row.tenant_id)
    provider = registry.find_provider(row.provider)
    account = await cfg.get_payment_account(session, row.tenant_id, row.provider)
    normalized = (row.payload or {}).get("normalized")
    if tenant is None or provider is None or account is None:
        return await _finish(session, row, EventResult("ignored_no_account"), now)
    if not normalized:
        return await _finish(session, row, EventResult("ignored_no_payload"), now)

    result = await apply_normalized_event(
        session,
        tenant=tenant,
        account=account,
        provider=provider,
        event=event_from_dict(normalized),
        now=now,
    )
    return await _finish(session, row, result, now)


async def _finish(
    session: AsyncSession, row: PaymentWebhookEvent, result: EventResult, now: datetime
) -> EventResult:
    row.status = EVENT_IGNORED if result.outcome.startswith("ignored") else EVENT_PROCESSED
    row.processed_at = now
    row.error_code = None
    await session.commit()
    logger.info(
        "payment_event_processed",
        tenant_id=str(row.tenant_id),
        provider=row.provider,
        event_type=row.event_type,
        outcome=result.outcome,
    )
    return result


async def record_event_failure(
    session: AsyncSession, event_row_id: UUID, *, error_code: str
) -> None:
    """Count a failed attempt (symbolic code only); after `MAX_EVENT_ATTEMPTS` the row is
    `failed` and stops being re-enqueued (the reconciliation job still finds a paid charge)."""
    row = await session.get(PaymentWebhookEvent, event_row_id)
    if row is None:
        return
    row.attempts += 1
    row.error_code = error_code[:64]
    if row.attempts >= MAX_EVENT_ATTEMPTS:
        row.status = EVENT_FAILED


async def apply_normalized_event(
    session: AsyncSession,
    *,
    tenant: Tenant,
    account: PaymentAccount | None,
    provider: PaymentProvider,
    event: NormalizedPaymentEvent,
    now: datetime | None = None,
) -> EventResult:
    """The decision core. Mutates through `session` but never commits. `account` may be None
    only on the legacy route (a tenant with a webhook token but no API key): such a PAID event
    cannot be confirmed with the provider and is therefore ignored, never trusted."""
    now = now or datetime.now(UTC)
    deposit = await find_deposit(
        session, tenant_id=tenant.id, provider=event.provider, charge_id=event.provider_charge_id
    )
    if deposit is None:
        return await _on_unknown_charge(session, tenant, event)
    if event.status is PaymentStatus.PAID:
        return await _on_paid(session, tenant, account, provider, deposit, event, now)
    if event.status is PaymentStatus.EXPIRED:
        if deposit.status == PixDepositStatus.AWAITING:
            if account is None:
                return EventResult("ignored_no_account")
            state = await provider.get_charge(account, event.provider_charge_id)
            if state.status is PaymentStatus.PAID:
                paid_event = replace(
                    event, status=PaymentStatus.PAID, paid_at=state.paid_at,
                    paid_amount_cents=state.paid_amount_cents,
                )
                return await _on_paid(session, tenant, account, provider, deposit, paid_event, now)
        return await _on_expired(session, tenant, deposit, event)
    if event.status is PaymentStatus.REFUNDED:
        return await _on_refunded(session, tenant, deposit, event, now)
    return EventResult("ignored_other")


async def _on_unknown_charge(
    session: AsyncSession, tenant: Tenant, event: NormalizedPaymentEvent
) -> EventResult:
    """A charge we have no deposit for. Almost always another product's charge on a shared
    account; but a PAID one carrying OUR reference means we created it and lost the row
    (a crash between the provider call and the commit): never matched automatically, always
    surfaced to the clinic."""
    if event.status is PaymentStatus.PAID and event.external_reference:
        ours = await session.scalar(
            select(PixDeposit.id).where(
                PixDeposit.tenant_id == tenant.id,
                PixDeposit.external_reference == event.external_reference,
            )
        )
        if ours is not None:
            logger.warning(
                "payment_adoption_needed", tenant_id=str(tenant.id), charge=event.provider_charge_id
            )
            await attention.open_attention_item(
                session,
                tenant_id=tenant.id,
                reason=REASON_ORPHAN_PAYMENT,
                dedupe_key=attention.attention_dedupe_key(
                    event.provider, event.provider_charge_id, REASON_ORPHAN_PAYMENT
                ),
                provider=event.provider,
                provider_charge_id=event.provider_charge_id,
                paid_amount_cents=event.paid_amount_cents,
                paid_at=event.paid_at,
            )
            return EventResult("orphan_payment")
    return EventResult("ignored_unknown_charge")


async def _claim_paid(
    session: AsyncSession, deposit: PixDeposit, event: NormalizedPaymentEvent,
    paid_cents: int, now: datetime,
) -> tuple[bool, bool]:
    """Atomically move the deposit to PAID. Returns (won, was_late). `was_late` means it had
    already been marked expired (paid after the due date). Exactly one caller can win."""
    for from_status, late in ((PixDepositStatus.AWAITING, False), (PixDepositStatus.EXPIRED, True)):
        claimed = await session.execute(
            update(PixDeposit)
            .where(
                PixDeposit.id == deposit.id,
                PixDeposit.tenant_id == deposit.tenant_id,
                PixDeposit.status == from_status,
            )
            .values(
                status=PixDepositStatus.PAID,
                paid_at=event.paid_at or now,
                confirming_event_id=event.event_id,
                paid_amount_cents=paid_cents,
            )
            .returning(PixDeposit.id)
            .execution_options(synchronize_session=False)
        )
        if claimed.scalar_one_or_none() is not None:
            await session.refresh(deposit)
            return True, late
    await session.refresh(deposit)
    return False, False


# A paid reservation keeps its slot held until the promotion retries have run their course
# (`fulfillment.GIVE_UP_AFTER` is 15 minutes; 5 more of margin): otherwise the Pix window ending
# a moment before the promotion finishes lets a patient who has NOT paid take the slot of one who
# has. The hold is released by the promotion itself (`release_hold_of_charge`).
PROMOTION_HOLD_MINUTES = 20


async def _pin_hold_while_promoting(
    session: AsyncSession, deposit: PixDeposit, now: datetime
) -> None:
    if deposit.hold_id is None:
        return
    hold = await session.get(BookingHold, deposit.hold_id)
    if hold is None or hold.tenant_id != deposit.tenant_id:
        return
    until = now + timedelta(minutes=PROMOTION_HOLD_MINUTES)
    current = hold.expires_at if hold.expires_at.tzinfo else hold.expires_at.replace(tzinfo=UTC)
    if current < until:
        hold.expires_at = until


async def _on_paid(
    session: AsyncSession,
    tenant: Tenant,
    account: PaymentAccount | None,
    provider: PaymentProvider,
    deposit: PixDeposit,
    event: NormalizedPaymentEvent,
    now: datetime,
) -> EventResult:
    charge_id = event.provider_charge_id
    if account is None:
        logger.warning("payment_event_unverifiable_no_account", tenant_id=str(tenant.id))
        return EventResult("ignored_no_account")

    # The provider is the authority (spec 8.4). A retryable failure propagates so arq retries
    # and NO state has moved; a charge the provider does not know is a forged/stale webhook.
    try:
        state = await provider.get_charge(account, charge_id)
    except ProviderError as exc:
        if exc.status_code == 404:
            logger.warning("payment_event_charge_unknown_at_provider", tenant_id=str(tenant.id))
            return EventResult("ignored_not_paid_at_provider")
        raise
    if state.status is not PaymentStatus.PAID:
        logger.warning(
            "payment_event_not_confirmed_by_provider",
            tenant_id=str(tenant.id),
            provider=event.provider,
            provider_status=state.status.value,
        )
        return EventResult("ignored_not_paid_at_provider")

    appointment = (
        await session.get(Appointment, deposit.appointment_id) if deposit.appointment_id else None
    )
    paid = state.paid_amount_cents or 0

    if state.external_reference != expected_reference(deposit):
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_REFERENCE_MISMATCH,
            dedupe_key=attention.attention_dedupe_key(
                event.provider, charge_id, REASON_REFERENCE_MISMATCH
            ),
            deposit=deposit,
            paid_amount_cents=paid,
            **_slot(appointment),
        )
        return EventResult("ignored_mismatch")

    if paid < deposit.amount_cents:
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_AMOUNT_UNDER,
            dedupe_key=attention.attention_dedupe_key(
                event.provider, charge_id, REASON_AMOUNT_UNDER
            ),
            deposit=deposit,
            paid_amount_cents=paid,
            **_slot(appointment),
        )
        return EventResult("amount_under")

    won, _late = await _claim_paid(session, deposit, event, paid, now)
    if not won:
        return EventResult("already_paid")

    if paid > deposit.amount_cents:
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_AMOUNT_OVER,
            dedupe_key=attention.attention_dedupe_key(
                event.provider, charge_id, REASON_AMOUNT_OVER
            ),
            deposit=deposit,
            paid_amount_cents=paid,
            **_slot(appointment),
        )

    if deposit.deposit_timing == DEPOSIT_TIMING_BEFORE:
        # The consultation does not exist yet: the promoter (F2) creates it, retryably,
        # from the deposit's own state. Nothing else happens here.
        attention.mark_fulfillment(deposit, FULFILLMENT_PROMOTING)
        # Our own clock, not the provider's date: the promotion's retry window starts here
        # (`fulfillment._promotion_started_at`), and the slot stays held for it (below).
        deposit.last_payment_check_at = now
        await _pin_hold_while_promoting(session, deposit, now)
        return EventResult("paid_before", fulfill_deposit_id=deposit.id)

    if appointment is None:
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_ORPHAN_PAYMENT,
            dedupe_key=attention.attention_dedupe_key(
                event.provider, charge_id, REASON_ORPHAN_PAYMENT
            ),
            deposit=deposit,
            paid_amount_cents=paid,
        )
        return EventResult("paid_appointment_missing")
    if appointment.status == AppointmentStatus.CANCELLED:
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_PAID_THEN_CANCELLED,
            dedupe_key=attention.attention_dedupe_key(
                event.provider, charge_id, REASON_PAID_THEN_CANCELLED
            ),
            deposit=deposit,
            paid_amount_cents=paid,
            suggested_refund_cents=deposit.amount_cents,
            **_slot(appointment),
        )
        return EventResult("paid_appointment_cancelled")

    attention.mark_fulfillment(deposit, FULFILLMENT_CONFIRMED)
    return EventResult("paid", notify_paid_deposit_id=deposit.id)


async def _on_expired(
    session: AsyncSession, tenant: Tenant, deposit: PixDeposit, event: NormalizedPaymentEvent
) -> EventResult:
    if deposit.status == PixDepositStatus.EXPIRED:
        return EventResult("expired_replay")
    if deposit.status == PixDepositStatus.PAID:
        # A charge that was paid is never "expired": nothing is cancelled. Only if the
        # consultation was cancelled by another path and nobody has been told yet.
        appointment = (
            await session.get(Appointment, deposit.appointment_id)
            if deposit.appointment_id
            else None
        )
        if (
            appointment is not None
            and appointment.status == AppointmentStatus.CANCELLED
            and not await attention.has_open_item(
                session, tenant_id=tenant.id, deposit_id=deposit.id
            )
        ):
            await attention.open_attention_item(
                session,
                tenant_id=tenant.id,
                reason=REASON_PAID_THEN_CANCELLED,
                dedupe_key=attention.attention_dedupe_key(
                    event.provider, event.provider_charge_id, REASON_PAID_THEN_CANCELLED
                ),
                deposit=deposit,
                suggested_refund_cents=deposit.amount_cents,
                **_slot(appointment),
            )
        return EventResult("ignored_expired_on_paid")
    if deposit.status != PixDepositStatus.AWAITING:
        return EventResult("ignored_expired_on_resolved")

    claimed = await session.execute(
        update(PixDeposit)
        .where(
            PixDeposit.id == deposit.id,
            PixDeposit.tenant_id == tenant.id,
            PixDeposit.status == PixDepositStatus.AWAITING,
        )
        .values(status=PixDepositStatus.EXPIRED, resolved_at=datetime.now(UTC))
        .returning(PixDeposit.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        await session.refresh(deposit)
        return EventResult("expired_replay")
    await session.refresh(deposit)

    appointment = (
        await session.get(Appointment, deposit.appointment_id) if deposit.appointment_id else None
    )
    if appointment is None or appointment.tenant_id != tenant.id:
        return EventResult("expired")  # `before`: no consultation exists; F2 releases the hold
    if appointment.status != AppointmentStatus.SCHEDULED:
        # Confirmed / rescheduled / attended / no-show: the money is late, the consultation
        # is not ours to cancel. Tell the clinic instead of deciding for them.
        logger.warning(
            "payment_overdue_appointment_kept",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
            appointment_status=str(appointment.status.value),
        )
        return EventResult("expired_appointment_kept", alert_kind="overdue_kept")

    previous = appointment.status
    appointment.status = AppointmentStatus.CANCELLED
    log_status_transition(
        appointment_id=appointment.id,
        tenant_id=tenant.id,
        old_status=previous,
        new_status=AppointmentStatus.CANCELLED,
        source=SOURCE_SYSTEM,
        idempotency_key=f"{event.provider}:{event.event_id}",
    )
    return EventResult("expired", notify_expired_appointment_id=appointment.id)


async def _on_refunded(
    session: AsyncSession,
    tenant: Tenant,
    deposit: PixDeposit,
    event: NormalizedPaymentEvent,
    now: datetime,
) -> EventResult:
    """The CLINIC refunded in the provider's panel (we never do). Stamp the open item, or open
    the informative one when the charge had been paid and nobody was asked yet."""
    stamped = await attention.mark_refund_seen(
        session, tenant_id=tenant.id, deposit_id=deposit.id, now=now
    )
    if not stamped and deposit.paid_at is not None:
        await attention.open_attention_item(
            session,
            tenant_id=tenant.id,
            reason=REASON_REFUNDED_EXTERNALLY,
            dedupe_key=attention.attention_dedupe_key(
                event.provider, event.provider_charge_id, REASON_REFUNDED_EXTERNALLY
            ),
            deposit=deposit,
        )
    return EventResult("refund_observed")


async def run_post_commit(
    session: AsyncSession,
    tenant: Tenant,
    result: EventResult,
    *,
    redis=None,
    enqueue: Callable[[str, str], Awaitable[None]] | None = None,
) -> None:
    """Everything an event asks for AFTER its transaction committed: patient notices, the
    clinic alert, the hand-off to fulfillment. Each step is best-effort and isolated - a
    failure here never undoes the money state and never blocks a sibling step."""
    from secretaria.services.payments import deposit_lifecycle

    if result.notify_paid_deposit_id is not None:
        try:
            deposit = await session.get(PixDeposit, result.notify_paid_deposit_id)
            if deposit is not None and deposit.tenant_id == tenant.id:
                await deposit_lifecycle._notify_paid(session, tenant, deposit)
                deposit.paid_notice_sent_at = datetime.now(UTC)
                await session.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning("payment_paid_notice_failed", error_type=type(exc).__name__)
    if result.notify_expired_appointment_id is not None:
        try:
            appointment = await session.get(Appointment, result.notify_expired_appointment_id)
            if appointment is not None and appointment.tenant_id == tenant.id:
                await deposit_lifecycle._notify_expired(session, tenant, appointment)
        except Exception as exc:  # noqa: BLE001
            logger.warning("payment_expired_notice_failed", error_type=type(exc).__name__)
    if result.alert_kind is not None:
        try:
            await alerts.send_payment_alert(redis, tenant, result.alert_kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("payment_alert_failed", error_type=type(exc).__name__)
    if result.fulfill_deposit_id is not None and enqueue is not None:
        try:
            await enqueue("fulfill_paid_charge_job", str(result.fulfill_deposit_id))
        except Exception as exc:  # noqa: BLE001 - the promotion sweep re-enqueues
            logger.warning("payment_fulfill_enqueue_failed", error_type=type(exc).__name__)
