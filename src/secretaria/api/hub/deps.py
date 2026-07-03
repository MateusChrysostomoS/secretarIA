"""Shared FastAPI dependencies for the doctor hub.

`get_current_tenant` is the single authentication gate for every hub endpoint.
It turns an `Authorization: Bearer <token>` header into a Tenant row, delegating
token validation to core.subscription (which calls brain-api).
"""

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.database import get_session
from secretaria.core.subscription import verify_subscription_token
from secretaria.models import Tenant


def _bearer_token(authorization: str | None) -> str | None:
    """Extract the token from an Authorization header, tolerating a missing
    'Bearer ' prefix (handy for local curl/testing)."""
    if not authorization:
        return None
    parts = authorization.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return authorization.strip()


async def _resolve_tenant(session: AsyncSession, tenant_id) -> Tenant | None:
    """Load the claimed tenant, or the only tenant when the claim has no id.

    A real brain-api claim always carries a concrete tenant_id, so the
    single-tenant fallback below is dead in practice today; it stays as a
    defensive fallback rather than a relied-upon code path.
    """
    if tenant_id is not None:
        return await session.get(Tenant, tenant_id)
    rows = (await session.scalars(select(Tenant).limit(2))).all()
    return rows[0] if len(rows) == 1 else None


async def get_current_tenant(
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> Tenant:
    """Authenticate the request and return the caller's Tenant row.

    401 for a missing/invalid/inactive token, 404 when the token is valid but
    no tenant can be resolved.
    """
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing bearer token")

    claim = await verify_subscription_token(token)
    if claim is None or not claim.active:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive subscription token")

    tenant = await _resolve_tenant(session, claim.tenant_id)
    if tenant is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No tenant resolved for this subscription")
    return tenant
