"""Fulfillment: what happens to a charge AFTER it is paid.

`fulfill_paid_charge` is the retryable, idempotent step that runs from the paid state of the
deposit (never from the webhook ledger, so a crash after the commit is re-run by the promotion
sweep).

* `after` timing: the consultation already exists and the patient was told by the post-commit
  notice; fulfillment just records `confirmed`.
* `before` timing (TASK-017): the consultation does NOT exist yet. This is where it is born -
  revalidate the slot, create the Google event (deterministic id) and the appointment, fire the
  post-booking hooks, tell the patient "consulta confirmada" on THEIR channel, free the hold and
  take the conversation out of `AWAITING_DEPOSIT`. Every step is behind its own guard, so a
  retry, a replayed webhook and the sweep can all run it again without duplicating an event, a
  hook or a message.

Nothing here refunds (decision D15). When the money cannot become a consultation - the slot was
taken (a late payment, a doctor's manual event), the snapshot is gone, or the promotion keeps
failing past `GIVE_UP_AFTER` - it opens ONE attention item for the clinic
(`attention.open_attention_item`), flags the deposit `needs_attention` and tells the patient,
honestly and without promising a return, that the CLINIC will get in touch.
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Patient, Tenant
from secretaria.models.payment_attention_item import (
    REASON_LATE_SLOT_TAKEN,
    REASON_ORPHAN_PAYMENT,
    REASON_PROMOTION_FAILED,
)
from secretaria.models.pix_deposit import (
    DEPOSIT_TIMING_BEFORE,
    FULFILLMENT_CONFIRMED,
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_NONE,
    FULFILLMENT_PROMOTING,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.services import booking_promotion, deposit_wait
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.payments import attention, booking_charge, deposit_lifecycle, notify

logger = get_logger(__name__)

# A promotion that still fails this long after the payment stops being retried and becomes an
# attention item (the sweep re-enqueues every few minutes; there is no attempts column).
GIVE_UP_AFTER = timedelta(minutes=15)


class PromotionRetry(Exception):
    """The promotion could not finish yet (Google down, notice undeliverable): raised so the
    arq job fails and is retried, and the sweep re-enqueues it. Carries a symbolic code only."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(f"promotion not finished: {code}")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def _tell_patient(
    session: AsyncSession, tenant: Tenant, deposit: PixDeposit, text: str
) -> bool:
    """Best-effort notice to the patient of `deposit`. True when it was delivered."""
    if deposit.patient_id is None:
        return False
    patient = await session.get(Patient, deposit.patient_id)
    if patient is None or patient.tenant_id != tenant.id:
        return False
    try:
        await notify.deliver(
            session,
            tenant,
            patient,
            [notify.Text(text)],
            conversation_id=deposit.conversation_id,
        )
    except notify.DeliveryError as exc:
        logger.warning("payment_notice_undeliverable", code=exc.code, tenant_id=str(tenant.id))
        return False
    return True


