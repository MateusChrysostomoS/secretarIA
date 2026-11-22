"""Doctor hub — units CRUD.

GET   /tenants/me/units      - list.
POST  /tenants/me/units      - create.
PATCH /tenants/me/units/{id} - update (name, address, is_active).

Units are NOT a paid add-on any more (TASK-090, owner decision): any clinic can create and
activate units, with no limit on how many. Nothing here consults entitlements or
`limits["units"]` (no 403 `multi_unit_not_entitled`, no 409 `unit_limit_reached`, no 503 from
an entitlement fetch).
  - A unit always has an address: create without a (non-blank) address, or a PATCH that
    sends a blank/null address, is rejected with 422
    {"detail": {"code": "unit_address_required", "message": ...}}. A PATCH that omits
    `address` is untouched (legacy units without one stay editable).
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.unit import Unit
from secretaria.schemas.unit import UnitCreate, UnitRead, UnitUpdate
from secretaria.services.professional_units import count_professionals_by_unit

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/units", tags=["hub-units"])


def _require_address(raw: str | None) -> str:
    """A unit always has an address: strip it and refuse blank/missing (422, structured)."""
    address = (raw or "").strip()
    if not address:
        raise HTTPException(
            422,
            {"code": "unit_address_required", "message": "Informe o endereço da unidade."},
        )
    return address


def _read_model(unit: Unit, professionals_count: int = 0) -> UnitRead:
    return UnitRead(
        id=str(unit.id),
        name=unit.name,
        address=unit.address,
        is_active=unit.is_active,
        created_at=unit.created_at,
        professionals_count=professionals_count,
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


@router.get("", response_model=list[UnitRead])
async def list_units(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[UnitRead]:
    rows = await session.scalars(
        select(Unit).where(Unit.tenant_id == tenant.id).order_by(Unit.name)
    )
    units = list(rows)
    counts = await count_professionals_by_unit(session, tenant.id)
    return [_read_model(u, counts.get(u.id, 0)) for u in units]


@router.post("", response_model=UnitRead, status_code=status.HTTP_201_CREATED)
async def create_unit(
    body: UnitCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> UnitRead:
    address = _require_address(body.address)

    unit = Unit(
        tenant_id=tenant.id,
        name=body.name,
        address=address,
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
    # Sending `address` (even blank/null) must carry a real one; omitting it leaves a legacy
    # unit without address editable (name/is_active only).
    if "address" in data:
        data["address"] = _require_address(data["address"])

    if "name" in data:
        unit.name = data["name"]
    if "address" in data:
        unit.address = data["address"]
    if "is_active" in data:
        unit.is_active = data["is_active"]

    await session.commit()
    await session.refresh(unit)
    logger.info("hub_unit_updated", tenant_id=str(tenant.id), unit_id=str(unit.id))
    counts = await count_professionals_by_unit(session, tenant.id, [unit.id])
    return _read_model(unit, counts.get(unit.id, 0))
