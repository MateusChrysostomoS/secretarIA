"""Doctor hub — units CRUD (multi_unit addon).

GET   /tenants/me/units      - list (always allowed, even when the addon is
                                disabled — a tenant that lost the addon can
                                still see what it had).
POST  /tenants/me/units      - create.
PATCH /tenants/me/units/{id} - update (name, address, is_active).

Entitlement + limit enforcement — mirrors api/hub/professionals.py exactly,
against the `multi_unit` addon and `limits["units"]`:
  - Addon disabled -> creating, or activating (is_active: false -> true), a
    unit is rejected with 403 {"detail": "multi_unit_not_entitled"}.
  - Creating/activating an ACTIVE unit beyond `limits["units"]` is rejected
    with 409 {"detail": "unit_limit_reached"}.
  - A failed entitlement fetch (None) fails CLOSED: 503, never silently allowed.
  - Renaming, changing the address, or DEACTIVATING never touch
    entitlements/limits at all.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.unit import Unit
from secretaria.schemas.unit import UnitCreate, UnitRead, UnitUpdate
from secretaria.services.entitlements_client import get_entitlements, is_entitled

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/units", tags=["hub-units"])

ADDON_KEY = "multi_unit"
LIMIT_KEY = "units"


def _read_model(unit: Unit) -> UnitRead:
    return UnitRead(
        id=str(unit.id),
        name=unit.name,
        address=unit.address,
        is_active=unit.is_active,
        created_at=unit.created_at,
    )


async def _get_unit(session: AsyncSession, tenant: Tenant, unit_id: str) -> Unit:
    try:
        unit_uuid = UUID(unit_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found") from None
    unit = await session.scalar(
        select(Unit).where(Unit.id == unit_uuid, Unit.tenant_id == tenant.id)
    )
    if unit is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")
    return unit


async def _count_active(session: AsyncSession, tenant_id: UUID) -> int:
    result = await session.scalar(
        select(func.count())
        .select_from(Unit)
        .where(Unit.tenant_id == tenant_id, Unit.is_active.is_(True))
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
        raise HTTPException(status.HTTP_403_FORBIDDEN, "multi_unit_not_entitled")

    limit = summary.limits.get(LIMIT_KEY)
    if limit is not None:
        active_count = await _count_active(session, tenant.id)
        if active_count >= limit:
            raise HTTPException(status.HTTP_409_CONFLICT, "unit_limit_reached")


@router.get("", response_model=list[UnitRead])
async def list_units(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[UnitRead]:
    rows = await session.scalars(
        select(Unit).where(Unit.tenant_id == tenant.id).order_by(Unit.name)
    )
    return [_read_model(u) for u in rows]


@router.post("", response_model=UnitRead, status_code=status.HTTP_201_CREATED)
async def create_unit(
    body: UnitCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> UnitRead:
    if body.is_active:
        await _require_entitled_within_limit(session, tenant)

    unit = Unit(
        tenant_id=tenant.id,
        name=body.name,
        address=body.address,
        is_active=body.is_active,
    )
    session.add(unit)
    await session.commit()
    await session.refresh(unit)
    logger.info("hub_unit_created", tenant_id=str(tenant.id), unit_id=str(unit.id))
    return _read_model(unit)


@router.patch("/{unit_id}", response_model=UnitRead)
async def update_unit(
    unit_id: str,
    body: UnitUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> UnitRead:
    unit = await _get_unit(session, tenant, unit_id)
    data = body.model_dump(exclude_unset=True)

    activating = data.get("is_active") is True and not unit.is_active
    if activating:
        await _require_entitled_within_limit(session, tenant)

    if "name" in data:
        unit.name = data["name"]
    if "address" in data:
        unit.address = data["address"]
    if "is_active" in data:
        unit.is_active = data["is_active"]

    await session.commit()
    await session.refresh(unit)
    logger.info("hub_unit_updated", tenant_id=str(tenant.id), unit_id=str(unit.id))
    return _read_model(unit)