async def _send_once(
    session: AsyncSession, tenant: Tenant, deposit: PixDeposit, text: str
) -> str:
    """Send the patient notice AT MOST ONCE across concurrent runs: `sent`, `already` (another
    run owns it) or `failed` (the claim is released so a retry can send it).

    The claim is an atomic `UPDATE ... WHERE paid_notice_sent_at IS NULL RETURNING`, taken BEFORE
    the send: two workers running the same promotion (a job and the sweep) can never both send.
    A crash between the claim and the send loses the message; a failed send is retried.
    """
    claimed = await session.execute(
        update(PixDeposit)
        .where(
            PixDeposit.id == deposit.id,
            PixDeposit.tenant_id == tenant.id,
            PixDeposit.paid_notice_sent_at.is_(None),
        )
        .values(paid_notice_sent_at=datetime.now(UTC))
        .returning(PixDeposit.id)
        .execution_options(synchronize_session=False)
    )
    won = claimed.scalar_one_or_none() is not None
    await session.commit()
    if not won:
        return "already"
    if await _tell_patient(session, tenant, deposit, text):
        return "sent"
    await session.execute(
        update(PixDeposit)
        .where(PixDeposit.id == deposit.id, PixDeposit.tenant_id == tenant.id)
        .values(paid_notice_sent_at=None)
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    return "failed"


async def _hand_to_the_clinic(
    session: AsyncSession,
    tenant: Tenant,
    deposit: PixDeposit,
    snapshot: booking_promotion.BookingSnapshot | None,
    reason: str,
    notice: str,
) -> None:
    """Open the attention item, flag the deposit and tell the patient the clinic will call."""
    await attention.open_attention_item(
        session,
        tenant_id=deposit.tenant_id,
        reason=reason,
        dedupe_key=attention.attention_dedupe_key(
            deposit.provider, deposit.provider_charge_id or str(deposit.id), reason
        ),
        deposit=deposit,
        paid_amount_cents=deposit.paid_amount_cents,
        slot_start=snapshot.start_at if snapshot else None,
        slot_end=snapshot.end_at if snapshot else None,
        service_label=snapshot.appointment_type if snapshot else None,
    )
    attention.mark_fulfillment(deposit, FULFILLMENT_NEEDS_ATTENTION)
    await session.commit()
    await _send_once(session, tenant, deposit, notice)
    await deposit_wait.leave_wait(session, deposit)
    await session.commit()


async def fulfill_paid_charge(
    session: AsyncSession, *, deposit_id: UUID, redis=None, now: datetime | None = None
) -> str:
    """Run the post-payment step for one deposit. Idempotent. Returns a short outcome:
    missing | not_paid | noop | confirmed_after | promoted | promoted_late | slot_taken |
    orphan_payment | promotion_failed. Commits at its own steps (the caller may commit again).
    Raises `PromotionRetry` when the promotion must be tried again."""
    now = now or datetime.now(UTC)
    deposit = await session.get(PixDeposit, deposit_id)
    if deposit is None:
        return "missing"
    if deposit.status != PixDepositStatus.PAID:
        return "not_paid"
    if deposit.fulfillment_status not in (FULFILLMENT_NONE, FULFILLMENT_PROMOTING):
        return "noop"  # confirmed, needs_attention or resolved: nothing left to do here

    if deposit.deposit_timing != DEPOSIT_TIMING_BEFORE:
        attention.mark_fulfillment(deposit, FULFILLMENT_CONFIRMED)
        return "confirmed_after"

    tenant = await session.get(Tenant, deposit.tenant_id)
    if tenant is None:
        return "missing"
    snapshot = booking_promotion.BookingSnapshot.from_deposit(deposit)
    if snapshot is None:
        await _hand_to_the_clinic(
            session,
            tenant,
            deposit,
            None,
            REASON_ORPHAN_PAYMENT,
            deposit_lifecycle.BEFORE_PROMOTION_STUCK_TEXT,
        )
        return "orphan_payment"

    # It was already expired (and its hold released) when the money arrived: a LATE payment.
    late = deposit.resolved_at is not None

    first_time = False
    if deposit.appointment_id is None:
        created = await _create_the_consultation(session, tenant, deposit, snapshot, now)
        if isinstance(created, str):
            return created  # the promotion will not happen: the clinic was asked instead
        first_time = created.created
    # else: a previous attempt got this far, only the tail (notice, state) is retried.

    return await _finish(
        session, tenant, deposit, snapshot, redis, late=late, first_time=first_time
    )


async def _create_the_consultation(
    session: AsyncSession,
    tenant: Tenant,
    deposit: PixDeposit,
    snapshot: booking_promotion.BookingSnapshot,
    now: datetime,
) -> "booking_promotion.Promotion | str":
    """Revalidate the slot and create the event + appointment. Returns the `Promotion`
    (carry on) or, when the promotion will not happen, the final outcome string. Raises
    `PromotionRetry` while a failure is recent enough to retry."""
    calendar = await booking_promotion.calendar_for_booking(
        session, tenant, snapshot.professional_id
    )
    try:
        if calendar is None:
            raise CalendarUnavailableError("no calendar")
        conflict = await booking_promotion.slot_conflict(
            session, calendar, snapshot, deposit=deposit
        )
        if conflict is not None:
            logger.info(
                "payment_promotion_slot_taken", reason=conflict, tenant_id=str(tenant.id)
            )
            await _hand_to_the_clinic(
                session,
                tenant,
                deposit,
                snapshot,
                REASON_LATE_SLOT_TAKEN,
                deposit_lifecycle.BEFORE_LATE_TAKEN_TEXT,
            )
            return "slot_taken"
        return await booking_promotion.promote(session, tenant, deposit, snapshot, calendar)
    except CalendarUnavailableError as exc:
        await _after_rollback(session, tenant, deposit)
        return await _retry_or_give_up(session, tenant, deposit, snapshot, now, "calendar", exc)
    except Exception as exc:  # noqa: BLE001 - never lose a paid charge to a bug
        await _after_rollback(session, tenant, deposit)
        logger.error(
            "payment_promotion_failed",
            error_type=type(exc).__name__,
            tenant_id=str(tenant.id),
        )
        return await _retry_or_give_up(session, tenant, deposit, snapshot, now, "error", exc)


async def _after_rollback(session: AsyncSession, tenant: Tenant, deposit: PixDeposit) -> None:
    """A rollback expires every loaded row, and touching an expired attribute in async code is a
    `MissingGreenlet`: reload the two rows the rest of the function reads."""
    await session.rollback()
    await session.refresh(tenant)
    await session.refresh(deposit)


def _promotion_started_at(deposit: PixDeposit) -> datetime | None:
    """When the retry window of a paid `before` charge started: the LATER of the provider's
    `paid_at` and OUR stamp of when the event core learned of the payment
    (`last_payment_check_at`, set in `event_processor._on_paid`). Asaas reports a bare date for a
    payment read back by `get_charge` ("Já paguei", reconciliation, expiry), which parses as
    00:00 local: measured alone it would end the window before the first retry."""
    anchors = [_aware(value) for value in (deposit.paid_at, deposit.last_payment_check_at) if value]
    return max(anchors) if anchors else None


async def _retry_or_give_up(
    session: AsyncSession,
    tenant: Tenant,
    deposit: PixDeposit,
    snapshot: booking_promotion.BookingSnapshot | None,
    now: datetime,
    code: str,
    cause: Exception,
) -> str:
    """Raise `PromotionRetry` while the payment is recent; past `GIVE_UP_AFTER` the clinic is
    asked instead and the outcome is `promotion_failed`."""
    started = _promotion_started_at(deposit)
    if started is not None and now - started < GIVE_UP_AFTER:
        raise PromotionRetry(code) from cause
    logger.error("payment_promotion_gave_up", code=code, tenant_id=str(tenant.id))
    await _hand_to_the_clinic(
        session,
        tenant,
        deposit,
        snapshot,
        REASON_PROMOTION_FAILED,
        deposit_lifecycle.BEFORE_PROMOTION_STUCK_TEXT,
    )
    return "promotion_failed"


async def _finish(
    session: AsyncSession,
    tenant: Tenant,
    deposit: PixDeposit,
    snapshot: booking_promotion.BookingSnapshot,
    redis,
    *,
    late: bool,
    first_time: bool,
) -> str:
    """Everything after the appointment exists. Each step is idempotent on its own guard."""
    from secretaria.models import Appointment

    await session.refresh(tenant)  # `promote` may have rolled back a lost race
    appointment = await session.get(Appointment, deposit.appointment_id)
    await booking_charge.release_hold_of_charge(session, deposit)
    await session.commit()
    if first_time and appointment is not None:
        # Fired ONCE, by the call that created the appointment (a retry never re-fires them).
        await _enqueue_hooks(redis, tenant, appointment)

    patient = await session.get(Patient, deposit.patient_id) if deposit.patient_id else None
    text = booking_promotion.confirmation_text(
        tenant, snapshot, patient.name if patient else None, late_reopened=late
    )
    if await _send_once(session, tenant, deposit, text) == "failed":
        started = _promotion_started_at(deposit)
        if started is not None and datetime.now(UTC) - started < GIVE_UP_AFTER:
            raise PromotionRetry("notice_undeliverable")
        # Past the window (e.g. a late payment outside WhatsApp's 24 h): the appointment exists
        # and is in the agenda; the clinic can reach the patient. Not retried again.
        logger.warning("payment_confirmation_notice_given_up", tenant_id=str(tenant.id))
    await session.refresh(deposit)

    await deposit_wait.leave_wait(session, deposit)
    attention.mark_fulfillment(deposit, FULFILLMENT_CONFIRMED)
    await session.commit()
    return "promoted_late" if late else "promoted"


async def _enqueue_hooks(redis, tenant: Tenant, appointment) -> None:
    """The `post_booking` hooks (analytics, EHR, handoff, notifications), off the hot path."""
    if redis is None:
        logger.info("payment_promotion_hooks_skipped_no_pool", tenant_id=str(tenant.id))
        return
    try:
        from secretaria.plugins.post_booking import enqueue_post_booking_hooks

        await enqueue_post_booking_hooks(redis, tenant.id, appointment.id, source="flow")
    except Exception as exc:  # noqa: BLE001 - the appointment exists; hooks are best-effort
        logger.warning("payment_promotion_hooks_failed", error_type=type(exc).__name__)
