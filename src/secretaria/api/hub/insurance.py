"""Doctor hub — convênio mode, catalog, the clinic's plans, each doctor's plans.

GET/PUT /tenants/me/insurance-mode                          - the topology gate.
GET     /tenants/me/insurance-catalog                        - global catalog.
GET     /tenants/me/insurance-plans                          - the clinic's plans.
PUT     /tenants/me/insurance-plans                          - replace the catalog-backed subset.
POST    /tenants/me/insurance-plans/custom                   - the clinic's "Outro".
PUT     /tenants/me/insurance-plans/{plan_id}/payment-note   - a clinic line's payment rule.
PUT     /tenants/me/insurance-plans/{plan_id}/active         - the clinic's "Outro" ON/OFF.
GET     /tenants/me/professionals/{id}/insurance-plans       - doctor's picker.
PUT     /tenants/me/professionals/{id}/insurance-plans       - replace doctor's (shape by mode).
POST    /tenants/me/professionals/{id}/insurance-plans/custom - doctor's own "Outro"
                                                                 (independent only).
PUT     /tenants/me/professionals/{id}/insurance-plans/{plan_id}/payment-note
                                                               - a doctor line's payment rule
                                                                 (independent only; manager
                                                                 or that doctor himself, C8).
PUT     /tenants/me/professionals/{id}/insurance-plans/{plan_id}/active
                                                               - the doctor's "Outro" ON/OFF
                                                                 (independent only).

"Outro" ON/OFF (TASK-090 Adendo 1, contract C7, owner 2026-11-24): a typed-by-hand convênio can
be switched OFF without losing it (row, name and payment rule stay); it then reaches no patient
and no AI tool, and every read here returns it with `is_active=false`. Same role gate as creating
an "Outro" (any staff role); switching ON is held to the convênio cap (only ACTIVE lines count),
switching OFF never is. A catalog line answers 422 `not_custom_plan` (OFF deletes a catalog line).

Payment rule (TASK-090 Adendo 1, contract C6): every line (catalog or "Outro", clinic or doctor)
may carry an optional `custom_payment_note`, returned on every read. Writing it - the two PUT
.../payment-note routes, or a note sent with a custom POST - is for a clinic MANAGER only
(`require_clinic_manager` / `ensure_clinic_manager`), except a DOCTOR's line (contract C8, owner
2026-11-24): the manager OR that doctor himself - the `doctor` user whose linked professional is
the path's (`require_clinic_manager_or_own_professional` /
`ensure_clinic_manager_or_own_professional`); secretaries and other doctors stay 403. Goes through
services/insurance_payment_note.clean_payment_note (one place) and answers 422
`payment_note_too_long` above 1000 characters of the cleaned text (never truncated). The note is
never logged (only whether a rule is set). Only the AI reads it (`get_insurance_payment_rule`).

Product decisions (models/insurance.py, TASK-006 + TASK-008): the catalog is
GLOBAL; `Tenant.insurance_mode` (nullable, no default) picks one of three
acceptance topologies and gates every per-doctor section below until chosen.
TASK-085 R D-R12/U1b (owner, 2026-11-18): a convênio never exempts the sinal - the
old per-line "não cobra sinal" option is DELETED: no read carries it, the toggle
route is gone, and an older client that still SENDS the flag is not refused (the
input schemas ignore it). The two legacy columns stay in the database, unmapped,
until a later release drops them.

Never entitlement-gated: like the service catalog and every config save
(api/hub/services.py, api/hub/config.py), this is core wiring, not an addon.
Contract for the hub frontend: docs/CHECKPOINT_convenio_catalogo.md.
"""

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import (
    ensure_clinic_manager,
    ensure_clinic_manager_or_own_professional,
    get_current_tenant,
    path_professional_uuid,
    require_clinic_manager,
    require_clinic_manager_or_own_professional,
)
from secretaria.core.catalog_limits import (
    INSURANCE_PLANS_PER_CLINIC,
    CatalogLimitExceeded,
    check_growth,
)
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models.insurance import (
    INSURANCE_MODES_SELECTABLE,
    InsuranceCatalog,
    ProfessionalInsurancePlan,
    TenantInsurancePlan,
)
from secretaria.models.professional import Professional
from secretaria.models.tenant import Tenant
from secretaria.schemas.insurance import (
    InsuranceCatalogRead,
    InsuranceModeRead,
    InsuranceModeUpdate,
    PaymentNoteUpdate,
    PlanActiveUpdate,
    ProfessionalInsuranceCustomCreate,
    ProfessionalInsurancePlansRead,
    ProfessionalInsurancePlansUpdate,
    TenantInsurancePlanCustomCreate,
    TenantInsurancePlanOrderUpdate,
    TenantInsurancePlanRead,
    TenantInsurancePlansUpdate,
)
from secretaria.services import hub_configuration as hubcfg
from secretaria.services.insurance_catalog import (
    NotClinicPlans,
    NotCustomPlan,
    PlanNotFound,
    UnknownCatalogIds,
    create_professional_custom_plan,
    create_tenant_custom_plan,
    list_catalog,
    list_professional_plans,
    list_tenant_plans,
    load_tenant_insurance,
    professional_accepted_tenant_plan_ids,
    set_custom_plan_active,
    set_professional_clinic_plans,
    set_professional_independent_plans,
    set_tenant_plan_order,
    set_tenant_plans,
)
from secretaria.services.insurance_payment_note import (
    MAX_PAYMENT_NOTE_CHARS,
    PaymentNoteTooLong,
    clean_payment_note,
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


def _tenant_plan_read(
    plan: TenantInsurancePlan, entry: InsuranceCatalog | None
) -> TenantInsurancePlanRead:
    return TenantInsurancePlanRead(
        id=str(plan.id),
        catalog_id=str(entry.id) if entry is not None else None,
        name=entry.name if entry is not None else str(plan.custom_name or ""),
        is_custom=entry is None,
        custom_payment_note=plan.custom_payment_note,
        sort_order=plan.sort_order,
        is_active=plan.is_active is not False,
    )


async def _tenant_plans(session: AsyncSession, tenant: Tenant) -> list[TenantInsurancePlanRead]:
    return [
        _tenant_plan_read(plan, entry)
        for plan, entry in await list_tenant_plans(session, tenant.id)
    ]


def _payment_note(raw: str | None) -> str | None:
    """The cleaned rule (None = no rule), or 422 `payment_note_too_long` - never truncated."""
    try:
        return clean_payment_note(raw)
    except PaymentNoteTooLong as exc:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "payment_note_too_long",
                "message": (
                    f"A regra de pagamento pode ter no máximo {MAX_PAYMENT_NOTE_CHARS} "
                    f"caracteres (tem {exc.length})."
                ),
                "max_chars": MAX_PAYMENT_NOTE_CHARS,
                "length": exc.length,
            },
        ) from None


