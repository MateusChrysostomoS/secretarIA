"""arq worker entry point.

Start the worker with:
    arq secretaria.workers.arq_worker.WorkerSettings
"""

from arq import cron
from arq.connections import RedisSettings

from secretaria.config import get_settings
from secretaria.core.database import engine
from secretaria.core.logging import get_logger, setup_logging
from secretaria.plugins.post_booking import run_post_booking_hooks
from secretaria.plugins.reminders import send_appointment_reminders
from secretaria.workers.tasks import (
    check_handover_timeouts,
    process_webhook_event,
    send_patient_notification,
)

logger = get_logger(__name__)


async def on_startup(ctx: dict) -> None:
    """Run once when the worker process starts."""
    setup_logging()
    logger.info("worker_started")


async def on_shutdown(ctx: dict) -> None:
    """Run once when the worker process stops."""
    await engine.dispose()
    logger.info("worker_stopped")


class WorkerSettings:
    """arq worker configuration.

    arq reads these as plain class attributes, so `redis_settings` must be a
    RedisSettings instance (not a classmethod / callable).
    """

    functions = [
        process_webhook_event,
        send_patient_notification,
        # post_booking plugin hooks (EHR push, Pix ask, analytics event, ...) —
        # enqueued off both booking commit points, never run inline. See
        # plugins/post_booking.py.
        run_post_booking_hooks,
    ]
    # Sweep stale human-handover conversations back to the bot every 15 min;
    # sweep upcoming appointments for due lead-window reminders every 5 min
    # (plugins/reminders.py — entitlement-gated, silent no-op per tenant/
    # appointment when not entitled).
    cron_jobs = [
        cron(check_handover_timeouts, minute={0, 15, 30, 45}),
        cron(
            send_appointment_reminders,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
