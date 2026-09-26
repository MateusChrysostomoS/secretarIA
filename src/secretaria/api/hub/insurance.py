"""Doctor hub — the convênio catalog, the clinic's plans, each doctor's plans.

GET /tenants/me/insurance-catalog                          - global catalog.
GET /tenants/me/insurance-plans                            - the clinic's plans.
PUT /tenants/me/insurance-plans                            - replace them.
GET /tenants/me/professionals/{id}/insurance-plans         - doctor's picker.
PUT /tenants/me/professionals/{id}/insurance-plans         - replace doctor's.

Product decisions (models/insurance.py): the catalog is GLOBAL (the clinic
picks from it, never types a name); a doctor's plans are a SUBSET of the
clinic's (a plan outside it is refused with 422, never silently dropped);
`charge_deposit` is per clinic AND per plan (default true = unchanged).

Never entitlement-gated: like the service catalog and every config save
(api/hub/services.py, api/hub/config.py), this is core wiring, not an addon.
Contract for the hub frontend: docs/CHECKPOINT_convenio_catalogo.md.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Tenant
from secretaria.models.professional import Professional
from secretaria.schemas.insurance import (
    InsuranceCatalogRead,
    ProfessionalInsurancePlansRead,
    ProfessionalInsurancePlansUpdate,
    TenantInsurancePlanRead,
    TenantInsurancePlansUpdate,
)
from secretaria.services import hub_configuration as hubcfg
from secretaria.services.insurance_catalog import (
    NotClinicPlans,
    UnknownCatalogIds,
    list_catalog,
    list_tenant_plans,
    professional_plan_ids,
    set_professional_plans,
    set_tenant_plans,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me", tags=["hub-insurance"])


def _bad_ids(code: str, message: str, ids: list[str]) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": code, "message": message, "catalog_ids": ids},
    )


def _parse_ids(raw: list[str]) -> list[UUID]:
    bad = []
    parsed = []
    for value in raw:
        try:
            parsed.append(UUID(str(value)))
        except ValueError:
            bad.append(str(value)[:64])
    if bad:
        raise _bad_ids("invalid_catalog_ids", "Identificador de convênio inválido.", bad)
    return parsed


async def _tenant_plans(session: AsyncSession, tenant: Tenant) -> list[TenantInsurancePlanRead]:
    return [
        TenantInsurancePlanRead(
            catalog_id=str(entry.id), name=entry.name, charge_deposit=plan.charge_deposit
        )
        for plan, entry in await list_tenant_plans(session, tenant.id)
    ]


async def _professional(
    session: AsyncSession, tenant: Tenant, professional_id: str
) -> Professional:
    try:
        return await hubcfg.resolve_professional(session, tenant, professional_id)
    except hubcfg.ProfessionalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found") from None


@router.get("/insurance-catalog", response_model=list[InsuranceCatalogRead])
async def get_insurance_catalog(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[InsuranceCatalogRead]:
    """The global catalog, active entries only, by name. Same for every tenant."""
    return [
        InsuranceCatalogRead(
            id=str(entry.id),
            slug=entry.slug,
            name=entry.name,
            mechanism=entry.mechanism,
            note=entry.note,
        )
        for entry in await list_catalog(session)
    ]


@router.get("/insurance-plans", response_model=list[TenantInsurancePlanRead])
async def get_tenant_insurance_plans(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[TenantInsurancePlanRead]:
    return await _tenant_plans(session, tenant)


@router.put("/insurance-plans", response_model=list[TenantInsurancePlanRead])
async def put_tenant_insurance_plans(
    body: TenantInsurancePlansUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[TenantInsurancePlanRead]:
    """Replace the clinic's plan set. Removing a plan removes it from every doctor."""
    ids = _parse_ids([plan.catalog_id for plan in body.plans])
    try:
        await set_tenant_plans(
            session,
            tenant,
            [(cid, plan.charge_deposit) for cid, plan in zip(ids, body.plans, strict=True)],
        )
    except UnknownCatalogIds as exc:
        await session.rollback()
        raise _bad_ids(
            "unknown_catalog_ids",
            "Um ou mais convênios não existem no catálogo. Recarregue a página.",
            exc.catalog_ids,
        ) from None
    await session.commit()
    plans = await _tenant_plans(session, tenant)
    logger.info(
        "hub_insurance_plans_updated",
        tenant_id=str(tenant.id),
        plan_count=len(plans),
        no_deposit_count=sum(1 for plan in plans if not plan.charge_deposit),
    )
    return plans


@router.get(
    "/professionals/{professional_id}/insurance-plans",
    response_model=ProfessionalInsurancePlansRead,
)
async def get_professional_insurance_plans(
    professional_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalInsurancePlansRead:
    professional = await _professional(session, tenant, professional_id)
    return ProfessionalInsurancePlansRead(
        professional_id=str(professional.id),
        selectable=await _tenant_plans(session, tenant),
        accepted_catalog_ids=await professional_plan_ids(session, professional.id),
    )


@router.put(
    "/professionals/{professional_id}/insurance-plans",
    response_model=ProfessionalInsurancePlansRead,
)
async def put_professional_insurance_plans(
    professional_id: str,
    body: ProfessionalInsurancePlansUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> ProfessionalInsurancePlansRead:
    """Replace the plans this doctor accepts - a subset of the clinic's, or 422."""
    professional = await _professional(session, tenant, professional_id)
    ids = _parse_ids(body.catalog_ids)
    try:
        accepted = await set_professional_plans(session, professional, [str(i) for i in ids])
    except NotClinicPlans as exc:
        await session.rollback()
        raise _bad_ids(
            "plans_not_enabled_by_clinic",
            "O profissional só pode aceitar convênios que a clínica aceita.",
            exc.catalog_ids,
        ) from None
    await session.commit()
    logger.info(
        "hub_professional_insurance_plans_updated",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        plan_count=len(accepted),
    )
    return ProfessionalInsurancePlansRead(
        professional_id=str(professional.id),
        selectable=await _tenant_plans(session, tenant),
        accepted_catalog_ids=accepted,
    )