def _ensure_manager_writes_note(request: Request, note: str | None) -> None:
    """A custom POST that carries a rule: only a clinic manager writes a payment rule (C6).
    Without a rule the POST keeps its old, role-blind gate."""
    if note is None:
        return
    claim: SubscriptionClaim | None = getattr(request.state, "staff_subscription_claim", None)
    if claim is None:  # fail closed: never an AttributeError -> 500
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive subscription token")
    ensure_clinic_manager(claim)


def _ensure_rule_writer_for_professional(
    request: Request, note: str | None, professional_id: str
) -> None:
    """A professional custom POST that carries a rule (C8): the manager or that doctor himself.
    Decided from the path id before the 404 lookup; without a rule the POST keeps its gate."""
    if note is None:
        return
    claim: SubscriptionClaim | None = getattr(request.state, "staff_subscription_claim", None)
    if claim is None:  # fail closed: never an AttributeError -> 500
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or inactive subscription token")
    ensure_clinic_manager_or_own_professional(claim, path_professional_uuid(professional_id))


def _not_custom_plan() -> HTTPException:
    return HTTPException(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail={
            "code": "not_custom_plan",
            "message": (
                "Só um convênio digitado pela clínica ('Outro') pode ser ligado ou desligado "
                "sem ser removido."
            ),
        },
    )


async def _switch_custom_plan(
    session: AsyncSession,
    tenant_id: UUID,
    plan: TenantInsurancePlan | ProfessionalInsurancePlan,
    is_active: bool,
) -> bool:
    """Apply the C7 switch (not committed): 422 `not_custom_plan` for a catalog line; switching
    ON is held to the convênio cap exactly like creating one (only ACTIVE lines count), switching
    OFF never fails on it. Returns whether the value changed (False = idempotent no-op)."""
    if plan.custom_name is None or plan.catalog_id is not None:
        raise _not_custom_plan()
    before = await _visible_plan_count(session, tenant_id) if is_active else 0
    try:
        changed = set_custom_plan_active(plan, is_active)
    except NotCustomPlan:
        raise _not_custom_plan() from None
    if changed and is_active:
        await session.flush()
        await _refuse_growth(session, tenant_id, before)
    return changed


def _parse_plan_id(raw: str) -> UUID:
    try:
        return UUID(raw)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found") from None


