"""arq cron wrappers of the payment jobs (services/payments/jobs.py).

Minutes are chosen so no payment tick shares a minute with any other cron
(`arq_worker.py`: handover 0/15/30/45, reminders every 5 min, nudges :10, deploy parity :07,
metering 03:30) or with each other. arq's `cron(..., unique=True)` (the default) keeps two
workers from running the same scheduled tick twice; the jobs are idempotent regardless.
"""

from dataclasses import asdict
from datetime import UTC, datetime

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger

# Imported for their side effects: `asaas_connection` registers the daily Asaas key recheck
# into `jobs.DAILY_STEPS`; `deposit_wait` registers the `before` expiry hook that frees the
# hold and tells the patient into `jobs.EXPIRY_HOOKS`.
from secretaria.services import deposit_wait  # noqa: F401
from secretaria.services.payments import (
    asaas_connection,  # noqa: F401
    attention_alerts,
    jobs,
)
from secretaria.workers.payments_tasks import enqueue_for

logger = get_logger(__name__)

# Odd minutes that are not multiples of 5 and not :07 -> about every 2 minutes.
EXPIRY_MINUTES = frozenset(minute for minute in range(1, 60, 2) if minute % 5 and minute != 7)
# About every 10 minutes, on even minutes (never an expiry minute).
RECONCILE_MINUTES = frozenset({2, 12, 22, 32, 42, 52})
# Both sweeps, about every 4-6 minutes, on even minutes that no other payment job uses.
SWEEP_MINUTES = frozenset({4, 8, 14, 18, 24, 28, 34, 38, 44, 48, 54, 58})
# Every ~10 minutes, on the even minutes no other cron uses: the e-mail of a paid cancellation.
ATTENTION_ALERT_MINUTES = frozenset({6, 16, 26, 36, 46, 56})
DAILY_HOUR = 4
DAILY_MINUTE = 17


async def run_payment_expiry(ctx: dict) -> None:
    async with async_session_factory() as session:
        report = await jobs.expire_due_charges(
            session, now=datetime.now(UTC), enqueue=enqueue_for(ctx)
        )
    if any(asdict(report).values()):
        logger.info("payment_expiry_tick", **asdict(report))


async def run_payment_reconciliation(ctx: dict) -> None:
    async with async_session_factory() as session:
        report = await jobs.reconcile_open_charges(
            session, now=datetime.now(UTC), enqueue=enqueue_for(ctx), redis=ctx.get("redis")
        )
    if any(asdict(report).values()):
        logger.info("payment_reconcile_tick", **asdict(report))


async def run_payment_sweeps(ctx: dict) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        events = await jobs.requeue_stuck_events(session, now=now, enqueue=enqueue_for(ctx))
        fulfillments = await jobs.requeue_stuck_fulfillments(
            session, now=now, enqueue=enqueue_for(ctx)
        )
        notices = await jobs.recover_paid_notices(session, now=now)
    if events or fulfillments or notices:
        logger.info("payment_sweep_tick", events=events, fulfillments=fulfillments, notices=notices)


async def run_attention_alerts(ctx: dict) -> None:
    """E-mail the clinic ONCE per item when a patient cancelled a paid consultation (answer 13)."""
    async with async_session_factory() as session:
        report = await attention_alerts.alert_new_items(session, now=datetime.now(UTC))
    if any(asdict(report).values()):
        logger.info("payment_attention_alert_tick", **asdict(report))


async def run_payment_daily(ctx: dict) -> None:
    async with async_session_factory() as session:
        report = await jobs.daily_maintenance(
            session, now=datetime.now(UTC), redis=ctx.get("redis")
        )
    logger.info("payment_daily_done", **asdict(report))
