"""Doctor hub — professionals CRUD (multi_professional addon).

GET   /tenants/me/professionals      - list (always allowed, even when the
                                        addon is disabled — a tenant that lost
                                        the addon can still see what it had).
POST  /tenants/me/professionals      - create.
PATCH /tenants/me/professionals/{id} - update (name, google_calendar_id, is_active).

Entitlement + limit enforcement (brain-api is the source of truth, fetched
fresh — `redis=None` — since this path is not hot):
  - Addon disabled -> creating, or activating (is_active: false -> true), a
    professional is rejected with 403 {"detail": "multi_professional_not_entitled"}.
  - Creating/activating an ACTIVE professional beyond `limits["professionals"]`
    is rejected with 409 {"detail": "professional_limit_reached"}.
  - A failed entitlement fetch (None) fails CLOSED: 503, never silently allowed.
  - Renaming, changing the calendar id, or DEACTIVATING never touch
    entitlements/limits at all — those are always allowed regardless of addon
    state, so a downgraded tenant can still tidy up its existing rows.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.professional import Professional
from secretaria.schemas.professional import ProfessionalCreate, ProfessionalRead, ProfessionalUpdate
from secretaria.services.entitlements_client import get_entitlements, is_entitled

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/professionals", tags=["hub-professionals"])

ADDON_KEY = "multi_professional"
LIMIT_KEY = "professionals"


def _read_model(professional: Professional) -> ProfessionalRead:
    return ProfessionalRead(
        id=str(professional.id),
        name=professional.name,
        google_calendar_id=professional.google_calendar_id,
        is_active=professional.is_active,
        created_at=professional.created_at,
    )


async def _get_professional(
    session: AsyncSession, tenant: Tenant, professional_id: str
) -> Professional:
    try:
        prof_uuid = UUID(professional_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found") from None
    professional = await session.scalar(
        select(Professional).where(
            Professional.id == prof_uuid, Professional.tenant_id == tenant.id
        )
    )
    if professional is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found")
    return professional


async def _count_active(session: AsyncSession, tenant_id: UUID) -> int:
    result = await session.scalar(
        select(func.count())
        .select_from(Professional)
        .where(Professional.tenant_id == tenant_id, Professional.is_active.is_(True))
    )
    return int(result or 0)


async def _require_entitled_within_limit(session: AsyncSession, tenant: Tenant) -> None:
    """Gate an activate/create-active mutation. Raises HTTPException on failure.

    Fetches entitlements fresh (redis=None — this path is not hot). None
    (fetch failure) fails CLOSED: 503, never treated as "allowed".
    """
    summary = await get_entitlements(tenant.id, redis=None)
    if summary is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not verify entitlements right now"
        )
    if not is_entitled(summary, ADDON_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "multi_professional_not_entitled")

    limit = summary.limits.get(LIMIT_KEY)
    if limit is not None:
        active_count = await _count_active(session, tenant.id)
        if active_count >= limit:
            raise HTTPException(status.HTTP_409_CONFLICT, "professional_limit_reached")


@router.get("", response_model=list[ProfessionalRead])
async def list_professionals(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[ProfessionalRead]:
    rows = await session.scalars(
        select(Professional).where(Professional.tenant_id == tenant.id).order_by(Professional.name)
    )
    return [_read_model(p) for p in rows]


@router.post("", response_model=ProfessionalRead, status_code=status.HTTP_201_CREATED)
async def create_professional(
    body: ProfessionalCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalRead:
    if body.is_active:
        await _require_entitled_within_limit(session, tenant)

    professional = Professional(
        tenant_id=tenant.id,
        name=body.name,
        google_calendar_id=body.google_calendar_id,
        is_active=body.is_active,
    )
    session.add(professional)
    await session.commit()
    await session.refresh(professional)
    logger.info(
        "hub_professional_created", tenant_id=str(tenant.id), professional_id=str(professional.id)
    )
    return _read_model(professional)


@router.patch("/{professional_id}", response_model=ProfessionalRead)
async def update_professional(
    professional_id: str,
    body: ProfessionalUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalRead:
    professional = await _get_professional(session, tenant, professional_id)
    data = body.model_dump(exclude_unset=True)

    activating = data.get("is_active") is True and not professional.is_active
    if activating:
        await _require_entitled_within_limit(session, tenant)

    if "name" in data:
        professional.name = data["name"]
    if "google_calendar_id" in data:
        professional.google_calendar_id = data["google_calendar_id"]
    if "is_active" in data:
        professional.is_active = data["is_active"]

    await session.commit()
    await session.refresh(professional)
    logger.info(
        "hub_professional_updated", tenant_id=str(tenant.id), professional_id=str(professional.id)
    )
    return _read_model(professional)