async def _visible_plan_count(session: AsyncSession, tenant_id: UUID) -> int:
    """How many convênios the patient's list shows - mode-aware (TASK-082 S4).

    The same read the conversation uses (`load_tenant_insurance`): the clinic's plans
    in `shared`/`clinic_with_exceptions`, the union of the active doctors' own plans
    in `independent`. Counting only `tenant_insurance_plans` would leave
    `independent` unlimited.
    """
    return len((await load_tenant_insurance(session, tenant_id)).plans)


async def _refuse_growth(session: AsyncSession, tenant_id: UUID, before: int) -> None:
    """After a convênio mutation (not committed): 422 + rollback when the patient's list
    grew past INSURANCE_PLANS_PER_CLINIC ("no growth past the limit", core/catalog_limits)."""
    after = await _visible_plan_count(session, tenant_id)
    try:
        check_growth(limit=INSURANCE_PLANS_PER_CLINIC, before=before, after=after)
    except CatalogLimitExceeded as exc:
        await session.rollback()
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=exc.detail()) from None


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
    if body.mode not in INSURANCE_MODES_SELECTABLE:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "code": "invalid_insurance_mode",
                "message": (
                    "insurance_mode deve ser um de: " + ", ".join(INSURANCE_MODES_SELECTABLE) + "."
                ),
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
    tenant_id = tenant.id
    before = await _visible_plan_count(session, tenant_id)
    try:
        await set_tenant_plans(session, tenant, ids)
    except UnknownCatalogIds as exc:
        await session.rollback()
        raise _bad_ids(
            "unknown_catalog_ids",
            "Um ou mais convênios não existem no catálogo. Recarregue a página.",
            exc.catalog_ids,
        ) from None
    await _refuse_growth(session, tenant_id, before)
    await session.commit()
    plans = await _tenant_plans(session, tenant)
    logger.info(
        "hub_insurance_plans_updated",
        tenant_id=str(tenant.id),
        plan_count=len(plans),
    )
    return plans


