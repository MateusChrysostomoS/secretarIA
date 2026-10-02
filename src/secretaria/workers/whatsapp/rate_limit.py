"""rate_limit - split out of workers/tasks.py (TASK-023)."""

from secretaria.config import get_settings
from secretaria.core.logging import get_logger, wa_suffix

logger = get_logger(__name__)


async def _is_rate_limited(redis, phone_number_id: str | None, wa_id: str) -> bool:
    """Sliding-window inbound rate limit per wa_id, backed by the arq Redis pool.

    Fail-open: when Redis is unavailable (or not passed, e.g. in tests) no
    message is dropped. Once a sender exceeds the window cap, a silence key
    suppresses them for RATE_LIMIT_SILENCE_SECONDS.
    """
    if redis is None:
        return False
    settings = get_settings()
    scope = phone_number_id or "default"
    silence_key = f"ratelimit:silence:{scope}:{wa_id}"
    count_key = f"ratelimit:count:{scope}:{wa_id}"
    try:
        if await redis.exists(silence_key):
            return True
        count = await redis.incr(count_key)
        if count == 1:
            await redis.expire(count_key, settings.RATE_LIMIT_WINDOW_SECONDS)
        if count > settings.RATE_LIMIT_MAX_MESSAGES:
            await redis.setex(silence_key, settings.RATE_LIMIT_SILENCE_SECONDS, "1")
            return True
    except Exception as exc:
        logger.warning(
            "worker_rate_limit_check_failed", error=str(exc), wa_id_suffix=wa_suffix(wa_id)
        )
        return False
    return False
