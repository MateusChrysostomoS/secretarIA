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

Contract (brain-api). POST {BRAIN_API_BASE_URL}/internal/precheck-handoff,
header X-Internal-Api-Key: <INTERNAL_API_KEY>, body {"tenant_id": "<uuid
string>", "phone_number": "<digits, 8-15>"} — plus, OPTIONALLY, the booking
context added by FEAT 39: "patient_name" and "booked_service" (<= 255 chars
each; sent only when non-blank, and the key is omitted entirely otherwise, so
a call that passes neither produces byte-identically the two-field body above).

ADDING A FIELD HERE IS NOT A LOCAL CHANGE. brain-api's `PrecheckHandoffIn` is
`extra="forbid"`: a field name it does not yet know does not get ignored, it
422s the WHOLE request — and a 422 lands in the fail-closed bucket below as an
UNAVAILABLE indistinguishable from an outage, silently killing the
already-shipped automatic trigger (plugins/precheck_handoff.py) that needs none
of the new fields. So brain-api must accept a new field, LIVE in production,
BEFORE this module starts sending it — never the other way round. See the
`frozen-contract-migration` skill. Verified live on 2026-08-26 before this
module was allowed to send them: brain-api's published OpenAPI carries
patient_name + booked_service on PrecheckHandoffIn, and so does PreCheck's own
PrecheckHandoffRequest one hop further in.

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
X-Internal-Api-Key value is never logged at all. `patient_name` is
patient-identifying free text: it travels in the request BODY and nowhere else.
No log line in this module takes it — not on the happy path, and not on any
failure path, where only the hashed phone, the status code and the exception
string are recorded. Sending a value and logging it are different acts, and
only the first one is in scope here.

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


def _context_field(value: str | None) -> str | None:
    """One optional context field, normalized: blank and absent are the same thing.

    A `patient_name` that is really `""` or `"   "` is not a name — forwarding
    it as one would have PreCheck address the patient as an empty string, which
    reads worse than not knowing the name at all. Both collapse to None, and
    None means the key is left out of the body rather than sent as null.
    """
    cleaned = (value or "").strip()
    return cleaned or None


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


async def request_precheck_handoff(
    tenant_id: UUID,
    phone_number: str,
    *,
    patient_name: str | None = None,
    booked_service: str | None = None,
) -> HandoffResult:
    """Ask brain-api to pre-seed a PreCheck session for `phone_number` under `tenant_id`.

    Fails closed to UNAVAILABLE on any ambiguity (unconfigured settings,
    network error, timeout, unexpected status/body) — never raises into the
    caller (the agent tool wrapping this).

    `patient_name` / `booked_service` are optional booking context (FEAT 39),
    forwarded verbatim for PreCheck to open the questionnaire already knowing
    who it is talking to and what they booked. Both are KEYWORD-ONLY on
    purpose: they are two adjacent `str | None` with no shape that tells them
    apart, so a positional swap would quietly ship the patient's name as the
    service they booked, with nothing anywhere raising. Both are also optional
    on purpose — every caller that does not know either value keeps working
    unchanged, and `None` here is the normal case (a block slot, an untyped
    appointment), not an error.
    """
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.warning("precheck_handoff_unconfigured", tenant_id=str(tenant_id))
        return HandoffResult(HandoffOutcome.UNAVAILABLE)

    body: dict[str, str] = {"tenant_id": str(tenant_id), "phone_number": phone_number}
    # Absent keys, never null ones: `extra="forbid"` on the other side polices
    # unknown NAMES, but omitting a key we have nothing for also keeps the
    # no-context payload identical to the pre-FEAT-39 one, which is what makes
    # this module safe to deploy on its own.
    for key, value in (
        ("patient_name", _context_field(patient_name)),
        ("booked_service", _context_field(booked_service)),
    ):
        if value is not None:
            body[key] = value

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(
                "/internal/precheck-handoff",
                json=body,
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
