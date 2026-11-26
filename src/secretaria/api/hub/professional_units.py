"""Doctor hub - the units a professional attends (TASK-083, spec §5/§9).

GET /tenants/me/professionals/{professional_id}/units
    Every ACTIVE unit of the clinic with `linked` (does this professional attend it) and the
    link's own `business_hours` (null = inherits the professional's, {} = closed all week).
    Readable by any staff member of the tenant (the secretary needs it when booking), except a
    doctor who is not the manager: he reads only his own (TASK-091 T4).

PUT /tenants/me/professionals/{professional_id}/units
    Body `{"links": [{"unit_id", "business_hours"}]}`: REPLACES the professional's set of links to
    ACTIVE units (empty list = no links). Manager, or the doctor themself, only. Answers 200 with
    the same shape as the GET (state after the save).
      * links and hours of DEACTIVATED units are preserved untouched; a deactivated unit's id in
        the body is ignored, not an error;
      * a unit of another tenant (or an unknown id) is a 404 and nothing is written;
      * two saves at once: the professional's row is locked for the replace; a save that still
        collides answers 409 `professional_units_changed` and keeps nothing;
      * units are not a paid add-on any more (TASK-090): any set of links is accepted - no
        entitlement or `limits["units"]` check (no 403 `multi_unit_not_entitled`, no 409
        `unit_limit_reached`, no 503 from an entitlement fetch).

Service marks ("this service is done only at these units") travel in the same body and the same
transaction: `service_marks` is the COMPLETE restriction state (absent service = every unit);
omitted/`null` keeps the stored marks (minus those of units just unlinked). Refusals: 404 for a
unit id that is not this clinic's (like any unknown unit); 422 `{"code", "service_ids"}` with code
`service_not_offered`, `unit_not_linked` (a unit of the clinic the professional does not attend) or
`service_without_unit` (a restricted service would be left without a unit, e.g. unlinking its only
unit); nothing of the save is kept on any refusal. The GET answers `services` (the active catalog
services the professional offers) and `service_marks` (restricted ones only).

Price/duration exceptions (TASK-085, professional_service_unit_terms) follow the same save: those of
a unit just unlinked, or outside a service's marks, are erased in the same transaction.

Nothing here is ever taken from the client except the unit ids and their hours: no `agenda_id`,
no link flags (extra fields are a 422). Without links nothing changes anywhere else.
"""

from datetime import UTC, datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import (
    ensure_clinic_manager_or_own_professional,
    get_current_tenant,
    professional_reader,
)
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import Professional, ProfessionalServiceUnitTerms, Tenant, Unit
from secretaria.schemas.professional_unit import (
    ProfessionalServiceRead,
    ProfessionalUnitRead,
    ProfessionalUnitsRead,
    ProfessionalUnitsUpdate,
    ServiceUnitMark,
)
from secretaria.services import (
    hub_configuration as hubcfg,
    hub_unit_terms as unit_terms_service,
    professional_service_units as psu,
    professional_units as pu,
    tenant_config as cfg,
)
from secretaria.services.payments import price_change
from secretaria.services.service_catalog import entry_service_id, load_service_catalog

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me/professionals", tags=["hub-professional-units"])


async def _get_professional(
    session: AsyncSession, tenant: Tenant, professional_id: str
) -> Professional:
    try:
        return await hubcfg.resolve_professional(session, tenant, professional_id)
    except hubcfg.ProfessionalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found") from None


async def _read(
    session: AsyncSession, tenant: Tenant, professional: Professional
) -> ProfessionalUnitsRead:
    links = await pu.links_by_unit(session, professional.id)
    rows: list[ProfessionalUnitRead] = []
    for unit in await pu.active_units(session, tenant.id):
        link = links.get(unit.id)
        linked = link is not None and link.is_active
        rows.append(
            ProfessionalUnitRead(
                unit_id=str(unit.id),
                name=unit.name,
                address=unit.address,
                is_active=unit.is_active,
                linked=linked,
                business_hours=link.business_hours if linked else None,
            )
        )
    marks = await psu.read_marks(session, professional.id)
    services = await _offered_services(session, tenant, professional)
    # Only the services the doctor still offers (final review): a stale mark is not shown, and
    # the next save - which sends the complete desired state - removes its row.
    offered = {service.service_id for service in services}
    return ProfessionalUnitsRead(
        units=rows,
        services=services,
        service_marks=[
            ServiceUnitMark(service_id=str(service_id), unit_ids=sorted(str(u) for u in unit_ids))
            for service_id, unit_ids in sorted(marks.items(), key=lambda item: str(item[0]))
            if str(service_id) in offered
        ],
    )


async def _offered_services(
    session: AsyncSession, tenant: Tenant, professional: Professional
) -> list[ProfessionalServiceRead]:
    """The professional's active catalog services (entries with a `service_id`), clinic's names."""
    catalog = await load_service_catalog(session, tenant.id)
    names = {service.id: service.name for service in catalog if service.is_active}
    seen: set[UUID] = set()
    out: list[ProfessionalServiceRead] = []
    for entry in cfg.professional_appointment_types(professional, tenant, catalog):
        service_id = entry_service_id(entry)
        if service_id in names and service_id not in seen:
            seen.add(service_id)
            out.append(ProfessionalServiceRead(service_id=str(service_id), name=names[service_id]))
    return out


