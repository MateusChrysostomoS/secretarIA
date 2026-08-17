"""Doctor hub — the clinic's CANONICAL service catalog.

GET   /tenants/me/services        - the whole catalog (active and retired).
POST  /tenants/me/services        - create one canonical service.
PATCH /tenants/me/services/{id}   - rename / edit copy / retire.

This is the ONE place a service's name and descriptive copy are edited. A
professional never types a service name again: they pick an id from here and
say only what is genuinely theirs (price, duration, whether they offer it) —
see schemas/professional.py's `appointment_types`.

Two consequences worth stating out loud:

  - RENAMING here renames everywhere. Every professional references the id, so
    a rename needs no fan-out write and cannot half-apply.
  - CREATING a near-duplicate is refused by default. An exact duplicate (same
    normalized name) can never exist — the DB constraint forbids it. A merely
    SIMILAR name ("Limpeza" when "Limpeza Dental" exists) is refused with 409
    and the suggestions attached, and the client may repeat the request with
    `?force=true` to say "no, these really are different services". That
    deliberate action is the point; guessing on the clinic's behalf is exactly
    how "Limpeza" and "Limpeza dental" were born as separate things.

Never entitlement-gated: a catalog is core platform wiring, not an addon, and
the same "a config save is always allowed" principle the tenant-level PUT and
the professionals router follow applies here (api/hub/config.py).
"""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.service import Service
from secretaria.schemas.service import ServiceCreate, ServiceRead, ServiceUpdate
from secretaria.services.service_catalog import (
    find_near_duplicates,
    load_service_catalog,
    normalize,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/services", tags=["hub-services"])


def _read_model(service: Service) -> ServiceRead:
    return ServiceRead(
        id=str(service.id),
        name=service.name,
        description=service.description,
        long_description=service.long_description,
        requirements=list(service.requirements or []),
        is_active=service.is_active,
        sort_order=service.sort_order,
        created_at=service.created_at,
    )


async def _resolve(session: AsyncSession, tenant: Tenant, service_id: str) -> Service:
    """Fetch one catalog row, scoped to this tenant.

    The tenant comes from the hub token, never the body, and ownership is
    checked on `tenant_id` — so a caller cannot reach another clinic's service
    by guessing an id. A malformed id is indistinguishable from an unknown
    one, on purpose (same rule as hub_configuration.resolve_professional).
    """
    try:
        parsed = UUID(service_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="service_not_found") from None
    service = await session.get(Service, parsed)
    if service is None or service.tenant_id != tenant.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="service_not_found")
    return service


def _duplicate_error(existing: Service) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "service_already_exists",
            "message": f"A clínica já tem o serviço '{existing.name}'.",
            "service": _read_model(existing).model_dump(mode="json"),
        },
    )


def _similar_error(name: str, similar: list[str]) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={
            "code": "similar_service_exists",
            "message": (
                f"'{name}' é muito parecido com um serviço que já existe: "
                f"{', '.join(similar)}. Use o serviço existente, ou confirme "
                "que este é mesmo um serviço diferente."
            ),
            "similar": similar,
        },
    )


@router.get("", response_model=list[ServiceRead])
async def list_services(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[ServiceRead]:
    """The clinic's whole catalog, retired services included.

    Retired rows are returned so the hub can show (and un-retire) them; the
    booking surfaces filter them out on their own
    (services/service_catalog.py::resolve_entries).
    """
    return [_read_model(row) for row in await load_service_catalog(session, tenant.id)]


@router.post("", response_model=ServiceRead, status_code=status.HTTP_201_CREATED)
async def create_service(
    payload: ServiceCreate,
    force: bool = Query(
        False,
        description=(
            "Create even when a SIMILAR service already exists. Never bypasses "
            "an exact duplicate, which the database itself forbids."
        ),
    ),
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ServiceRead:
    normalized = normalize(payload.name)
    if not normalized:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail="service_name_required")

    catalog = await load_service_catalog(session, tenant.id)
    for existing in catalog:
        if existing.normalized_name == normalized:
            # Same service, different spelling — the case the whole feature
            # exists to prevent. Never creatable, `force` or not.
            raise _duplicate_error(existing)

    if not force:
        similar = find_near_duplicates(payload.name, catalog)
        if similar:
            raise _similar_error(payload.name, similar)

    service = Service(
        id=uuid4(),
        tenant_id=tenant.id,
        name=payload.name.strip(),
        normalized_name=normalized,
        description=payload.description,
        long_description=payload.long_description,
        requirements=payload.requirements,
        is_active=payload.is_active,
        sort_order=payload.sort_order,
    )
    session.add(service)
    await session.commit()
    await session.refresh(service)
    logger.info(
        "service_catalog_created",
        tenant_id=str(tenant.id),
        service_id=str(service.id),
        forced=force,
    )
    return _read_model(service)


@router.patch("/{service_id}", response_model=ServiceRead)
async def update_service(
    service_id: str,
    payload: ServiceUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ServiceRead:
    """Partial update. A new `name` re-derives the identity key.

    No fan-out write: every professional entry points at this row's id, so the
    new name is what they all resolve to from the next read onwards.
    """
    service = await _resolve(session, tenant, service_id)
    data = payload.model_dump(exclude_unset=True)

    if data.get("name") is not None:
        normalized = normalize(data["name"])
        if not normalized:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, detail="service_name_required"
            )
        if normalized != service.normalized_name:
            clash = await session.scalar(
                select(Service).where(
                    Service.tenant_id == tenant.id,
                    Service.normalized_name == normalized,
                    Service.id != service.id,
                )
            )
            if clash is not None:
                raise _duplicate_error(clash)
        service.name = data["name"].strip()
        service.normalized_name = normalized

    for field in ("description", "long_description", "requirements", "is_active", "sort_order"):
        if field in data and data[field] is not None:
            setattr(service, field, data[field])

    await session.commit()
    await session.refresh(service)
    logger.info(
        "service_catalog_updated",
        tenant_id=str(tenant.id),
        service_id=str(service.id),
        renamed="name" in data,
    )
    return _read_model(service)
