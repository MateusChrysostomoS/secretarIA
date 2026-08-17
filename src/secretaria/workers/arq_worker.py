"""arq worker entry point.

Start the worker with:
    arq secretaria.workers.arq_worker.WorkerSettings
"""

import httpx
from arq import cron
from arq.connections import RedisSettings

from secretaria.config import get_settings
from secretaria.core.build_info import (
    build_identity,
    check_deploy_parity,
    publish_build_identity,
)
from secretaria.core.database import engine
from secretaria.core.logging import get_logger, setup_logging
from secretaria.plugins.post_booking import run_post_booking_hooks
from secretaria.plugins.reminders import send_appointment_reminders
from secretaria.workers.deploy_parity import check_deploy_parity_cron
from secretaria.workers.onboarding_cron import (
    run_onboarding_nudges,
    run_patient_usage_metering,
)
from secretaria.workers.payments_tasks import process_asaas_event
from secretaria.workers.tasks import (
    check_handover_timeouts,
    process_webhook_event,
    send_patient_notification,
    send_transactional_email,
    transcribe_audio_message,
)

logger = get_logger(__name__)


def registered_function_names() -> list[str]:
    """Job names arq will accept in `enqueue_job`, in registration order.

    Logged at startup so the registry of the DEPLOYED process is verifiable
    from its own log — an enqueue that fails with "unknown job" is otherwise
    indistinguishable from a worker running an older image.
    """
    return [getattr(func, "name", None) or func.__name__ for func in WorkerSettings.functions]


def registered_cron_names() -> list[str]:
    """Cron job names, in registration order. Same purpose as the above."""
    return [
        getattr(job, "name", None) or getattr(job.coroutine, "__name__", "?")
        for job in WorkerSettings.cron_jobs
    ]


async def on_startup(ctx: dict) -> None:
    """Run once when the worker process starts."""
    setup_logging()
    # Shared client for transcribe_audio_message: WhatsApp media download and
    # the OpenAI/Groq STT call both reuse this one connection pool instead of
    # opening a fresh httpx.AsyncClient per job.
    ctx["http_client"] = httpx.AsyncClient()
    # Build identity + registry on the very first line (FIX_01 §5.1): the
    # worker has no HTTP surface, so this log IS its only proof of which
    # commit it is running. Then announce it and compare against the API —
    # `deploy_sha_divergence` here means somebody deployed one service and
    # not the other. Both calls are fail-open.
    identity = build_identity("worker")
    logger.info(
        "worker_started",
        **identity.as_dict(),
        functions=registered_function_names(),
        cron_jobs=registered_cron_names(),
    )
    redis = ctx.get("redis")
    await publish_build_identity(redis, identity)
    await check_deploy_parity(redis, identity)


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
        # Pix deposit (sinal) webhook processing, enqueued by
        # POST /webhooks/asaas. See workers/payments_tasks.py and
        # services/payments/deposit_lifecycle.py.
        process_asaas_event,
    ]
    # Sweep stale human-handover conversations back to the bot every 15 min;
    # sweep upcoming appointments for due lead-window reminders every 5 min
    # (plugins/reminders.py — entitlement-gated, silent no-op per tenant/
    # appointment when not entitled).
    # run_onboarding_nudges (hourly at :10) and run_patient_usage_metering
    # (daily at 03:30 UTC) — contract v1 §11. See workers/onboarding_cron.py.
    # check_deploy_parity_cron (hourly at :07) re-announces this worker's build
    # identity and WARNs when the API is running different code — FIX_01 §5.2.
    # The minute is offset from every other cron above so the parity check
    # never shares a tick with real work.
    cron_jobs = [
        cron(check_handover_timeouts, minute={0, 15, 30, 45}),
        cron(
            send_appointment_reminders,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
        ),
        cron(run_onboarding_nudges, minute={10}),
        cron(run_patient_usage_metering, hour={3}, minute={30}),
        cron(check_deploy_parity_cron, minute={7}),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
