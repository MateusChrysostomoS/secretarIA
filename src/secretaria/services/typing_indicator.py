"""The "digitando…" flag, the way WhatsApp has it: whoever is typing, the other side sees it.

Three typers: the clinic's AUTOMATION (a turn is running - seconds to most of a minute), the
clinic's STAFF (a person typing in the console) and the PATIENT. Each is one Redis key per
conversation with a short TTL - ephemeral by nature, and a dead worker or a closed tab simply
expires instead of leaving "digitando" stuck.

Who sees whom: the patient sees the automation and the staff; the clinic sees the automation
and the patient. Nobody sees their own typing.

The patient's typing is only worth recording while a HUMAN conducts the conversation (the
callers check the handover state): with the automation conducting, no one reads it, so no
work is done for it.

EVERYTHING here fails OPEN and quiet. A flag that can break a turn or a request is worse
than no flag: Redis missing, erroring, or a test double without `exists` all read as "not
typing". Only the exception TYPE is logged - never a conversation's content.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from secretaria.core.logging import get_logger

logger = get_logger(__name__)
REDIS_BUDGET_SECONDS = 0.1

TypingBy = Literal["automation", "staff", "patient"]
Viewer = Literal["patient", "staff"]

# Automation: longer than a normal turn (the whole-turn budget is LLM_TURN_TIMEOUT_SECONDS).
# Humans: the client re-sends every ~3 s while a key is being typed, so 6 s rides out one lost beat.
TYPING_TTL_SECONDS: dict[str, int] = {"automation": 90, "staff": 6, "patient": 6}

# What each viewer can see, in priority order.
_VISIBLE_TO: dict[str, tuple[TypingBy, ...]] = {
    "patient": ("automation", "staff"),
    "staff": ("automation", "patient"),
}


# Each automation execution owns a member. Server-time deadlines keep crashed workers
# from surviving later executions that renew the key TTL. Lua makes prune/add/expire atomic.
_AUTOMATION_MARK = """
local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) + tonumber(stamp[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[2]), ARGV[1])
redis.call('EXPIRE', KEYS[1], ARGV[2])
return 1
"""
_AUTOMATION_READ = """
local stamp = redis.call('TIME')
local now = tonumber(stamp[1]) + tonumber(stamp[2]) / 1000000
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
return redis.call('ZCARD', KEYS[1])
"""
_SHARED_MARK = "shared"


def typing_key(conversation_id, by: str) -> str:
    return f"brain_message:typing:{conversation_id}:{by}"


async def mark_typing(redis, conversation_id, by: TypingBy, *, turn_id: str | None = None) -> None:
    if redis is None:
        return
    try:
        async with asyncio.timeout(REDIS_BUDGET_SECONDS):
            key = typing_key(conversation_id, by)
            if by == "automation":
                await redis.eval(
                    _AUTOMATION_MARK, 1, key, turn_id or _SHARED_MARK, TYPING_TTL_SECONDS[by]
                )
            else:
                await redis.setex(key, TYPING_TTL_SECONDS[by], "1")
    except Exception as exc:
        logger.warning(
            "typing_indicator_redis_failed", op="mark", by=by, error_type=type(exc).__name__
        )


async def clear_typing(redis, conversation_id, by: TypingBy, *, turn_id: str | None = None) -> None:
    if redis is None:
        return
    try:
        async with asyncio.timeout(REDIS_BUDGET_SECONDS):
            key = typing_key(conversation_id, by)
            if by == "automation":
                await redis.zrem(key, turn_id or _SHARED_MARK)
            else:
                await redis.delete(key)
    except Exception as exc:
        logger.warning(
            "typing_indicator_redis_failed", op="clear", by=by, error_type=type(exc).__name__
        )


async def typing_by(redis, conversation_id, *, viewer: Viewer) -> TypingBy | None:
    """Who the `viewer` should see typing right now, or None."""
    if redis is None:
        return None
    for by in _VISIBLE_TO[viewer]:
        try:
            async with asyncio.timeout(REDIS_BUDGET_SECONDS):
                key = typing_key(conversation_id, by)
                present = (
                    await redis.eval(_AUTOMATION_READ, 1, key)
                    if by == "automation"
                    else await redis.exists(key)
                )
                if present:
                    return by
        except Exception as exc:
            logger.warning(
                "typing_indicator_redis_failed", op="read", by=by, error_type=type(exc).__name__
            )
            return None
    return None
