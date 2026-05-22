"""arq worker entry point.

Start the worker with:
    arq secretaria.workers.arq_worker.WorkerSettings
"""

from arq.connections import RedisSettings

from secretaria.config import get_settings
from secretaria.core.database import engine
from secretaria.core.logging import get_logger, setup_logging
from secretaria.workers.tasks import process_webhook_event

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

    functions = [process_webhook_event]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