@router.post(
    "/insurance-plans/custom",
    response_model=TenantInsurancePlanRead,
    status_code=status.HTTP_201_CREATED,
)
async def post_tenant_insurance_plan_custom(
    body: TenantInsurancePlanCustomCreate,
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """The clinic's own "Outro" convênio. Not offered in `independent` mode -
    that mode has no clinic-level list at all (SPEC §5.1). An optional payment rule
    (`custom_payment_note`, C6) rides along - manager only."""
    note = _payment_note(body.custom_payment_note)
    _ensure_manager_writes_note(request, note)
    if tenant.insurance_mode not in ("shared", "clinic_with_exceptions"):
        raise _mode_not_applicable(
            "Só é possível cadastrar um convênio 'Outro' da clínica nos modos "
            "compartilhado ou clínica-com-exceção. Escolha um modo primeiro."
        )
    tenant_id = tenant.id
    before = await _visible_plan_count(session, tenant_id)
    plan = await create_tenant_custom_plan(session, tenant, custom_name=body.custom_name)
    plan.custom_payment_note = note
    await _refuse_growth(session, tenant_id, before)
    await session.commit()
    return TenantInsurancePlanRead(
        id=str(plan.id),
        catalog_id=None,
        name=plan.custom_name or "",
        is_custom=True,
        custom_payment_note=plan.custom_payment_note,
        is_active=plan.is_active is not False,
    )


@router.put(
    "/insurance-plans/{plan_id}/payment-note",
    response_model=TenantInsurancePlanRead,
)
async def put_tenant_insurance_plan_payment_note(
    plan_id: str,
    body: PaymentNoteUpdate,
    tenant: Tenant = Depends(require_clinic_manager),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """Set or clear (null/blank) the payment rule of one of the clinic's lines - catalog
    or "Outro" (C6). 404 for an unknown id or another clinic's line."""
    note = _payment_note(body.custom_payment_note)
    row = await session.execute(
        select(TenantInsurancePlan, InsuranceCatalog)
        .outerjoin(InsuranceCatalog, InsuranceCatalog.id == TenantInsurancePlan.catalog_id)
        .where(
            TenantInsurancePlan.id == _parse_plan_id(plan_id),
            TenantInsurancePlan.tenant_id == tenant.id,
        )
    )
    found = row.first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    plan, entry = found
    plan.custom_payment_note = note
    await session.commit()
    logger.info(
        "hub_insurance_payment_note_saved",
        tenant_id=str(tenant.id),
        plan_id=str(plan.id),
        has_rule=note is not None,
    )
    return _tenant_plan_read(plan, entry)


@router.put(
    "/insurance-plans/{plan_id}/active",
    response_model=TenantInsurancePlanRead,
)
async def put_tenant_insurance_plan_active(
    plan_id: str,
    body: PlanActiveUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """Switch the clinic's own "Outro" ON/OFF (C7). The row, its name and its payment rule stay.

    Same gate as `POST /insurance-plans/custom` (whoever may create an "Outro" may switch it).
    404 for an unknown id or another clinic's line; 422 `not_custom_plan` for a catalog line;
    switching ON past the convênio cap is the cap's 422. The same value again is a 200 no-op.
    """
    row = await session.execute(
        select(TenantInsurancePlan, InsuranceCatalog)
        .outerjoin(InsuranceCatalog, InsuranceCatalog.id == TenantInsurancePlan.catalog_id)
        .where(
            TenantInsurancePlan.id == _parse_plan_id(plan_id),
            TenantInsurancePlan.tenant_id == tenant.id,
        )
    )
    found = row.first()
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    plan, entry = found
    tenant_id = tenant.id
    changed = await _switch_custom_plan(session, tenant_id, plan, body.is_active)
    if changed:
        await session.commit()
        logger.info(
            "hub_insurance_custom_plan_switched",
            tenant_id=str(tenant_id),
            plan_id=str(plan.id),
            is_active=plan.is_active,
        )
    return _tenant_plan_read(plan, entry)


@router.put("/insurance-plans/order", response_model=list[TenantInsurancePlanRead])
async def put_tenant_insurance_plan_order(
    body: TenantInsurancePlanOrderUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> list[TenantInsurancePlanRead]:
    """Choose the order the patient sees the clinic's convênios in.

    The listed plans come first, in the given order; the rest follow alphabetically.
    404 when an id is not one of this clinic's plans (nothing is changed).
    """
    try:
        plan_ids = [UUID(plan_id) for plan_id in body.plan_ids]
        await set_tenant_plan_order(session, tenant, plan_ids)
    except (ValueError, PlanNotFound):
        await session.rollback()
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found") from None
    await session.commit()
    plans = await _tenant_plans(session, tenant)
    logger.info(
        "hub_insurance_plan_order_updated",
        tenant_id=str(tenant.id),
        placed_count=sum(1 for plan in plans if plan.sort_order is not None),
    )
    return plans


# ---------------------------------------------------------------------------
# The doctor's plans
# ---------------------------------------------------------------------------


def _professional_plan_read(
    plan: ProfessionalInsurancePlan, entry: InsuranceCatalog | None
) -> TenantInsurancePlanRead:
    """One of the doctor's OWN lines (independent mode), keyed like the picker: the catalog
    id for a catalog pick, the row id for the doctor's "Outro"."""
    if entry is not None:
        return TenantInsurancePlanRead(
            id=str(entry.id),
            catalog_id=str(entry.id),
            name=entry.name,
            is_custom=False,
            custom_payment_note=plan.custom_payment_note,
        )
    return TenantInsurancePlanRead(
        id=str(plan.id),
        catalog_id=None,
        name=str(plan.custom_name or ""),
        is_custom=True,
        custom_payment_note=plan.custom_payment_note,
        is_active=plan.is_active is not False,
    )


async def _independent_selectable(
    session: AsyncSession, professional: Professional
) -> list[TenantInsurancePlanRead]:
    own = await list_professional_plans(session, professional.id)
    notes = {entry.id: plan.custom_payment_note for plan, entry in own if entry is not None}
    catalog = [
        TenantInsurancePlanRead(
            id=str(entry.id),
            catalog_id=str(entry.id),
            name=entry.name,
            is_custom=False,
            # The doctor's rule for a catalog pick they accept; null when not accepted.
            custom_payment_note=notes.get(entry.id),
        )
        for entry in await list_catalog(session)
    ]
    own_custom = [_professional_plan_read(plan, entry) for plan, entry in own if entry is None]
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
    """Replace this doctor's plans. Body field required matches the tenant's mode.

    In `independent` mode the doctor's picks ARE the patient's list (their union), so
    the save is held to the clinic's 60 (TASK-082 S4); in `clinic_with_exceptions` a
    doctor only picks among the clinic's plans and the list cannot grow.
    """
    professional = await _professional(session, tenant, professional_id)
    tenant_id = tenant.id
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
    before = await _visible_plan_count(session, tenant_id)
    try:
        await set_professional_independent_plans(session, professional, body.catalog_ids)
    except UnknownCatalogIds as exc:
        await session.rollback()
        raise _bad_ids(
            "unknown_catalog_ids",
            "Um ou mais convênios não existem no catálogo. Recarregue a página.",
            exc.catalog_ids,
        ) from None
    await _refuse_growth(session, tenant_id, before)
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
    request: Request,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """A doctor's own "Outro" convênio - `independent` mode only. An optional payment
    rule (`custom_payment_note`, C6) rides along - the manager or that doctor himself (C8)."""
    note = _payment_note(body.custom_payment_note)
    _ensure_rule_writer_for_professional(request, note, professional_id)
    professional = await _professional(session, tenant, professional_id)
    if tenant.insurance_mode != "independent":
        raise _mode_not_applicable(
            "Convênio 'Outro' por profissional só existe no modo independente."
        )
    tenant_id = tenant.id
    before = await _visible_plan_count(session, tenant_id)
    plan = await create_professional_custom_plan(
        session, professional, custom_name=body.custom_name
    )
    plan.custom_payment_note = note
    await _refuse_growth(session, tenant_id, before)
    await session.commit()
    return TenantInsurancePlanRead(
        id=str(plan.id),
        catalog_id=None,
        name=plan.custom_name or "",
        is_custom=True,
        custom_payment_note=plan.custom_payment_note,
        is_active=plan.is_active is not False,
    )


@router.put(
    "/professionals/{professional_id}/insurance-plans/{plan_id}/payment-note",
    response_model=TenantInsurancePlanRead,
)
async def put_professional_insurance_plan_payment_note(
    professional_id: str,
    plan_id: str,
    body: PaymentNoteUpdate,
    tenant: Tenant = Depends(require_clinic_manager_or_own_professional),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """Set or clear the payment rule of one of the doctor's OWN lines (`independent` mode,
    C6). Written by the clinic manager (any doctor) or that doctor himself (C8, owner
    2026-11-24); a secretary or another doctor gets 403 before any lookup.
    `plan_id` is the id the picker shows: the catalog id of a pick the doctor accepts, or
    the row id of the doctor's "Outro". 404 for another clinic's doctor, a line the doctor does
    not have, or any other id; 409 outside `independent` (the clinic's line carries the rule)."""
    note = _payment_note(body.custom_payment_note)
    professional = await _professional(session, tenant, professional_id)
    if tenant.insurance_mode != "independent":
        raise _mode_not_applicable(
            "A regra de pagamento por profissional só existe no modo independente; "
            "nos outros modos ela fica no convênio da clínica."
        )
    wanted = _parse_plan_id(plan_id)
    found = None
    for plan, entry in await list_professional_plans(session, professional.id):
        if (entry is not None and entry.id == wanted) or (entry is None and plan.id == wanted):
            found = (plan, entry)
            break
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    plan, entry = found
    plan.custom_payment_note = note
    await session.commit()
    logger.info(
        "hub_professional_payment_note_saved",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        has_rule=note is not None,
    )
    return _professional_plan_read(plan, entry)


@router.put(
    "/professionals/{professional_id}/insurance-plans/{plan_id}/active",
    response_model=TenantInsurancePlanRead,
)
async def put_professional_insurance_plan_active(
    professional_id: str,
    plan_id: str,
    body: PlanActiveUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> TenantInsurancePlanRead:
    """Switch a doctor's own "Outro" ON/OFF (`independent` mode, C7). The row, its name and its
    payment rule stay.

    Same gate as `POST .../insurance-plans/custom`. `plan_id` is the id the picker shows (the row
    id of the doctor's "Outro"). 404 for another clinic's doctor or a line the doctor does not
    have; 422 `not_custom_plan` for a catalog pick he accepts; 409 outside `independent`;
    switching ON past the convênio cap is the cap's 422. The same value again is a 200 no-op.
    """
    professional = await _professional(session, tenant, professional_id)
    if tenant.insurance_mode != "independent":
        raise _mode_not_applicable(
            "Convênio 'Outro' por profissional só existe no modo independente."
        )
    wanted = _parse_plan_id(plan_id)
    found = None
    for plan, entry in await list_professional_plans(session, professional.id):
        if (entry is not None and entry.id == wanted) or (entry is None and plan.id == wanted):
            found = (plan, entry)
            break
    if found is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Plan not found")
    plan, entry = found
    tenant_id = tenant.id
    changed = await _switch_custom_plan(session, tenant_id, plan, body.is_active)
    if changed:
        await session.commit()
        logger.info(
            "hub_professional_custom_plan_switched",
            tenant_id=str(tenant_id),
            professional_id=str(professional.id),
            plan_id=str(plan.id),
            is_active=plan.is_active,
        )
    return _professional_plan_read(plan, entry)