def _requested_marks(body: ProfessionalUnitsUpdate) -> dict[UUID, set[UUID]] | None:
    """The body's marks as ids; a malformed unit id is "unknown unit" (404), not a 422."""
    if body.service_marks is None:
        return None
    try:
        return {
            UUID(mark.service_id): {UUID(unit_id) for unit_id in mark.unit_ids}
            for mark in body.service_marks
        }
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found") from None


async def _require_marked_units_in_clinic(
    session: AsyncSession, tenant: Tenant, marks: dict[UUID, set[UUID]]
) -> None:
    """404 when a marked unit is not this clinic's (or does not exist): same answer for both."""
    wanted = {unit_id for units in marks.values() for unit_id in units}
    if not wanted:
        return
    found = set(
        await session.scalars(
            select(Unit.id).where(Unit.id.in_(wanted), Unit.tenant_id == tenant.id)
        )
    )
    if found != wanted:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found")


@router.get(
    "/{professional_id}/units",
    response_model=ProfessionalUnitsRead,
    dependencies=[Depends(professional_reader)],
)
async def get_professional_units(
    professional_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalUnitsRead:
    professional = await _get_professional(session, tenant, professional_id)
    return await _read(session, tenant, professional)


async def _unit_terms_count(session: AsyncSession, tenant_id: UUID, professional_id: UUID) -> int:
    """How many price/duration exceptions the professional has (TASK-085) - read before and after
    the prune so the price sweep runs only when one was actually erased."""
    return int(
        await session.scalar(
            select(func.count())
            .select_from(ProfessionalServiceUnitTerms)
            .where(
                ProfessionalServiceUnitTerms.tenant_id == tenant_id,
                ProfessionalServiceUnitTerms.professional_id == professional_id,
            )
        )
        or 0
    )


@router.put("/{professional_id}/units", response_model=ProfessionalUnitsRead)
async def put_professional_units(
    professional_id: str,
    body: ProfessionalUnitsUpdate,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalUnitsRead:
    professional = await _get_professional(session, tenant, professional_id)
    claim: SubscriptionClaim | None = getattr(request.state, "staff_subscription_claim", None)
    if claim is None:  # fail closed: never an AttributeError -> 500
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive subscription token")
    ensure_clinic_manager_or_own_professional(claim, professional.id)

    wanted = [
        pu.LinkRequest(unit_id=link.unit_id, business_hours=link.hours_as_json())
        for link in body.links
    ]
    try:
        targets = await pu.resolve_active_target_units(session, tenant.id, wanted)
    except pu.UnitNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unit not found") from None

    marks = _requested_marks(body)
    if marks is not None:
        await _require_marked_units_in_clinic(session, tenant, marks)

    ids = {"tenant_id": str(tenant.id), "professional_id": str(professional.id)}
    try:
        await pu.replace_links(session, professional, targets)
        # ALL the professional's links now (deactivated units' preserved ones included): the
        # "last unit of a restricted service" refusal looks at links only.
        linked_ids = set(await pu.links_by_unit(session, professional.id))
        await psu.apply_marks(session, tenant.id, professional, linked_ids, marks)
        # TASK-085: a price/duration exception of a unit the doctor no longer attends (or of a
        # service no longer done there) goes with it, in the same transaction.
        terms_before = await _unit_terms_count(session, tenant.id, professional.id)
        await unit_terms_service.prune_after_units_save(session, tenant.id, professional)
        pruned = await _unit_terms_count(session, tenant.id, professional.id) < terms_before
        await session.commit()
    except psu.ServiceMarkError as error:
        await session.rollback()
        logger.info("hub_professional_units_marks_refused", code=error.code, **ids)
        raise HTTPException(
            422,
            detail={"code": error.code, "service_ids": [str(s) for s in error.service_ids]},
        ) from None
    except IntegrityError:
        # Two saves at once (final wave T6): the row lock serializes them on Postgres; anything
        # that still collides is the other save winning - nothing of this one is kept. (The ids
        # were read before: a rollback expires the ORM rows.)
        await session.rollback()
        logger.info("hub_professional_units_conflict", **ids)
        raise HTTPException(status.HTTP_409_CONFLICT, "professional_units_changed") from None
    logger.info(
        "hub_professional_units_replaced",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        linked=len(targets),
    )
    # TASK-085 R (D-R14): an erased price exception puts the base price back on that unit - after
    # the commit, schedule the price sweep of a clinic with sinal. Never raises.
    if pruned and tenant.pix_deposit_enabled:
        await price_change.enqueue_sweep(
            getattr(request.app.state, "arq_pool", None), tenant.id, changed_at=datetime.now(UTC)
        )
    return await _read(session, tenant, professional)
