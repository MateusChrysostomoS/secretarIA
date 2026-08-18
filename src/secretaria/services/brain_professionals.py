"""secretarIA -> brain-api client for professional CONTACT data (their email).

============================ READ THIS BEFORE EDITING ============================

`Professional` has no email column, and deliberately never will. brain-api is
the single writer of identity (the same rule `services/brain_onboarding.py`
states for onboarding state): a professional's address lives on brain-api's
`users.email`, linked by `users.professional_id`, and is written there by the
invite flow. Copying it into a `professionals.email` column here would create a
second copy with no propagation path — the day a doctor changes their address
in brain-api, secretarIA would keep mailing the old one, silently and forever.
So this module ASKS, per booking, instead of storing.

Sibling of `services/brain_onboarding.py` in every respect — short-lived
`httpx.AsyncClient(base_url=..., headers=...)` per call, `BRAIN_API_BASE_URL` /
`INTERNAL_API_KEY` from Settings, `X-Internal-Api-Key` as the shared pair
secret, fail-soft on every ambiguity. It differs only in what it asks about
(professionals rather than tenants).

Contract (brain-api, api/internal.py):
  - GET {BRAIN_API_BASE_URL}/internal/tenants/{tenant_id}/professional-emails
    -> 200 {"items": [{"professional_id": <uuid str>, "email": <str>}]}

Deliberately a BATCH read of the whole tenant, not a per-professional lookup:
the caller (plugins/professional_notification.py) runs once per booking, and a
per-id endpoint would invite an N+1 the moment anything wants two of them.

`fetch_professional_emails` returns `None` on any ambiguity (unconfigured
settings, network error, non-200, bad JSON/shape) so a caller can tell "the
lookup failed" apart from "the lookup succeeded and this tenant has nobody
linked" (an empty dict). Both outcomes end the same way for the notification
hook — no email goes out — but only one of them is worth a warning.

NEVER logs an email address (personal data): only the tenant id and counts.
Same rule `brain_onboarding.py` follows for `owner_email`.

=================================================================================
"""

from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


async def fetch_professional_emails(tenant_id: UUID) -> dict[str, str] | None:
    """`{professional_id: email}` for one tenant's linked professionals.

    Keys are professional ids as STRINGS — the caller compares against
    `str(appointment.professional_id)`, and keeping the wire shape avoids a
    parse step that could only ever throw away a row brain-api considers valid.

    Returns `None` when the answer is unknown (see the module docstring);
    `{}` when brain-api answered and nobody on this tenant has a linked user.
    """
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.warning("professional_emails_fetch_unconfigured", tenant_id=str(tenant_id))
        return None

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.get(f"/internal/tenants/{tenant_id}/professional-emails")
    except httpx.HTTPError as exc:
        logger.warning(
            "professional_emails_fetch_failed",
            reason="network_error",
            error=str(exc),
            tenant_id=str(tenant_id),
        )
        return None

    if response.status_code != 200:
        logger.warning(
            "professional_emails_fetch_failed",
            reason="non_200_status",
            status_code=response.status_code,
            tenant_id=str(tenant_id),
        )
        return None

    try:
        body = response.json()
    except ValueError as exc:
        logger.warning(
            "professional_emails_fetch_failed",
            reason="invalid_json",
            error=str(exc),
            tenant_id=str(tenant_id),
        )
        return None

    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        logger.warning(
            "professional_emails_fetch_failed", reason="bad_shape", tenant_id=str(tenant_id)
        )
        return None

    emails: dict[str, str] = {}
    for raw in body["items"]:
        if not isinstance(raw, dict):
            continue
        professional_id = str(raw.get("professional_id") or "").strip()
        email = str(raw.get("email") or "").strip()
        # A row missing either half is unusable, not fatal: skip it and keep
        # the rest, exactly like `brain_onboarding._item_from_dict`'s per-item
        # skip. Count-only logging — never the address itself.
        if professional_id and email:
            emails[professional_id] = email
    logger.info("professional_emails_fetched", tenant_id=str(tenant_id), linked_count=len(emails))
    return emails
