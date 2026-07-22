"""Entitlement summary reader — the plugin/tier gating seam.

============================ READ THIS BEFORE EDITING ============================

`get_entitlements` turns a tenant_id into an `EntitlementSummary` by asking
brain-api (the identity + billing authority) which addons/tier a tenant is
currently entitled to. brain-api owns the plan/addon truth; we never re-derive
it locally — `is_entitled` below only ever *reads* the summary it returned.

Contract (brain-api, already implemented, do not change): GET
{BRAIN_API_BASE_URL}/internal/tenants/{tenant_id}/entitlements, header
X-Internal-Api-Key: <INTERNAL_API_KEY>. 200 response: {"tenant_id", "status",
"active": bool, "secretaria_enabled": bool, "plan", "secretaria_tier":
"ferro"|"bronze_1"|"bronze_2"|null, "addons": {<9 addon ids>: bool},
"limits": {<keys>: int}}. `addons` is a FULL keyset — plan-implied addons are
already resolved to True by brain-api, we do not add fallback logic on top.

Caching (Redis-backed, bounds both brain-api call volume AND how fast a
revocation actually locks the bot out):
  - A FRESH copy lives at `ent:{tenant_id}` for ENTITLEMENT_CACHE_TTL_SECONDS
    (config.py). THIS is the revocation-latency ceiling for the bot reply path
    (workers/tasks.py) — `<= 0` disables fresh caching (every call re-fetches).
  - A STALE copy lives at `ent:stale:{tenant_id}` for 24h, rewritten on every
    successful fetch REGARDLESS of the fresh TTL setting. It is read ONLY when
    a fresh fetch fails (network error, non-200, unconfigured), so a brief
    brain-api outage degrades to "whatever we last knew" instead of locking
    every tenant out at once. No stale copy available -> None (fail closed;
    the caller must treat None as "not entitled to anything").
  - `redis=None` (unit tests, or callers with no Redis pool) skips caching
    entirely and always fetches directly.

Never logged: tenant_id and status are fine; the cached JSON blob (the
summary itself) never appears in a log line, and neither does the Redis key.

=================================================================================
"""

import json
from dataclasses import asdict, dataclass
from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

# Subscription tiers, cheapest first. Cumulative: a tenant on a higher tier is
# automatically entitled to every lower tier's tier-gated capability.
TIERS: tuple[str, ...] = ("ferro", "bronze_1", "bronze_2")

# The fixed addon keyset brain-api resolves per tenant (see contract above).
# MUST stay in sync with brain-api's catalog.ADDON_IDS — is_entitled() raises on any
# key not listed here, so a gate on a missing key would crash instead of failing closed.
ADDON_IDS: frozenset[str] = frozenset(
    {
        "reactivation_pack",
        "verified_identity",
        "multi_professional",
        "multi_unit",
        "ehr",
        "pix_deposit",
        "analytics_bi",
        "analytics_bi_advanced",
        "human_backup_24_7",
    }
)

_STALE_TTL_SECONDS = 86400


@dataclass(frozen=True)
class EntitlementSummary:
    """A tenant's live entitlement state, as reported by brain-api."""

    tenant_id: str
    status: str
    active: bool
    secretaria_enabled: bool
    plan: str | None
    secretaria_tier: str | None
    addons: dict[str, bool]
    limits: dict[str, int]


def _tier_rank(tier: str | None) -> int:
    """Ordinal rank of a tier name. Unknown/None ranks below every real tier."""
    if tier is None:
        return -1
    try:
        return TIERS.index(tier)
    except ValueError:
        return -1


