"""The same capacity check for hub and interactive identity provisioning."""

from sqlalchemy import func, select

from secretaria.models import Professional, Tenant
from secretaria.services.entitlements_client import get_entitlements, is_entitled


class ProfessionalCapacityError(Exception):
    def __init__(self, status: int, code: str):
        super().__init__(code)
        self.status = status
        self.code = code


async def require_professional_capacity(session, tenant, fetch_entitlements=None) -> None:
    await session.execute(select(Tenant.id).where(Tenant.id == tenant.id).with_for_update())
    fetch = fetch_entitlements or get_entitlements
    summary = await fetch(tenant.id, redis=None)
    if summary is None:
        raise ProfessionalCapacityError(503, "Could not verify entitlements right now")
    if not is_entitled(summary, "multi_professional"):
        raise ProfessionalCapacityError(403, "multi_professional_not_entitled")
    limit = summary.limits.get("professionals")
    count = await session.scalar(
        select(func.count())
        .select_from(Professional)
        .where(Professional.tenant_id == tenant.id, Professional.is_active.is_(True))
    )
    if limit is not None and count >= limit:
        raise ProfessionalCapacityError(409, "professional_limit_reached")
