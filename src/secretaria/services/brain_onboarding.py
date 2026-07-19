"""Onboarding cron -> brain-api client (cross-service contract v1 §4 endpoints 7/8).

============================ READ THIS BEFORE EDITING ============================

brain-api is the SINGLE WRITER of onboarding state (contract v1 §8: "single
writer: brain-api services/onboarding.py"). This module only READS the
onboarding tenant list and REPORTS events back — it never derives or stores
onboarding state locally. Mirrors the outbound-call shape already established
by `core/subscription.py` (hub-token verify) and `services/entitlements_client.py`
(entitlement reads): a short-lived `httpx.AsyncClient(base_url=..., headers=...)`
per call, `BRAIN_API_BASE_URL` / `INTERNAL_API_KEY` from Settings, fail-soft on
every ambiguity.

Contract (brain-api, already implemented, do not change):
  - GET  {BRAIN_API_BASE_URL}/internal/onboarding/tenants
    -> 200 {"items": [{tenant_id, onboarding_state, blocker_reason,
       config_status, onboarding_anchor_at, next_retry_at, retry_paused,
       config_reminder_paused, config_reminder_anchor_at,
       last_config_reminder_at, closing_email_sent_at,
       manual_review_flagged_at, owner_email, owner_name, clinic_name,
       subscription_active}]}
  - POST {BRAIN_API_BASE_URL}/internal/onboarding/tenants/{tenant_id}/events
    body {"event": "retry_nudge_sent"|"config_reminder_sent"|
       "closing_email_sent"|"manual_review_flagged", "at": <iso datetime>,
       "next_retry_at": <iso datetime>|null} -> 200 {"applied": bool}

Both header X-Internal-Api-Key: <INTERNAL_API_KEY> (same shared secret sent
outbound by core/subscription.py / entitlements_client.py).

`list_onboarding_tenants` returns `None` on any ambiguity (unconfigured
settings, network error, non-200, bad JSON/shape) so the cron can tell "the
pull failed, do nothing this tick" apart from "the pull succeeded and found
zero tenants" (empty list). A single malformed ITEM inside an otherwise-good
response is skipped (logged) rather than failing the whole pull — one bad row
must not blind the sweep to every other tenant.

`post_onboarding_event` NEVER raises — every failure mode is caught, logged,
and turned into `False` (mirrors `services/usage_events.py:emit_usage_event`
exactly). The one-shot guard for closing/manual-review events lives
server-side (brain-api); a duplicate POST from a retried tick is safe.

Nothing here ever logs `owner_email`/`owner_name` (personal data) — only
tenant_id and, where useful, the event/template name.

=================================================================================
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class OnboardingTenant:
    """One row of brain-api's `GET /internal/onboarding/tenants` response."""

    tenant_id: UUID
    onboarding_state: str
    blocker_reason: str | None
    config_status: str
    onboarding_anchor_at: datetime | None
    next_retry_at: datetime | None
    retry_paused: bool
    config_reminder_paused: bool
    config_reminder_anchor_at: datetime | None
    last_config_reminder_at: datetime | None
    closing_email_sent_at: datetime | None
    manual_review_flagged_at: datetime | None
    owner_email: str | None
    owner_name: str | None
    clinic_name: str | None
    subscription_active: bool


def _parse_dt(value: object) -> datetime | None:
    """Parse an ISO-ish datetime from JSON. Always returns tz-aware (UTC when
    the source was naive/ambiguous) so callers can freely subtract/compare —
    mirrors the `_as_utc` idiom already used in plugins/reminders.py /
    workers/tasks.py. None on any falsy/unparseable input."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _item_from_dict(data: dict) -> OnboardingTenant:
    return OnboardingTenant(
        tenant_id=UUID(str(data["tenant_id"])),
        onboarding_state=str(data["onboarding_state"]),
        blocker_reason=data.get("blocker_reason"),
        config_status=str(data["config_status"]),
        onboarding_anchor_at=_parse_dt(data.get("onboarding_anchor_at")),
        next_retry_at=_parse_dt(data.get("next_retry_at")),
        retry_paused=bool(data.get("retry_paused", False)),
        config_reminder_paused=bool(data.get("config_reminder_paused", False)),
        config_reminder_anchor_at=_parse_dt(data.get("config_reminder_anchor_at")),
        last_config_reminder_at=_parse_dt(data.get("last_config_reminder_at")),
        closing_email_sent_at=_parse_dt(data.get("closing_email_sent_at")),
        manual_review_flagged_at=_parse_dt(data.get("manual_review_flagged_at")),
        owner_email=data.get("owner_email"),
        owner_name=data.get("owner_name"),
        clinic_name=data.get("clinic_name"),
        subscription_active=bool(data.get("subscription_active", False)),
    )


async def list_onboarding_tenants() -> list[OnboardingTenant] | None:
    """Fetch the onboarding tenant list from brain-api.

    Returns `None` on any ambiguity (unconfigured settings, network error,
    non-200, bad JSON/shape) — the caller must treat that as "do nothing this
    tick", distinct from a legitimate empty list. A malformed individual item
    is skipped (logged) rather than failing the whole pull.
    """
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.warning("onboarding_tenants_fetch_unconfigured")
        return None

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.get("/internal/onboarding/tenants")
    except httpx.HTTPError as exc:
        logger.warning("onboarding_tenants_fetch_failed", reason="network_error", error=str(exc))
        return None

    if response.status_code != 200:
        logger.warning(
            "onboarding_tenants_fetch_failed",
            reason="non_200_status",
            status_code=response.status_code,
        )
        return None

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning("onboarding_tenants_fetch_failed", reason="invalid_json", error=str(exc))
        return None

    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        logger.warning("onboarding_tenants_fetch_failed", reason="bad_shape")
        return None

    items: list[OnboardingTenant] = []
    for raw in body["items"]:
        try:
            items.append(_item_from_dict(raw))
        except (KeyError, TypeError, ValueError) as exc:
            logger.warning("onboarding_tenant_item_skipped", error=str(exc))
    return items


async def post_onboarding_event(
    tenant_id: UUID,
    event: str,
    at: datetime,
    next_retry_at: datetime | None = None,
) -> bool:
    """POST one onboarding event to brain-api. Returns `applied`, or False on
    any failure. NEVER raises (mirrors services/usage_events.py:emit_usage_event) —
    a cron tick must never abort because one event POST failed."""
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.warning(
            "onboarding_event_unconfigured", onboarding_event=event, tenant_id=str(tenant_id)
        )
        return False

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(
                f"/internal/onboarding/tenants/{tenant_id}/events",
                json={
                    "event": event,
                    "at": at.isoformat(),
                    "next_retry_at": next_retry_at.isoformat() if next_retry_at else None,
                },
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "onboarding_event_send_failed",
            reason="network_error",
            onboarding_event=event,
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        return False

    if response.status_code != 200:
        logger.warning(
            "onboarding_event_send_failed",
            reason="non_200_status",
            onboarding_event=event,
            tenant_id=str(tenant_id),
            status_code=response.status_code,
        )
        return False

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning(
            "onboarding_event_send_failed",
            reason="invalid_json",
            onboarding_event=event,
            tenant_id=str(tenant_id),
            error=str(exc),
        )
        return False

    return bool(body.get("applied"))
