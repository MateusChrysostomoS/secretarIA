"""Usage-event emission — the billing-meter seam to brain-api.

============================ READ THIS BEFORE EDITING ============================

Contract (brain-api, being built in parallel — code against it exactly): POST
{BRAIN_API_BASE_URL}/internal/usage-events, header X-Internal-Api-Key:
<INTERNAL_API_KEY>, JSON {"tenant_id": "<uuid>", "feature": "<str>", "amount":
<int>, "event_id": "<idempotency key>"} -> 200 {"recorded": bool}. Idempotent
server-side on `event_id` — a duplicate POST (our own retry, or two callers
racing on the same key) is safe to send more than once.

This function NEVER raises — every failure mode (unconfigured settings,
network error, non-200, bad JSON) is caught, logged, and turned into a `False`
return. That is deliberate: today's only caller (plugins/reminders.py, the
billable HSM-template send) treats metering as FAIL-OPEN — the WhatsApp send
already happened, and the caller's own idempotency ledger (not this call) is
what actually prevents a resend. A metering miss here is, at worst, an
undercount to reconcile later — never a reason to re-send or crash the sweep.

=================================================================================
"""

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


async def emit_usage_event(*, tenant_id: str, feature: str, amount: int, event_id: str) -> bool:
    """POST one usage event to brain-api. Returns the `recorded` flag, or False on any failure."""
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.warning("usage_event_unconfigured", feature=feature, event_id=event_id)
        return False

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(
                "/internal/usage-events",
                json={
                    "tenant_id": tenant_id,
                    "feature": feature,
                    "amount": amount,
                    "event_id": event_id,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "usage_event_send_failed",
            reason="network_error",
            feature=feature,
            event_id=event_id,
            error=str(exc),
        )
        return False

    if response.status_code != 200:
        logger.warning(
            "usage_event_send_failed",
            reason="non_200_status",
            feature=feature,
            event_id=event_id,
            status_code=response.status_code,
        )
        return False

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning(
            "usage_event_send_failed",
            reason="invalid_json",
            feature=feature,
            event_id=event_id,
            error=str(exc),
        )
        return False

    return bool(body.get("recorded"))
