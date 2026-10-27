"""secretarIA -> brain-api: does this clinic have a WORKING secretary? (TASK-052 Task 16).

When the clinic releases a PAID slot, the patient's decision deadline B is 30 minutes before
closing (services/release_deadlines.py). Whose closing (owner, 2026-10-12): the CLINIC's when it
has a secretary (someone answers the clinic's phone until it closes), else the consultation
DOCTOR's. The role in the hub token of whoever presses "liberar" cannot answer it (it says who
acts, not who the clinic has), and secretarIA keeps no copy of the team: brain-api, the single
writer of identity, is ASKED at each release - no cache, no manual field.

Contract (brain-api, api/internal.py):
  - GET {BRAIN_API_BASE_URL}/internal/tenants/{tenant_id}/staff-roles
    header X-Internal-Api-Key: INTERNAL_API_KEY (the pair secret)
    -> 200 {"roles": ["doctor", "manager", "secretary", ...]}  distinct roles of the ACTIVE
       members (not revoked, no pending access, usable account); 404 unknown tenant.

`fetch_has_secretary` -> True ("secretary" present) / False (absent) / None on ANY ambiguity:
unconfigured settings, network error or timeout (`BRAIN_API_TIMEOUT_SECONDS`), non-200 (404
included), invalid JSON, a shape the contract does not describe. None = unknown, and the caller
uses the doctor's closing (owner-approved fallback) - a release never fails because of this.

Same client conventions as `services/brain_professionals.py`. Logs carry only the tenant id, a
reason and a status code or exception TYPE - never the URL, the key, the body or a person.
"""

from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)

ROLE_SECRETARY = "secretary"


def _unknown(tenant_id: UUID, reason: str, **fields) -> None:
    logger.warning("staff_roles_fetch_failed", reason=reason, tenant_id=str(tenant_id), **fields)


async def fetch_has_secretary(tenant_id: UUID) -> bool | None:
    """True/False from brain-api's live answer; None when it cannot be known (see module)."""
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        _unknown(tenant_id, "unconfigured")
        return None

    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.get(f"/internal/tenants/{tenant_id}/staff-roles")
    except httpx.HTTPError as exc:
        _unknown(tenant_id, "network_error", error_type=type(exc).__name__)
        return None

    if response.status_code != 200:
        _unknown(tenant_id, "non_200_status", status_code=response.status_code)
        return None

    try:
        body = response.json()
    except ValueError:
        _unknown(tenant_id, "invalid_json")
        return None

    roles = body.get("roles") if isinstance(body, dict) else None
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        _unknown(tenant_id, "bad_shape")
        return None

    has_secretary = ROLE_SECRETARY in roles
    logger.info("staff_roles_fetched", tenant_id=str(tenant_id), has_secretary=has_secretary)
    return has_secretary
