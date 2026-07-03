"""Precheck hand-off — asks brain-api (the mesh hub) to pre-seed a PreCheck
conversation session for a patient's phone, bound to the right clinic.

============================ READ THIS BEFORE EDITING ============================

DIRECTION CHANGE from the old (dead) version of this module: secretarIA used
to be designed to call PreCheck directly (PRECHECK_BASE_URL/PRECHECK_API_KEY).
That violated the mesh's hub-and-spoke trust shape — every cross-service call
goes through brain-api, never spoke-to-spoke. `request_precheck_handoff` below
calls brain-api instead; brain-api verifies entitlement AND has PreCheck
pre-seed the session. secretarIA only ever sends the patient a wa.me deep link
to PreCheck's shared number afterwards — the prefilled text on that link is
COSMETIC, routing already happened server-side.

Contract (brain-api, frozen, built in parallel — do not change): POST
{BRAIN_API_BASE_URL}/internal/precheck-handoff, header X-Internal-Api-Key:
<INTERNAL_API_KEY>, body {"tenant_id": "<uuid string>", "phone_number":
"<digits, 8-15>"}.

Responses:
  200 {"status": "seeded" | "already_active"} -> SEEDED / ALREADY_ACTIVE
  403 precheck_not_entitled                   -> NOT_ENTITLED
  404 no_clinic_for_tenant                    -> NO_CLINIC
  409 conflicting_active_session              -> CONFLICT
  503 not configured / 502 upstream failure   -> UNAVAILABLE
  network error / unconfigured BRAIN_API_BASE_URL or INTERNAL_API_KEY
                                               -> UNAVAILABLE

Everything here FAILS CLOSED into UNAVAILABLE — this function never raises
into the calling agent tool. Same base URL + key pattern, and the same
httpx usage/timeouts, as core/subscription.py and services/entitlements_client.py.

The phone number is never logged in full — only a sha256 hash (enough to
correlate log lines for the same number, never enough to recover it). The
X-Internal-Api-Key value is never logged at all.

=================================================================================
"""

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


class HandoffOutcome(StrEnum):
    """Result of asking brain-api to pre-seed a PreCheck session."""

    SEEDED = "seeded"
    ALREADY_ACTIVE = "already_active"
    NOT_ENTITLED = "not_entitled"
    NO_CLINIC = "no_clinic"
    CONFLICT = "conflict"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class HandoffResult:
    """The outcome of one `request_precheck_handoff` call."""

    outcome: HandoffOutcome


def _phone_hash(phone_number: str) -> str:
    """Short, non-reversible fingerprint of a phone number, safe to log."""
    return hashlib.sha256(phone_number.encode("utf-8")).hexdigest()[:16]


def _log_outcome(
    tenant_id: UUID, phone_number: str, outcome: HandoffOutcome, **extra: object
) -> None:
    logger.info(
        "precheck_handoff_result",
        tenant_id=str(tenant_id),
        outcome=outcome.value,
        phone_hash=_phone_hash(phone_number),
        **extra,
    )


async def request_precheck_handoff(tenant_id: UUID, phone_number: str) -> HandoffResult:
    """Ask brain-api to pre-seed a PreCheck session for `phone_number` under `tenant_id`.

    Fails closed to UNAVAILABLE on any ambiguity (unconfigured settings,
    network error, timeout, unexpected status/body) — never raises into the
    caller (the agent tool wrapping this).
    """
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.warning("precheck_handoff_unconfigured", tenant_id=str(tenant_id))
        return HandoffResult(HandoffOutcome.UNAVAILABLE)

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(
                "/internal/precheck-handoff",
                json={"tenant_id": str(tenant_id), "phone_number": phone_number},
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "precheck_handoff_failed",
            reason="network_error",
            tenant_id=str(tenant_id),
            phone_hash=_phone_hash(phone_number),
            error=str(exc),
        )
        return HandoffResult(HandoffOutcome.UNAVAILABLE)

    if response.status_code == 200:
        try:
            body = response.json()
        except ValueError as exc:
            logger.warning(
                "precheck_handoff_failed",
                reason="invalid_json",
                tenant_id=str(tenant_id),
                phone_hash=_phone_hash(phone_number),
                error=str(exc),
            )
            return HandoffResult(HandoffOutcome.UNAVAILABLE)

        status = body.get("status") if isinstance(body, dict) else None
        if status == "seeded":
            outcome = HandoffOutcome.SEEDED
        elif status == "already_active":
            outcome = HandoffOutcome.ALREADY_ACTIVE
        else:
            logger.warning(
                "precheck_handoff_failed",
                reason="unexpected_status_body",
                tenant_id=str(tenant_id),
                phone_hash=_phone_hash(phone_number),
                body_status=status,
            )
            return HandoffResult(HandoffOutcome.UNAVAILABLE)
        _log_outcome(tenant_id, phone_number, outcome)
        return HandoffResult(outcome)

    status_outcome = {
        403: HandoffOutcome.NOT_ENTITLED,
        404: HandoffOutcome.NO_CLINIC,
        409: HandoffOutcome.CONFLICT,
    }.get(response.status_code)

    if status_outcome is not None:
        _log_outcome(tenant_id, phone_number, status_outcome)
        return HandoffResult(status_outcome)

    # 502/503 and anything else unexpected: fail closed.
    logger.warning(
        "precheck_handoff_failed",
        reason="non_200_status",
        tenant_id=str(tenant_id),
        phone_hash=_phone_hash(phone_number),
        status_code=response.status_code,
    )
    return HandoffResult(HandoffOutcome.UNAVAILABLE)
