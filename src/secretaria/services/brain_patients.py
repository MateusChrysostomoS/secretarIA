"""Read a Portal patient's booking contact from the identity authority.

The caller may refresh Patient.email, which is registered in the pseudonymizer.
The result interface distinguishes an authoritative empty contact from an
unavailable lookup. Only unavailable lookups permit the stored-copy fallback.
Never log the address, response payload or exception text.
"""

from dataclasses import dataclass
from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class PatientEmailResult:
    """Availability refers to a valid authority answer, including email=null."""

    available: bool = False
    email: str | None = None


async def fetch_patient_email(tenant_id: UUID, external_id: str) -> str | None:
    """Compatibility interface; callers needing authority use the result variant."""
    return (await fetch_patient_email_result(tenant_id, external_id)).email


async def fetch_patient_email_result(tenant_id: UUID, external_id: str) -> PatientEmailResult:
    """Fetch one clinic-scoped contact; external_id is the Portal UUID string."""
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        logger.info("patient_email_fetch_unconfigured")
        return PatientEmailResult()
    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            response = await client.post(
                "/internal/brain-message/patient-contact",
                json={"tenant_id": str(tenant_id), "external_id": external_id},
            )
    except httpx.HTTPError as exc:
        logger.warning(
            "patient_email_fetch_failed", reason="network_error", error_type=type(exc).__name__
        )
        return PatientEmailResult()
    if response.status_code != 200:
        logger.info("patient_email_fetch_unavailable", status=response.status_code)
        return PatientEmailResult()
    try:
        body = response.json()
    except ValueError:
        return PatientEmailResult()
    if not isinstance(body, dict):
        return PatientEmailResult()
    if "email" not in body:
        return PatientEmailResult()
    email = body["email"]
    if email is None:
        return PatientEmailResult(available=True)
    if isinstance(email, str):
        return PatientEmailResult(available=True, email=email.strip() or None)
    return PatientEmailResult()
