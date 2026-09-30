"""A tiny fixed-window rate limiter on Redis, for endpoints that must not become an oracle.

The Asaas key endpoint verifies a secret against a third party; without a limit a stolen hub
token could use the clinic's screen to test guessed keys. It FAILS CLOSED: with no Redis the
caller gets `RateLimiterUnavailable` (a 503), never a free pass.
"""


class RateLimiterUnavailable(RuntimeError):
    """No Redis (or it failed): the limit cannot be enforced, so the action is not allowed."""


async def allow(redis, *, key: str, limit: int, window_seconds: int) -> bool:
    """Count one attempt on `key`; True while the count is within `limit` for the window."""
    if redis is None:
        raise RateLimiterUnavailable("no redis")
    try:
        count = await redis.incr(key)
        if count == 1:
            await redis.expire(key, window_seconds)
    except Exception as exc:  # noqa: BLE001 - any Redis failure is "cannot enforce"
        raise RateLimiterUnavailable(type(exc).__name__) from exc
    return count <= limit
