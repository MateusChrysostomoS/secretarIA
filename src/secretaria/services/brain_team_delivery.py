"""Live first-access delivery checks; no credentials returned or logged."""

from uuid import UUID

import httpx

from secretaria.config import get_settings


async def staff_delivery_request(
    tenant_id: UUID, user_id: UUID, version: int, status: str | None = None
) -> bool:
    settings = get_settings()
    if not settings.BRAIN_API_BASE_URL or not settings.INTERNAL_API_KEY:
        return False
    path = f"/internal/tenants/{tenant_id}/team/{user_id}/delivery"
    try:
        async with httpx.AsyncClient(
            base_url=settings.BRAIN_API_BASE_URL,
            headers={"X-Internal-Api-Key": settings.INTERNAL_API_KEY},
            timeout=settings.BRAIN_API_TIMEOUT_SECONDS,
        ) as client:
            if status:
                response = await client.post(
                    path, json={"credential_version": version, "status": status}
                )
                return response.status_code == 204
            response = await client.get(path, params={"credential_version": version})
            return response.status_code == 200 and response.json().get("deliverable") is True
    except (httpx.HTTPError, ValueError):
        return False
