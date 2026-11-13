"""Doctor hub - the units a professional attends (TASK-083, spec §5/§9).

GET /tenants/me/professionals/{professional_id}/units
    Every ACTIVE unit of the clinic with `linked` (does this professional attend it) and the
    link's own `business_hours` (null = inherits the professional's, {} = closed all week).
    Readable by ANY staff member of the tenant: the secretary needs it when booking.

PUT /tenants/me/professionals/{professional_id}/units
    Body `{"links": [{"unit_id", "business_hours"}]}`: REPLACES the professional's set of links to
    ACTIVE units (empty list = no links). Manager, or the doctor themself, only. Answers 200 with
    the same shape as the GET (state after the save).
      * links and hours of DEACTIVATED units are preserved untouched; a deactivated unit's id in
        the body is ignored, not an error;
      * a unit of another tenant (or an unknown id) is a 404 and nothing is written;
      * two saves at once: the professional's row is locked for the replace; a save that still
        collides answers 409 `professional_units_changed` and keeps nothing;
      * multi-unit entitlement is judged on the RESULTING set: 403 `multi_unit_not_entitled` only
        when it would hold MORE than one active unit (one link is just "the clinic's address" and
        needs nothing); 409 `unit_limit_reached` when it exceeds `limits["units"]`; an entitlement
        fetch failure fails CLOSED with 503 - same rules and codes as api/hub/units.py.

Nothing here is ever taken from the client except the unit ids and their hours: no `agenda_id`,
no link flags (extra fields are a 422). Without links nothing changes anywhere else.
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import (
    ensure_clinic_manager_or_own_professional,
    get_current_tenant,
)
from secretaria.api.hub.units import ADDON_KEY, LIMIT_KEY
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import Professional, Tenant
from secretaria.schemas.professional_unit import (
    ProfessionalUnitRead,
    ProfessionalUnitsRead,
    ProfessionalUnitsUpdate,
)
from secretaria.services import hub_configuration as hubcfg, professional_units as pu
from secretaria.services.entitlements_client import get_entitlements, is_entitled

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
    return ProfessionalUnitsRead(units=rows)


async def _require_entitled_within_limit(tenant: Tenant, linked_count: int) -> None:
    """Gate a resulting set of MORE than one linked unit. Raises HTTPException on failure.

    Fetches entitlements fresh (redis=None, not a hot path). None (fetch failure) fails CLOSED:
    503, never treated as "allowed".
    """
    summary = await get_entitlements(tenant.id, redis=None)
    if summary is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Could not verify entitlements right now"
        )
    if not is_entitled(summary, ADDON_KEY):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "multi_unit_not_entitled")
    limit = summary.limits.get(LIMIT_KEY)
    if limit is not None and linked_count > limit:
        raise HTTPException(status.HTTP_409_CONFLICT, "unit_limit_reached")


@router.get("/{professional_id}/units", response_model=ProfessionalUnitsRead)
async def get_professional_units(
    professional_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalUnitsRead:
    professional = await _get_professional(session, tenant, professional_id)
    return await _read(session, tenant, professional)


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

    if len(targets) > 1:
        await _require_entitled_within_limit(tenant, len(targets))

    ids = {"tenant_id": str(tenant.id), "professional_id": str(professional.id)}
    try:
        await pu.replace_links(session, professional, targets)
        await session.commit()
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
    return await _read(session, tenant, professional)
