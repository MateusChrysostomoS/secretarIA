"""Doctor hub — convênio mode, catalog, the clinic's plans, each doctor's plans.

GET/PUT /tenants/me/insurance-mode                          - the topology gate.
GET     /tenants/me/insurance-catalog                        - global catalog.
GET     /tenants/me/insurance-plans                          - the clinic's plans.
PUT     /tenants/me/insurance-plans                          - replace the catalog-backed subset.
POST    /tenants/me/insurance-plans/custom                   - the clinic's "Outro".
PATCH   /tenants/me/insurance-plans/{plan_id}                - toggle charge_deposit.
GET     /tenants/me/professionals/{id}/insurance-plans       - doctor's picker.
PUT     /tenants/me/professionals/{id}/insurance-plans       - replace doctor's (shape by mode).
POST    /tenants/me/professionals/{id}/insurance-plans/custom - doctor's own "Outro"
                                                                 (independent only).

Product decisions (models/insurance.py, TASK-006 + TASK-008): the catalog is
GLOBAL; `Tenant.insurance_mode` (nullable, no default) picks one of three
acceptance topologies and gates every per-doctor section below until chosen;
`charge_deposit` is per line (clinic or doctor), never global.

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
from secretaria.models.insurance import INSURANCE_MODES
from secretaria.models.professional import Professional
from secretaria.models.tenant import Tenant
from secretaria.schemas.insurance import (
    InsuranceCatalogRead,
    InsuranceModeRead,
    InsuranceModeUpdate,
    ProfessionalInsuranceCustomCreate,
    ProfessionalInsurancePlansRead,
    ProfessionalInsurancePlansUpdate,
    TenantInsurancePlanCustomCreate,
    TenantInsurancePlanDepositUpdate,
    TenantInsurancePlanRead,
    TenantInsurancePlansUpdate,
)
from secretaria.services import hub_configuration as hubcfg
from secretaria.services.insurance_catalog import (
    NotClinicPlans,
    PlanNotFound,
    UnknownCatalogIds,
    create_professional_custom_plan,
    create_tenant_custom_plan,
    list_catalog,
    list_professional_plans,
    list_tenant_plans,
    professional_accepted_tenant_plan_ids,
    set_professional_clinic_plans,
    set_professional_independent_plans,
    set_tenant_plans,
    update_tenant_plan_charge_deposit,
)

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me", tags=["hub-insurance"])


def _bad_ids(code: str, message: str, ids: list[str]) -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={"code": code, "message": message, "catalog_ids": ids},
    )


def _mode_not_applicable(message: str) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail={"code": "insurance_mode_not_applicable", "message": message},
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
            id=str(plan.id),
            catalog_id=str(entry.id) if entry is not None else None,
            name=entry.name if entry is not None else str(plan.custom_name or ""),
            is_custom=entry is None,
            custom_payment_note=plan.custom_payment_note if entry is None else None,
            charge_deposit=plan.charge_deposit,
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


# ---------------------------------------------------------------------------
# Mode
# ---------------------------------------------------------------------------


@router.get("/insurance-mode", response_model=InsuranceModeRead)
async def get_insurance_mode(tenant: Tenant = Depends(get_current_tenant)) -> InsuranceModeRead:
    return InsuranceModeRead(mode=tenant.insurance_mode)


@router.put("/insurance-mode", response_model=InsuranceModeRead)
async def put_insurance_mode(
    body: InsuranceModeUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceModeRead:
    """Choose the acceptance topology. No going back to NULL - only across modes.

    Switching modes does not delete data from the mode being left (existing
    clinic plans, doctor customizations) - it only stops being READ, per each
    mode's own resolution rule. See docs/CHECKPOINT_convenio_catalogo.md for
    the cleanup this leaves as a documented, deliberate non-goal.
    """
    if body.mode not in INSURANCE_MODES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_insurance_mode",
                "message": f"insurance_mode deve ser um de: {', '.join(INSURANCE_MODES)}.",
            },
        )
    tenant.insurance_mode = body.mode
    await session.commit()
    logger.info("hub_insurance_mode_updated", tenant_id=str(tenant.id), mode=body.mode)
    return InsuranceModeRead(mode=tenant.insurance_mode)


# ---------------------------------------------------------------------------
# Global catalog
# ---------------------------------------------------------------------------


@router.get("/insurance-catalog", response_model=list[InsuranceCatalogRead])
async def get_insurance_catalog(
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


# ---------------------------------------------------------------------------
# The clinic's plans
# ---------------------------------------------------------------------------


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
    """Replace the clinic's CATALOG-BACKED plan set. Never touches custom rows.

    Removing a plan removes it from every doctor.
    """
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


@router.post(
    "/insurance-plans/custom",
    response_model=TenantInsurancePlanRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_tenant_insurance_plan_custom(
    body: TenantInsurancePlanCustomCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """The clinic's own "Outro" convênio. Not offered in `independent` mode -
    that mode has no clinic-level list at all (SPEC §5.1)."""
    if tenant.insurance_mode not in ("shared", "clinic_with_exceptions"):
        raise _mode_not_applicable(
            "Só é possível cadastrar um convênio 'Outro' da clínica nos modos "
            "compartilhado ou clínica-com-exceção. Escolha um modo primeiro."
        )
    plan = await create_tenant_custom_plan(
        session,
        tenant,
        custom_name=body.custom_name,
        custom_payment_note=body.custom_payment_note,
        charge_deposit=body.charge_deposit,
    )
    await session.commit()
    return TenantInsurancePlanRead(
        id=str(plan.id),
        catalog_id=None,
        name=plan.custom_name or "",
        is_custom=True,
        custom_payment_note=plan.custom_payment_note,
        charge_deposit=plan.charge_deposit,
    )


@router.patch("/insurance-plans/{plan_id}", response_model=TenantInsurancePlanRead)
async def patch_tenant_insurance_plan(
    plan_id: str,
    body: TenantInsurancePlanDepositUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """Toggle `charge_deposit` for ONE plan (catalog-backed or custom)."""
    try:
        plan_uuid = UUID(plan_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found") from None
    try:
        await update_tenant_plan_charge_deposit(session, tenant, plan_uuid, body.charge_deposit)
    except PlanNotFound:
        await session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found") from None
    await session.commit()
    plans = await _tenant_plans(session, tenant)
    updated = next((p for p in plans if p.id == plan_id), None)
    if updated is None:  # pragma: no cover - defensive, cannot happen post-commit
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    return updated


# ---------------------------------------------------------------------------
# The doctor's plans
# ---------------------------------------------------------------------------


async def _independent_selectable(
    session: AsyncSession, professional: Professional
) -> list[TenantInsurancePlanRead]:
    catalog = [
        TenantInsurancePlanRead(
            id=str(entry.id),
            catalog_id=str(entry.id),
            name=entry.name,
            is_custom=False,
            charge_deposit=True,
        )
        for entry in await list_catalog(session)
    ]
    own_custom = [
        TenantInsurancePlanRead(
            id=str(plan.id),
            catalog_id=None,
            name=str(plan.custom_name or ""),
            is_custom=True,
            custom_payment_note=plan.custom_payment_note,
            charge_deposit=plan.charge_deposit,
        )
        for plan, entry in await list_professional_plans(session, professional.id)
        if entry is None
    ]
    return catalog + own_custom


async def _independent_accepted(session: AsyncSession, professional: Professional) -> list[str]:
    accepted = []
    for plan, entry in await list_professional_plans(session, professional.id):
        accepted.append(str(entry.id) if entry is not None else str(plan.id))
    return sorted(accepted)


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
    mode = tenant.insurance_mode
    if mode in (None, "shared"):
        raise _mode_not_applicable(
            "Não há seleção de convênio por profissional neste modo."
            if mode == "shared"
            else "Escolha um modo de aceitação de convênio antes de configurar profissionais."
        )
    if mode == "independent":
        return ProfessionalInsurancePlansRead(
            professional_id=str(professional.id),
            mode=mode,
            selectable=await _independent_selectable(session, professional),
            accepted_plan_ids=await _independent_accepted(session, professional),
            inherits_clinic=False,
        )
    # clinic_with_exceptions
    selectable = await _tenant_plans(session, tenant)
    if professional.insurance_plans_customized:
        accepted = await professional_accepted_tenant_plan_ids(session, professional.id)
        inherits = False
    else:
        accepted = [plan.id for plan in selectable]
        inherits = True
    return ProfessionalInsurancePlansRead(
        professional_id=str(professional.id),
        mode=mode,
        selectable=selectable,
        accepted_plan_ids=accepted,
        inherits_clinic=inherits,
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
    """Replace this doctor's plans. Body field required matches the tenant's mode."""
    professional = await _professional(session, tenant, professional_id)
    mode = tenant.insurance_mode
    if mode in (None, "shared"):
        raise _mode_not_applicable(
            "Não há seleção de convênio por profissional neste modo."
            if mode == "shared"
            else "Escolha um modo de aceitação de convênio antes de configurar profissionais."
        )
    if mode == "clinic_with_exceptions":
        if body.plan_ids is None or body.catalog_ids is not None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "wrong_field_for_mode",
                    "message": "Envie `plan_ids` (não `catalog_ids`) neste modo.",
                },
            )
        try:
            await set_professional_clinic_plans(session, professional, body.plan_ids)
        except NotClinicPlans as exc:
            await session.rollback()
            raise _bad_ids(
                "plans_not_enabled_by_clinic",
                "O profissional só pode aceitar convênios que a clínica aceita.",
                exc.catalog_ids,
            ) from None
        await session.commit()
        selectable = await _tenant_plans(session, tenant)
        accepted = await professional_accepted_tenant_plan_ids(session, professional.id)
        logger.info(
            "hub_professional_insurance_plans_updated",
            tenant_id=str(tenant.id),
            professional_id=str(professional.id),
            plan_count=len(accepted),
        )
        return ProfessionalInsurancePlansRead(
            professional_id=str(professional.id),
            mode=mode,
            selectable=selectable,
            accepted_plan_ids=accepted,
            inherits_clinic=False,
        )

    # independent
    if body.catalog_ids is None or body.plan_ids is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "wrong_field_for_mode",
                "message": "Envie `catalog_ids` (não `plan_ids`) neste modo.",
            },
        )
    try:
        await set_professional_independent_plans(session, professional, body.catalog_ids)
    except UnknownCatalogIds as exc:
        await session.rollback()
        raise _bad_ids(
            "unknown_catalog_ids",
            "Um ou mais convênios não existem no catálogo. Recarregue a página.",
            exc.catalog_ids,
        ) from None
    await session.commit()
    logger.info(
        "hub_professional_insurance_plans_updated",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        mode=mode,
    )
    return ProfessionalInsurancePlansRead(
        professional_id=str(professional.id),
        mode=mode,
        selectable=await _independent_selectable(session, professional),
        accepted_plan_ids=await _independent_accepted(session, professional),
        inherits_clinic=False,
    )


@router.post(
    "/professionals/{professional_id}/insurance-plans/custom",
    response_model=TenantInsurancePlanRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_professional_insurance_plan_custom(
    professional_id: str,
    body: ProfessionalInsuranceCustomCreate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """A doctor's own "Outro" convênio - `independent` mode only."""
    professional = await _professional(session, tenant, professional_id)
    if tenant.insurance_mode != "independent":
        raise _mode_not_applicable(
            "Convênio 'Outro' por profissional só existe no modo independente."
        )
    plan = await create_professional_custom_plan(
        session,
        professional,
        custom_name=body.custom_name,
        custom_payment_note=body.custom_payment_note,
        charge_deposit=body.charge_deposit,
    )
    await session.commit()
    return TenantInsurancePlanRead(
        id=str(plan.id),
        catalog_id=None,
        name=plan.custom_name or "",
        is_custom=True,
        custom_payment_note=plan.custom_payment_note,
        charge_deposit=plan.charge_deposit,
    )
