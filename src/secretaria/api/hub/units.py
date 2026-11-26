"""Doctor hub — units CRUD.

GET   /tenants/me/units      - list.
POST  /tenants/me/units      - create.
PATCH /tenants/me/units/{id} - update (name, structured address, is_active).

Units are NOT a paid add-on any more (TASK-090, owner decision): any clinic can create and
activate units, with no limit on how many. Nothing here consults entitlements or
`limits["units"]` (no 403 `multi_unit_not_entitled`, no 409 `unit_limit_reached`, no 503 from
an entitlement fetch).

The address is structured (contract C1; see services/unit_address.py):
  - Input parts: cep (mask accepted, stored as 8 digits), street, number, complement,
    neighborhood, city, state (UF). An `address` text sent by the client is ignored; the
    stored/returned `address` line is derived from the parts.
  - Create, or a PATCH that sends ANY address part, validates the RESULT (for a PATCH: the
    existing parts merged with the sent ones): cep, street, number, city and state must be
    valid, else 422 {"detail": {"code": "unit_address_invalid", "message": ..., "fields": [...]}}.
  - A PATCH of only name/is_active never validates, so a legacy unit (text address only)
    stays editable. On read, `address_incomplete` flags those units.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant, manager_only
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.unit import Unit
from secretaria.schemas.unit import UnitCreate, UnitRead, UnitUpdate
from secretaria.services.professional_units import count_professionals_by_unit
from secretaria.services.unit_address import (
    UnitAddressParts,
    address_complete,
    clean_text,
    format_unit_address,
    invalid_fields,
    normalize_cep,
    normalize_state,
    oversize_line_fields,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/units", tags=["hub-units"])

_PART_FIELDS = ("cep", "street", "number", "complement", "neighborhood", "city", "state")


def _validated_parts(raw: UnitAddressParts) -> UnitAddressParts:
    """Clean the parts (strip, CEP to 8 digits, UF upper-case) or raise 422
    `unit_address_invalid` listing every bad field: a missing/invalid required part, a part longer
    than its column, or - when the derived line would not fit `units.address` - the longest
    parts. An invalid CEP is reported, never stored."""
    cleaned = UnitAddressParts(
        cep=normalize_cep(raw.cep),
        street=clean_text(raw.street),
        number=clean_text(raw.number),
        complement=clean_text(raw.complement),
        neighborhood=clean_text(raw.neighborhood),
        city=clean_text(raw.city),
        state=normalize_state(raw.state),
    )
    bad = invalid_fields(cleaned) or oversize_line_fields(cleaned)
    if bad:
        raise HTTPException(
            422,
            {
                "code": "unit_address_invalid",
                "message": "Confira o endereço da unidade: CEP, rua, número, cidade e estado.",
                "fields": bad,
            },
        )
    return cleaned


def _apply_parts(unit: Unit, parts: UnitAddressParts) -> None:
    for name in _PART_FIELDS:
        setattr(unit, name, getattr(parts, name))
    unit.address = format_unit_address(parts)


def _read_model(unit: Unit, professionals_count: int = 0) -> UnitRead:
    return UnitRead(
        id=str(unit.id),
        name=unit.name,
        address=unit.address,
        **{name: getattr(unit, name) for name in _PART_FIELDS},
        address_incomplete=not address_complete(unit),
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


@router.post(
    "",
    response_model=UnitRead,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(manager_only)],
)
async def create_unit(
    body: UnitCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> UnitRead:
    parts = _validated_parts(UnitAddressParts(**body.model_dump(include=set(_PART_FIELDS))))

    unit = Unit(tenant_id=tenant.id, name=body.name, is_active=body.is_active)
    _apply_parts(unit, parts)
    session.add(unit)
    await session.commit()
    await session.refresh(unit)
    logger.info("hub_unit_created", tenant_id=str(tenant.id), unit_id=str(unit.id))
    return _read_model(unit)


@router.patch("/{unit_id}", response_model=UnitRead, dependencies=[Depends(manager_only)])
async def update_unit(
    unit_id: str,
    body: UnitUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> UnitRead:
    unit = await _get_unit(session, tenant, unit_id)
    data = body.model_dump(exclude_unset=True)

    # Any address part sent => validate the MERGED result (existing parts + sent parts); a
    # PATCH of only name/is_active leaves a legacy unit (text address only) editable.
    sent_parts = {k: v for k, v in data.items() if k in _PART_FIELDS}
    if sent_parts:
        merged = {**{name: getattr(unit, name) for name in _PART_FIELDS}, **sent_parts}
        _apply_parts(unit, _validated_parts(UnitAddressParts(**merged)))

    if "name" in data:
        unit.name = data["name"]
    if "is_active" in data:
        unit.is_active = data["is_active"]

    await session.commit()
    await session.refresh(unit)
    logger.info("hub_unit_updated", tenant_id=str(tenant.id), unit_id=str(unit.id))
    counts = await count_professionals_by_unit(session, tenant.id, [unit.id])
    return _read_model(unit, counts.get(unit.id, 0))
