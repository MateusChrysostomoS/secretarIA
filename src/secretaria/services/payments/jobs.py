"""The recurring payment jobs, as plain functions over a session (the cron wrappers live in
`workers/payments_cron.py`). Spec section 9.

* `expire_due_charges`     - `before` charges whose Pix window ended: ASK the provider first,
                             void, then mark expired. Never expires blind.
* `expire_due_adjustments` - the service-change differences (unpaid Pix of a dearer service,
                             unanswered "quer a diferença de volta?"), same rules.
* `reconcile_open_charges` - the safety net for a webhook that never arrived: polls the provider
                             for unpaid charges and injects a synthetic PAID event.
* `requeue_stuck_events` / `requeue_stuck_fulfillments` - the two sweeps that make "durable
                             but never processed" impossible.
* `daily_maintenance`      - prune webhook payloads (30 days) and run the registered daily steps
                             (the Asaas key recheck; F3 adds the Mercado Pago token renewal).

Every job is idempotent (atomic transitions and `insert_ignore`), bounded per tick (the Asaas
quota is 25,000 requests / 12 h per account and 50 concurrent GETs), and isolates each charge:
one failure is logged and counted, never allowed to stop the batch. Every lookup is scoped by the
deposit's own `tenant_id`.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, null, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, PaymentWebhookEvent, PixDeposit, Tenant
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.clinic_payment_account import PAYMENT_PROVIDERS
from secretaria.models.payment_webhook_event import EVENT_RECEIVED
from secretaria.models.pix_deposit import (
    DEPOSIT_TIMING_AFTER,
    DEPOSIT_TIMING_BEFORE,
    FULFILLMENT_NONE,
    FULFILLMENT_PROMOTING,
    PixDepositStatus,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import alerts, registry
from secretaria.services.payments.db_utils import insert_ignore
from secretaria.services.payments.provider import (
    ChargeState,
    NormalizedPaymentEvent,
    PaymentAccount,
    PaymentProvider,
    PaymentStatus,
)
from secretaria.services.payments.webhook_payload import event_to_dict

logger = get_logger(__name__)

EnqueueFn = Callable[[str, str], Awaitable[None]]
ExpiryHook = Callable[[AsyncSession, PixDeposit], Awaitable[None]]
DailyStep = Callable[[AsyncSession, datetime, Any], Awaitable[None]]

# Called AFTER a `before` charge was voided and marked expired (F2 registers the one that frees
# the hold, tells the patient and leaves AWAITING_DEPOSIT). Best-effort: a failing hook is logged.
EXPIRY_HOOKS: list[ExpiryHook] = []
# Run by `daily_maintenance`, in order, each isolated (F1: the Asaas recheck; F3: MP refresh).
DAILY_STEPS: list[DailyStep] = []

PAYLOAD_RETENTION = timedelta(days=30)


def register_expiry_hook(hook: ExpiryHook) -> None:
    EXPIRY_HOOKS.append(hook)


def register_daily_step(step: DailyStep) -> None:
    DAILY_STEPS.append(step)


@dataclass
class ExpiryReport:
    expired: int = 0
    paid_found: int = 0
    skipped_provider_down: int = 0
    failed_cancel: int = 0


@dataclass
class ReconcileReport:
    checked: int = 0
    paid_found: int = 0
    webhooks_missing: int = 0


@dataclass
class DailyReport:
    payloads_pruned: int = 0


async def _provider_and_account(
    session: AsyncSession, deposit: PixDeposit
) -> tuple[PaymentProvider, PaymentAccount] | None:
    provider = registry.find_provider(deposit.provider)
    account = await cfg.get_payment_account(session, deposit.tenant_id, deposit.provider)
    if provider is None or account is None:
        return None
    return provider, account


async def inject_paid_event(
    session: AsyncSession, deposit: PixDeposit, state: ChargeState, enqueue: EnqueueFn
) -> bool:
    """Record a synthetic PAID event for a charge the provider reports paid, and enqueue it
    through the SAME path a webhook takes. Idempotent by `reconcile:<charge>:PAID`. Returns
    whether a new event was written (False = it already existed and is on its way)."""
    return await inject_paid_event_for_charge(
        session,
        tenant_id=deposit.tenant_id,
        provider=deposit.provider,
        charge_id=deposit.provider_charge_id or deposit.asaas_payment_id or "",
        state=state,
        enqueue=enqueue,
    )


async def inject_paid_event_for_charge(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    provider: str,
    charge_id: str,
    state: ChargeState,
    enqueue: EnqueueFn,
) -> bool:
    """`inject_paid_event` for any charge of ours (a sinal or the difference of a service
    change, `service_change.expire_adjustments`): same event id, same idempotency."""
    event = NormalizedPaymentEvent(
        provider=provider,
        event_id=f"reconcile:{charge_id}:PAID",
        provider_charge_id=charge_id,
        status=PaymentStatus.PAID,
        paid_amount_cents=state.paid_amount_cents,
        paid_at=state.paid_at,
        external_reference=state.external_reference,
        event_type="reconcile",
    )
    row_id = await insert_ignore(
        session,
        PaymentWebhookEvent,
        {
            "tenant_id": tenant_id,
            "provider": provider,
            "event_id": event.event_id,
            "provider_charge_id": charge_id,
            "event_type": "reconcile",
            "status": EVENT_RECEIVED,
            "attempts": 0,
            "payload": {"normalized": event_to_dict(event), "synthetic": True},
        },
        index_elements=["tenant_id", "provider", "event_id"],
    )
    await session.commit()
    if row_id is None:
        return False
    await enqueue("process_payment_event", str(row_id))
    return True


async def expire_due_charges(
    session: AsyncSession, *, now: datetime, enqueue: EnqueueFn, limit: int = 50
) -> ExpiryReport:
    """Expire `before` charges whose window ended. For each: `get_charge` FIRST (a paid charge
    whose webhook is late must never be expired - it is reconciled instead), then
    `cancel_charge`, then the atomic AWAITING -> EXPIRED. A provider outage or a failed void
    changes nothing and the next tick retries."""
    report = ExpiryReport()
    due = (
        await session.scalars(
            select(PixDeposit.id)
            .where(
                PixDeposit.deposit_timing == DEPOSIT_TIMING_BEFORE,
                PixDeposit.status == PixDepositStatus.AWAITING,
                PixDeposit.expires_at.is_not(None),
                PixDeposit.expires_at <= now,
            )
            .order_by(PixDeposit.last_payment_check_at.asc().nullsfirst(), PixDeposit.expires_at)
            .limit(limit)
        )
    ).all()
    for deposit_id in due:
        try:
            await _expire_one(session, deposit_id, now, enqueue, report)
        except Exception as exc:  # noqa: BLE001 - one charge never stops the batch
            await session.rollback()
            logger.warning("payment_expiry_failed", error_type=type(exc).__name__)
        finally:
            await session.execute(
                update(PixDeposit).where(PixDeposit.id == deposit_id).values(
                    last_payment_check_at=now
                )
            )
            await session.commit()
    return report


async def _expire_one(
    session: AsyncSession,
    deposit_id: UUID,
    now: datetime,
    enqueue: EnqueueFn,
    report: ExpiryReport,
) -> None:
    deposit = await session.get(PixDeposit, deposit_id)
    if deposit is None or deposit.status != PixDepositStatus.AWAITING:
        return
    pair = await _provider_and_account(session, deposit)
    charge_id = deposit.provider_charge_id
    if pair is None or not charge_id:
        return
    provider, account = pair

    try:
        state = await provider.get_charge(account, charge_id)
    except Exception as exc:  # noqa: BLE001 - cannot tell: never expire blind
        report.skipped_provider_down += 1
        logger.warning(
            "payment_expiry_provider_unavailable",
            tenant_id=str(deposit.tenant_id),
            error_type=type(exc).__name__,
        )
        return
    if state.status is PaymentStatus.PAID:
        report.paid_found += 1
        await inject_paid_event(session, deposit, state, enqueue)
        return

    try:
        await provider.cancel_charge(account, charge_id)
    except Exception as exc:  # noqa: BLE001 - not voided: leave it AWAITING, retry next tick
        report.failed_cancel += 1
        logger.warning(
            "payment_expiry_void_failed",
            tenant_id=str(deposit.tenant_id),
            error_type=type(exc).__name__,
        )
        return

    claimed = await session.execute(
        update(PixDeposit)
        .where(
            PixDeposit.id == deposit.id,
            PixDeposit.tenant_id == deposit.tenant_id,
            PixDeposit.status == PixDepositStatus.AWAITING,
        )
        .values(status=PixDepositStatus.EXPIRED, resolved_at=now)
        .returning(PixDeposit.id)
        .execution_options(synchronize_session=False)
    )
    won = claimed.scalar_one_or_none() is not None
    await session.commit()
    if not won:
        return
    report.expired += 1
    await session.refresh(deposit)
    for hook in list(EXPIRY_HOOKS):
        try:
            await hook(session, deposit)
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.warning("payment_expiry_hook_failed", error_type=type(exc).__name__)
    await session.commit()


async def expire_due_adjustments(
    session: AsyncSession, *, now: datetime, enqueue: EnqueueFn, limit: int = 50
) -> int:
    """The service-change sweep (`service_change.expire_adjustments`): a refund question
    unanswered for 24 h becomes credit; an unpaid difference Pix past its window is checked at
    the provider, voided and expired (the old service stays); a difference cancelled while the
    provider was down has its void retried until it succeeds. Run on every expiry tick."""
    from secretaria.services.payments import service_change

    return await service_change.expire_adjustments(
        session, now=now, enqueue=enqueue, limit=limit
    )


async def reconcile_open_charges(
    session: AsyncSession,
    *,
    now: datetime,
    enqueue: EnqueueFn,
    redis=None,
    older_than: timedelta = timedelta(minutes=10),
    limit_per_provider: int = 50,
) -> ReconcileReport:
    """Poll the provider for AWAITING charges older than `older_than` (both timings) and
    inject a synthetic PAID event for any that were paid. Sequential and bounded per provider
    (rate limits). A paid charge for which the provider never delivered a webhook alerts the
    clinic that its webhooks are not arriving (debounced)."""
    report = ReconcileReport()
    for provider_name in PAYMENT_PROVIDERS:
        ids = (
            await session.scalars(
                select(PixDeposit.id)
                .where(
                    PixDeposit.provider == provider_name,
                    PixDeposit.status == PixDepositStatus.AWAITING,
                    PixDeposit.provider_charge_id.is_not(None),
                    PixDeposit.created_at <= now - older_than,
                )
                .order_by(
                    PixDeposit.last_payment_check_at.asc().nullsfirst(), PixDeposit.created_at
                )
                .limit(limit_per_provider)
            )
        ).all()
        for deposit_id in ids:
            try:
                await _reconcile_one(session, deposit_id, enqueue, redis, report)
            except Exception as exc:  # noqa: BLE001
                await session.rollback()
                logger.warning("payment_reconcile_failed", error_type=type(exc).__name__)
            finally:
                await session.execute(
                    update(PixDeposit).where(PixDeposit.id == deposit_id).values(
                        last_payment_check_at=now
                    )
                )
                await session.commit()
    return report


async def _reconcile_one(
    session: AsyncSession,
    deposit_id: UUID,
    enqueue: EnqueueFn,
    redis,
    report: ReconcileReport,
) -> None:
    deposit = await session.get(PixDeposit, deposit_id)
    if deposit is None:
        return
    pair = await _provider_and_account(session, deposit)
    if pair is None:
        return
    provider, account = pair
    report.checked += 1
    state = await provider.get_charge(account, deposit.provider_charge_id)
    if state.status is not PaymentStatus.PAID:
        return
    report.paid_found += 1
    await inject_paid_event(session, deposit, state, enqueue)

    delivered = await session.scalar(
        select(func.count())
        .select_from(PaymentWebhookEvent)
        .where(
            PaymentWebhookEvent.tenant_id == deposit.tenant_id,
            PaymentWebhookEvent.provider == deposit.provider,
            PaymentWebhookEvent.provider_charge_id == deposit.provider_charge_id,
            ~PaymentWebhookEvent.event_id.startswith("reconcile:"),
        )
    )
    if not delivered:
        report.webhooks_missing += 1
        tenant = await session.get(Tenant, deposit.tenant_id)
        if tenant is not None:
            await alerts.send_payment_alert(redis, tenant, "webhooks_not_arriving")


async def requeue_stuck_events(
    session: AsyncSession,
    *,
    now: datetime,
    enqueue: EnqueueFn,
    older_than: timedelta = timedelta(minutes=1),
    limit: int = 200,
) -> int:
    """Re-enqueue durable events still `received` after `older_than` (the enqueue after the
    handler's INSERT failed, or the worker was down). Returns how many were re-enqueued."""
    ids = (
        await session.scalars(
            select(PaymentWebhookEvent.id)
            .where(
                PaymentWebhookEvent.status == EVENT_RECEIVED,
                PaymentWebhookEvent.received_at <= now - older_than,
            )
            .order_by(PaymentWebhookEvent.received_at)
            .limit(limit)
        )
    ).all()
    count = 0
    for event_id in ids:
        try:
            await enqueue("process_payment_event", str(event_id))
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("payment_sweep_enqueue_failed", error_type=type(exc).__name__)
    return count


async def requeue_stuck_fulfillments(
    session: AsyncSession,
    *,
    now: datetime,
    enqueue: EnqueueFn,
    older_than: timedelta = timedelta(minutes=1),
    limit: int = 200,
) -> int:
    """Re-run the post-payment step for PAID `before` charges that never reached
    `confirmed` (a crash after the payment commit). Retried from the deposit's own state."""
    ids = (
        await session.scalars(
            select(PixDeposit.id)
            .where(
                PixDeposit.status == PixDepositStatus.PAID,
                PixDeposit.deposit_timing == DEPOSIT_TIMING_BEFORE,
                PixDeposit.fulfillment_status.in_((FULFILLMENT_NONE, FULFILLMENT_PROMOTING)),
                PixDeposit.paid_at.is_not(None),
                PixDeposit.paid_at <= now - older_than,
            )
            .order_by(PixDeposit.paid_at)
            .limit(limit)
        )
    ).all()
    count = 0
    for deposit_id in ids:
        try:
            await enqueue("fulfill_paid_charge_job", str(deposit_id))
            count += 1
        except Exception as exc:  # noqa: BLE001
            logger.warning("payment_sweep_enqueue_failed", error_type=type(exc).__name__)
    return count


async def recover_paid_notices(
    session: AsyncSession, *, now: datetime, limit: int = 200
) -> int:
    """Retry patient notices after a crash between the paid commit and the external send.

    The marker is written only after a successful send. A crash after send but before marker
    may duplicate the notice; losing it permanently would be worse.

    Only a consultation that is still going to happen is told "sua consulta está garantida": the
    processor sends NO notice when the payment found it cancelled/missing (it opens a clinic
    item instead), so the NULL marker there is not a lost notice (session 07, TASK-067).
    """
    from secretaria.services.payments import deposit_lifecycle

    ids = (
        await session.scalars(
            select(PixDeposit.id)
            .join(Appointment, Appointment.id == PixDeposit.appointment_id)
            .where(
                PixDeposit.status == PixDepositStatus.PAID,
                PixDeposit.deposit_timing == DEPOSIT_TIMING_AFTER,
                PixDeposit.paid_notice_sent_at.is_(None),
                Appointment.tenant_id == PixDeposit.tenant_id,
                Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
                PixDeposit.paid_at.is_not(None),
                PixDeposit.paid_at <= now - timedelta(minutes=1),
            )
            .order_by(PixDeposit.last_payment_check_at.asc().nullsfirst(), PixDeposit.paid_at)
            .limit(limit)
        )
    ).all()
    sent = 0
    for deposit_id in ids:
        try:
            deposit = await session.get(PixDeposit, deposit_id)
            tenant = await session.get(Tenant, deposit.tenant_id) if deposit is not None else None
            appointment = (
                await session.get(Appointment, deposit.appointment_id)
                if deposit is not None and deposit.appointment_id is not None
                else None
            )
            if (
                deposit is not None
                and tenant is not None
                and deposit.paid_notice_sent_at is None
                # re-read: the consultation may have been cancelled since the batch query
                and appointment is not None
                and appointment.status in LIVE_APPOINTMENT_STATUSES
            ):
                await deposit_lifecycle._notify_paid(session, tenant, deposit)
                deposit.paid_notice_sent_at = now
                sent += 1
            await session.commit()
        except Exception as exc:  # noqa: BLE001
            await session.rollback()
            logger.warning("payment_paid_notice_retry_failed", error_type=type(exc).__name__)
        finally:
            await session.execute(
                update(PixDeposit).where(PixDeposit.id == deposit_id).values(
                    last_payment_check_at=now
                )
            )
            await session.commit()
    return sent


async def daily_maintenance(session: AsyncSession, *, now: datetime, redis=None) -> DailyReport:
    """Prune webhook payloads older than 30 days (the row and its ids stay; only the minimised
    body goes, decision D11) and run every registered daily step, each isolated."""
    pruned = await session.execute(
        update(PaymentWebhookEvent)
        .where(
            PaymentWebhookEvent.received_at < now - PAYLOAD_RETENTION,
            PaymentWebhookEvent.payload.is_not(None),
        )
        .values(payload=null())
        .execution_options(synchronize_session=False)
    )
    await session.commit()
    report = DailyReport(payloads_pruned=pruned.rowcount or 0)
    for step in list(DAILY_STEPS):
        try:
            await step(session, now, redis)
            await session.commit()
        except Exception as exc:  # noqa: BLE001 - a failing step never blocks the next one
            await session.rollback()
            logger.warning(
                "payment_daily_step_failed",
                step=getattr(step, "__name__", "step"),
                error_type=type(exc).__name__,
            )
    return report
