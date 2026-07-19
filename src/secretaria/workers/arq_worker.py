"""arq worker entry point.

Start the worker with:
    arq secretaria.workers.arq_worker.WorkerSettings
"""

import httpx
from arq import cron
from arq.connections import RedisSettings

from secretaria.config import get_settings
from secretaria.core.database import engine
from secretaria.core.logging import get_logger, setup_logging
from secretaria.plugins.post_booking import run_post_booking_hooks
from secretaria.plugins.reminders import send_appointment_reminders
from secretaria.workers.onboarding_cron import (
    run_onboarding_nudges,
    run_patient_usage_metering,
)
from secretaria.workers.tasks import (
    check_handover_timeouts,
    process_webhook_event,
    send_patient_notification,
    send_transactional_email,
    transcribe_audio_message,
)

logger = get_logger(__name__)


async def on_startup(ctx: dict) -> None:
    """Run once when the worker process starts."""
    setup_logging()
    # Shared client for transcribe_audio_message: WhatsApp media download and
    # the OpenAI/Groq STT call both reuse this one connection pool instead of
    # opening a fresh httpx.AsyncClient per job.
    ctx["http_client"] = httpx.AsyncClient()
    logger.info("worker_started")


async def on_shutdown(ctx: dict) -> None:
    """Run once when the worker process stops."""
    client = ctx.get("http_client")
    if client is not None:
        await client.aclose()
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
        # Dedicated voice-note job, enqueued directly by the webhook handler
        # alongside process_webhook_event (not dispatched from within it).
        # See workers/tasks.py:transcribe_audio_message.
        transcribe_audio_message,
        # post_booking plugin hooks (EHR push, Pix ask, analytics event, ...) —
        # enqueued off both booking commit points, never run inline. See
        # plugins/post_booking.py.
        run_post_booking_hooks,
        # Onboarding transactional email (contract v1 §4 endpoint 6), enqueued
        # by POST /internal/notifications/email. See workers/tasks.py and
        # services/email.py.
        send_transactional_email,
    ]
    # Sweep stale human-handover conversations back to the bot every 15 min;
    # sweep upcoming appointments for due lead-window reminders every 5 min
    # (plugins/reminders.py — entitlement-gated, silent no-op per tenant/
    # appointment when not entitled).
    # run_onboarding_nudges (hourly at :10) and run_patient_usage_metering
    # (daily at 03:30 UTC) — contract v1 §11. See workers/onboarding_cron.py.
    cron_jobs = [
        cron(check_handover_timeouts, minute={0, 15, 30, 45}),
        cron(
            send_appointment_reminders,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        cron(run_onboarding_nudges, minute={10}),
        cron(run_patient_usage_metering, hour={3}, minute={30}),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