def is_entitled(summary: EntitlementSummary, key: str) -> bool:
    """Whether `summary`'s tenant is entitled to `key`.

    `key` is either an addon id (one of ADDON_IDS) or a tier name (one of
    TIERS). Mirrors brain-api's own semantics exactly:

    - An inactive tenant (`not summary.active`) is entitled to nothing.
    - A tier name is entitled iff the tenant's tier rank is >= that tier's
      rank (cumulative — bronze_2 implies bronze_1 and ferro).
    - An addon id is entitled iff `summary.addons.get(key) is True`.
    - Anything else is a caller bug (a plugin misconfigured its
      `entitlement_key`) -> raise loudly instead of silently returning False.
    """
    if not summary.active:
        return False
    if key in TIERS:
        return _tier_rank(summary.secretaria_tier) >= _tier_rank(key)
    if key in ADDON_IDS:
        return summary.addons.get(key) is True
    raise ValueError(f"is_entitled: unknown entitlement key {key!r}")


def _fresh_key(tenant_id: UUID) -> str:
    return f"ent:{tenant_id}"


def _stale_key(tenant_id: UUID) -> str:
    return f"ent:stale:{tenant_id}"


def _summary_from_dict(data: dict) -> EntitlementSummary:
    return EntitlementSummary(
        tenant_id=str(data["tenant_id"]),
        status=str(data["status"]),
        active=bool(data["active"]),
        secretaria_enabled=bool(data["secretaria_enabled"]),
        plan=data.get("plan"),
        secretaria_tier=data.get("secretaria_tier"),
        addons=dict(data.get("addons") or {}),
        limits=dict(data.get("limits") or {}),
    )


async def _redis_get_summary(redis, key: str) -> EntitlementSummary | None:
    try:
        raw = await redis.get(key)
    except Exception as exc:
        logger.warning("entitlements_cache_read_failed", error=str(exc))
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
        return _summary_from_dict(data)
    except (TypeError, ValueError, KeyError) as exc:
        logger.warning("entitlements_cache_corrupt", error=str(exc))
        return None


async def _redis_put_summary(
    redis, key: str, summary: EntitlementSummary, ttl_seconds: int
) -> None:
    if ttl_seconds <= 0:
        return
    try:
        await redis.setex(key, ttl_seconds, json.dumps(asdict(summary)))
    except Exception as exc:
        logger.warning("entitlements_cache_write_failed", error=str(exc))


async def _fetch_entitlements(tenant_id: UUID) -> EntitlementSummary | None:
    """Direct (uncached) fetch from brain-api. None on any ambiguity (fail closed)."""
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.warning("entitlements_unconfigured", tenant_id=str(tenant_id))
        return None

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.get(f"/internal/tenants/{tenant_id}/entitlements")
    except httpx.HTTPError as exc:
        logger.warning(
            "entitlements_fetch_failed",
            reason="network_error",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "entitlements_fetch_failed",
            reason="non_200_status",
            tenant_id=str(tenant_id),
            status_code=response.status_code,
        )
        return None

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning(
            "entitlements_fetch_failed",
            reason="invalid_json",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        return None

    try:
        return _summary_from_dict(body)
    except (KeyError, TypeError) as exc:
        logger.warning(
            "entitlements_fetch_failed",
            reason="bad_shape",
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        return None


async def get_entitlements(tenant_id: UUID, redis) -> EntitlementSummary | None:
    """Redis-cached entitlement read. See module docstring for the cache contract.

    `redis=None` skips caching entirely (direct fetch every call — the shape
    tests and any caller without a Redis pool want).
    """
    if redis is None:
        return await _fetch_entitlements(tenant_id)

    fresh_key = _fresh_key(tenant_id)
    cached = await _redis_get_summary(redis, fresh_key)
    if cached is not None:
        return cached

    settings = get_settings()
    summary = await _fetch_entitlements(tenant_id)
    if summary is not None:
        stale_key = _stale_key(tenant_id)
        await _redis_put_summary(redis, fresh_key, summary, settings.ENTITLEMENT_CACHE_TTL_SECONDS)
        await _redis_put_summary(redis, stale_key, summary, _STALE_TTL_SECONDS)
        return summary

    stale = await _redis_get_summary(redis, _stale_key(tenant_id))
    if stale is not None:
        logger.warning("entitlements_stale_used", tenant_id=str(tenant_id))
        return stale
    return None
