"""Subscription-token verification — the doctor-hub auth seam.

============================ READ THIS BEFORE EDITING ============================

`verify_subscription_token` turns an opaque bearer token from the doctor hub into
a `SubscriptionClaim` by asking brain-api (the identity + billing authority) to
verify it. brain-api owns the token's signature/expiry AND the tenant's plan
entitlement (active/trialing + `secretaria_enabled`); we never re-derive either
locally.

Contract (brain-api, already implemented, do not change): POST
{BRAIN_API_BASE_URL}/internal/secretaria/hub-token/verify, header
X-Internal-Api-Key: <INTERNAL_API_KEY>, body {"token": "<token>"}. 200 response:
{"active": bool, "tenant_id": "<uuid>" | null}. "active" is the LIVE answer —
token validity AND entitlement, in one call.

Everything here FAILS CLOSED: any ambiguity (unconfigured settings, network
error, timeout, bad response) returns None, which the caller (api/hub/deps.py)
turns into a 401. A short-lived in-process cache remembers POSITIVE results
only, so a revoked/canceled subscription re-locks within
SUBSCRIPTION_CACHE_TTL_SECONDS without us re-hitting brain-api on every request.

*** THIS FUNCTION IS THE ONLY PLACE THAT NEEDS TO CHANGE. *** Every hub endpoint
depends on api/hub/deps.py:get_current_tenant, which depends on this function.
Nothing else in the codebase reads the token directly.

The token itself is NEVER logged — only its sha256 hash (for the cache key) or
nothing at all.

=================================================================================
"""

import hashlib
import time
from dataclasses import dataclass
from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

# In-process cache of POSITIVE verification results: sha256(token).hexdigest()
# -> (monotonic expiry, claim). Never caches negative/refused results — a
# refusal always re-checks brain-api next time. Bounded below to avoid
# unbounded growth if it's never restarted.
_cache: dict[str, tuple[float, "SubscriptionClaim"]] = {}
_CACHE_MAX_SIZE = 512


@dataclass(frozen=True)
class SubscriptionClaim:
    """The result of validating a subscription token against brain-api.

    tenant_id: which tenant the token authorises. A real (non-None) claim
        always carries a concrete tenant_id.
    active:    whether the subscription is currently active/trialing AND
        secretaria_enabled, as reported by brain-api.
    """

    tenant_id: UUID | None
    active: bool


def _cache_key(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> "SubscriptionClaim | None":
    entry = _cache.get(key)
    if entry is None:
        return None
    expiry, claim = entry
    if time.monotonic() >= expiry:
        _cache.pop(key, None)
        return None
    return claim


def _cache_put(key: str, claim: "SubscriptionClaim", ttl_seconds: int) -> None:
    if ttl_seconds <= 0:
        return
    if len(_cache) > _CACHE_MAX_SIZE:
        # Simple, correct-enough bound: drop everything rather than evict
        # cleverly. A brief cache-cold spell is cheap; unbounded memory is not.
        _cache.clear()
    _cache[key] = (time.monotonic() + ttl_seconds, claim)


async def verify_subscription_token(token: str) -> SubscriptionClaim | None:
    """Validate a bearer token against brain-api and return its claim, or None.

    None means "treat as unauthenticated" — covers an empty token, missing
    config, a brain-api refusal (active=false), and any error talking to
    brain-api (fail closed).
    """
    if not token or not token.strip():
        return None

    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        # No brain-api to ask -> the hub is effectively locked. Fail closed.
        logger.warning("subscription_verify_unconfigured")
        return None

    key = _cache_key(token)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(
                "/internal/secretaria/hub-token/verify",
                json={"token": token},
            )
    except httpx.HTTPError as exc:
        logger.warning("subscription_verify_failed", reason="network_error", error=str(exc))
        return None

    if response.status_code != 200:
        logger.warning(
            "subscription_verify_failed",
            reason="non_200_status",
            status_code=response.status_code,
        )
        return None

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning("subscription_verify_failed", reason="invalid_json", error=str(exc))
        return None

    if not isinstance(body, dict) or not body.get("active"):
        return None

    raw_tenant_id = body.get("tenant_id")
    if not raw_tenant_id:
        logger.warning("subscription_verify_failed", reason="active_without_tenant_id")
        return None

    try:
        tenant_id = UUID(raw_tenant_id)
    except (ValueError, AttributeError, TypeError):
        logger.warning("subscription_verify_failed", reason="bad_tenant_id")
        return None

    claim = SubscriptionClaim(tenant_id=tenant_id, active=True)
    _cache_put(key, claim, settings.SUBSCRIPTION_CACHE_TTL_SECONDS)
    return claim
