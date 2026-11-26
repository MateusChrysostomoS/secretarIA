"""Doctor hub - the convênio acceptance matrix (TASK-086 T3).

Contract: tasks/TASK-086/results/inventario.md §6.2. The matrix stores only what is NOT accepted
(SPEC E1); a slice = (scope, unit) where scope is the CLINIC (mode `shared`) or one DOCTOR (mode
`independent`), and the unit is a real unit of the clinic or the "no unit" slice (`none`).

GET  /tenants/me/insurance-acceptance/summary
GET  /tenants/me/insurance-acceptance/clinic                              (mode shared)
PUT  /tenants/me/insurance-acceptance/clinic/units/{unit_key}             (mode shared)
POST /tenants/me/insurance-acceptance/clinic/copy                         (mode shared)
GET  /tenants/me/professionals/{id}/insurance-acceptance                  (mode independent)
PUT  /tenants/me/professionals/{id}/insurance-acceptance/units/{unit_key} (mode independent)
POST /tenants/me/professionals/{id}/insurance-acceptance/copy             (mode independent)
PUT  /tenants/me/professionals/{id}/insurance-acceptance/plan-order       (mode independent)

The doctor matrix's columns follow the clinic's ONE per-doctor-mode convênio order (TASK-090,
services/insurance_plan_order.py) - the same order the patient sees; `plan-order` writes it.

PUT REPLACES every exception of the slice in one transaction (lock, delete, insert, commit);
copy REPLACES each destination slice with the source slice's exceptions. Every id in a body must
belong to the caller's scope (services and plans of the clinic / the doctor, units of the clinic
the scope can book), so a foreign id is a 422/404 and nothing is written.

Role: same as the rest of the convênio hub (`get_current_tenant`: any staff of the tenant). The
inventory registered this as an open point; tightening it is a one-line `Depends` swap.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import (
    get_current_tenant,
    manager_only,
    professional_reader,
    professional_writer,
)
from secretaria.core.database import get_session
from secretaria.core.logging import get_logger
from secretaria.models import Professional, Service, Tenant, Unit
from secretaria.models.insurance_acceptance_exception import InsuranceAcceptanceException
from secretaria.schemas.insurance_acceptance import (
    InsuranceMatrixCopy,
    InsuranceMatrixRead,
    InsuranceMatrixSliceUpdate,
    InsuranceMatrixSummary,
    MatrixCell,
    MatrixCount,
    MatrixPlan,
    MatrixService,
    MatrixSlice,
    MatrixUnitCount,
    PlanOrderUpdate,
    ProfessionalMatrixCount,
)
from secretaria.services import (
    hub_configuration as hubcfg,
    insurance_plan_order as plan_order,
    professional_service_units as psu,
    professional_units as pu,
    tenant_config as cfg,
)
from secretaria.services.insurance_catalog import list_professional_plans, list_tenant_plans
from secretaria.services.service_catalog import entry_service_id, load_service_catalog
from secretaria.services.unit_booking import unit_options_for

logger = get_logger(__name__)
router = APIRouter(prefix="/tenants/me", tags=["hub-insurance-acceptance"])

NONE_KEY = "none"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


def _error(code: int, key: str, message: str, **extra) -> HTTPException:
    return HTTPException(code, detail={"code": key, "message": message, **extra})


def _not_in_mode(mode: str | None, needed: str) -> HTTPException:
    where = "a mesma lista para todos" if needed == "shared" else "lista por profissional"
    return _error(
        status.HTTP_409_CONFLICT,
        "insurance_matrix_not_in_mode",
        f"Esta matriz só existe quando os convênios estão no modo '{where}'.",
        mode=mode,
    )


def _unit_not_found() -> HTTPException:
    return HTTPException(status.HTTP_404_NOT_FOUND, "unit_not_found")


def _unavailable(unit_id: UUID | None) -> HTTPException:
    return _error(
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "unit_slice_not_available",
        "Esta unidade não faz parte da matriz deste profissional ou da clínica.",
        unit_id=None if unit_id is None else str(unit_id),
    )


# ---------------------------------------------------------------------------
# Scope resolution (what the screen's rows, columns and tabs are)
# ---------------------------------------------------------------------------


@dataclass
class _Slice:
    unit_id: UUID | None
    unit_name: str | None
    offered: list[UUID]


@dataclass
class _Scope:
    mode: str
    professional: Professional | None
    services: list[tuple[UUID, str]]
    without_id: list[str]
    plans: list[tuple[UUID, str, bool]]
    slices: list[_Slice]
    # TASK-090 Adendo 1 (C7): this scope's "Outro" lines switched OFF. Not a column (the patient
    # is never offered them), but their stored cells survive a slice save, so switching the
    # convênio back ON brings its refusals back too.
    switched_off_plan_ids: frozenset[UUID] = frozenset()

    @property
    def plan_column(self):
        if self.professional is None:
            return InsuranceAcceptanceException.tenant_plan_id
        return InsuranceAcceptanceException.professional_plan_id

    def slice_for(self, unit_id: UUID | None) -> _Slice | None:
        return next((s for s in self.slices if s.unit_id == unit_id), None)


def _offered_here(marks: dict[UUID, frozenset[UUID]], service_ids: Sequence[UUID], unit_id):
    """Services of `service_ids` done at `unit_id` (no mark = every unit of the doctor)."""
    return [s for s in service_ids if marks.get(s) is None or unit_id in marks[s]]


async def _doctor_services(
    session: AsyncSession, tenant: Tenant, professional: Professional, catalog_names: dict
) -> tuple[list[UUID], list[str]]:
    """The doctor's active catalog services (in his order) and the entries with no service id."""
    catalog = await load_service_catalog(session, tenant.id)
    ids: list[UUID] = []
    without_id: list[str] = []
    for entry in cfg.professional_appointment_types(professional, tenant, catalog):
        service_id = entry_service_id(entry)
        if service_id is None:
            name = str(entry.get("name") or "").strip()
            if name and name not in without_id:
                without_id.append(name)
        elif service_id in catalog_names and service_id not in ids:
            ids.append(service_id)
    return ids, without_id


async def _active_services(session: AsyncSession, tenant_id: UUID) -> list[tuple[UUID, str]]:
    rows = await session.execute(
        select(Service.id, Service.name)
        .where(Service.tenant_id == tenant_id, Service.is_active.is_(True))
        .order_by(Service.sort_order, Service.name, Service.id)
    )
    return [(sid, name) for sid, name in rows.all()]


async def _clinic_scope(session: AsyncSession, tenant: Tenant) -> _Scope:
    services = await _active_services(session, tenant.id)
    names = dict(services)
    clinic_rows = await list_tenant_plans(session, tenant.id)
    plans = [
        (plan.id, entry.name if entry is not None else str(plan.custom_name or ""), entry is None)
        for plan, entry in clinic_rows
        if plan.is_active is not False
    ]
    switched_off = frozenset(plan.id for plan, _entry in clinic_rows if plan.is_active is False)
    doctors = list(
        await session.scalars(
            select(Professional)
            .where(Professional.tenant_id == tenant.id, Professional.is_active.is_(True))
            .order_by(Professional.name, Professional.id)
        )
    )
    # The manager's matrix shows every active unit, complete address or not (Adendo 1 §A).
    options = await unit_options_for(session, tenant, doctors, usable_only=False)
    marks = await psu.read_marks_for(session, [d.id for d in doctors])
    units = await pu.active_units(session, tenant.id)
    per_unit: dict[UUID | None, set[UUID]] = {unit.id: set() for unit in units}
    no_unit_doctors = False
    for doctor in doctors:
        service_ids, _ = await _doctor_services(session, tenant, doctor, names)
        doctor_units = options.get(doctor.id, [])
        if not doctor_units:
            no_unit_doctors = True
            per_unit.setdefault(None, set()).update(service_ids)
            continue
        for option in doctor_units:
            per_unit.setdefault(option.id, set()).update(
                _offered_here(marks.get(doctor.id, {}), service_ids, option.id)
            )
    order = {sid: i for i, (sid, _n) in enumerate(services)}
    slices = [
        _Slice(unit.id, unit.name, sorted(per_unit[unit.id], key=order.__getitem__))
        for unit in units
    ]
    if not units or no_unit_doctors:
        slices.append(_Slice(None, None, sorted(per_unit.get(None, set()), key=order.__getitem__)))
    return _Scope("shared", None, services, [], plans, slices, switched_off)


async def _professional_plan_rows(
    session: AsyncSession, tenant_id: UUID, professional_id: UUID
) -> list[tuple[UUID, str, bool, str]]:
    """The doctor's own plans as (row id, name, is_custom, clinic order key), in the clinic's
    one per-doctor-mode order (TASK-090 SPEC §4.6) - the same order the patient sees. An
    "Outro" switched OFF (C7) is not a column (and keeps its stored place in the order)."""
    rows = [
        (
            plan.id,
            entry.name if entry is not None else str(plan.custom_name or ""),
            entry is None,
            plan_order.plan_key_for_row(plan.catalog_id, plan.id),
        )
        for plan, entry in await list_professional_plans(
            session, professional_id, active_only=True
        )
    ]
    order = await plan_order.load_clinic_order(session, tenant_id) if rows else {}
    rows.sort(key=lambda row: plan_order.clinic_order_sort_key(order, row[3], row[1]))
    return rows


async def _professional_plan_columns(
    session: AsyncSession, tenant_id: UUID, professional_id: UUID
) -> list[tuple[UUID, str, bool]]:
    return [
        (row_id, name, is_custom)
        for row_id, name, is_custom, _key in await _professional_plan_rows(
            session, tenant_id, professional_id
        )
    ]


async def _professional_scope(
    session: AsyncSession, tenant: Tenant, professional: Professional
) -> _Scope:
    active = await _active_services(session, tenant.id)
    names = dict(active)
    service_ids, without_id = await _doctor_services(session, tenant, professional, names)
    services = [(sid, names[sid]) for sid in service_ids]
    plans = await _professional_plan_columns(session, tenant.id, professional.id)
    switched_off = frozenset(
        plan.id
        for plan, _entry in await list_professional_plans(session, professional.id)
        if plan.is_active is False
    )
    options = (
        await unit_options_for(session, tenant, [professional], usable_only=False)
    ).get(professional.id, [])
    marks = await psu.read_marks(session, professional.id)
    if options:
        slices = [
            _Slice(option.id, option.name, _offered_here(marks, service_ids, option.id))
            for option in options
        ]
    else:
        slices = [_Slice(None, None, list(service_ids))]
    return _Scope("independent", professional, services, without_id, plans, slices, switched_off)


async def _professional(
    session: AsyncSession, tenant: Tenant, professional_id: str
) -> Professional:
    try:
        return await hubcfg.resolve_professional(session, tenant, professional_id)
    except hubcfg.ProfessionalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Professional not found") from None


async def _clinic_guard(tenant: Tenant) -> None:
    if tenant.insurance_mode != "shared":
        raise _not_in_mode(tenant.insurance_mode, "shared")


async def _professional_guard(tenant: Tenant) -> None:
    if tenant.insurance_mode != "independent":
        raise _not_in_mode(tenant.insurance_mode, "independent")


async def _scope_for(
    session: AsyncSession, tenant: Tenant, professional: Professional | None
) -> _Scope:
    if professional is None:
        return await _clinic_scope(session, tenant)
    return await _professional_scope(session, tenant, professional)


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------


def _scope_filter(tenant_id: UUID, professional: Professional | None):
    conditions = [InsuranceAcceptanceException.tenant_id == tenant_id]
    if professional is None:
        conditions.append(InsuranceAcceptanceException.professional_id.is_(None))
    else:
        conditions.append(InsuranceAcceptanceException.professional_id == professional.id)
    return conditions


def _unit_filter(unit_id: UUID | None):
    if unit_id is None:
        return InsuranceAcceptanceException.unit_id.is_(None)
    return InsuranceAcceptanceException.unit_id == unit_id


async def _read(session: AsyncSession, tenant_id: UUID, scope: _Scope) -> InsuranceMatrixRead:
    plan_col = scope.plan_column
    rows = await session.execute(
        select(
            InsuranceAcceptanceException.unit_id,
            InsuranceAcceptanceException.service_id,
            plan_col,
        ).where(*_scope_filter(tenant_id, scope.professional), plan_col.is_not(None))
    )
    service_ids = {sid for sid, _name in scope.services}
    plan_ids = {pid for pid, _name, _custom in scope.plans}
    cells: dict[UUID | None, list[MatrixCell]] = {}
    for unit_id, service_id, plan_id in sorted(rows.all(), key=lambda r: (str(r[1]), str(r[2]))):
        # Orphans (a service/plan that left the scope) are never returned.
        if service_id in service_ids and plan_id in plan_ids:
            cells.setdefault(unit_id, []).append(
                MatrixCell(service_id=str(service_id), plan_id=str(plan_id))
            )
    return InsuranceMatrixRead(
        mode=scope.mode,  # type: ignore[arg-type]
        scope="clinic" if scope.professional is None else "professional",
        professional_id=None if scope.professional is None else str(scope.professional.id),
        services=[MatrixService(id=str(sid), name=name) for sid, name in scope.services],
        plans=[MatrixPlan(id=str(pid), name=n, is_custom=c) for pid, n, c in scope.plans],
        slices=[
            MatrixSlice(
                unit_id=None if sl.unit_id is None else str(sl.unit_id),
                unit_name=sl.unit_name,
                offered_service_ids=[str(s) for s in sl.offered],
                exceptions=cells.get(sl.unit_id, []),
            )
            for sl in scope.slices
        ],
        services_without_id=scope.without_id,
    )


def _parse_ids(raw: Sequence[str]) -> tuple[set[UUID], list[str]]:
    good: set[UUID] = set()
    bad: list[str] = []
    for value in raw:
        try:
            good.add(UUID(str(value)))
        except ValueError:
            bad.append(str(value)[:64])
    return good, bad


def _validated_cells(body: InsuranceMatrixSliceUpdate, scope: _Scope) -> set[tuple[UUID, UUID]]:
    """The body's cells as (service, plan) ids, refusing anything outside the scope (422)."""
    services, bad_services = _parse_ids([c.service_id for c in body.exceptions])
    plans, bad_plans = _parse_ids([c.plan_id for c in body.exceptions])
    known_services = {sid for sid, _n in scope.services}
    known_plans = {pid for pid, _n, _c in scope.plans}
    unknown_services = sorted([str(s) for s in services - known_services] + bad_services)
    if unknown_services:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "unknown_service_ids",
            "Um ou mais serviços não fazem parte desta matriz. Recarregue a página.",
            service_ids=unknown_services,
        )
    unknown_plans = sorted([str(p) for p in plans - known_plans] + bad_plans)
    if unknown_plans:
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "unknown_plan_ids",
            "Um ou mais convênios não fazem parte desta matriz. Recarregue a página.",
            plan_ids=unknown_plans,
        )
    return {(UUID(c.service_id), UUID(c.plan_id)) for c in body.exceptions}


async def _resolve_unit(session: AsyncSession, tenant: Tenant, raw: str, *, allow_none: bool):
    """`none` -> None; otherwise an ACTIVE unit of THIS clinic, else 404 (like the units API)."""
    if raw == NONE_KEY and allow_none:
        return None
    try:
        unit_id = UUID(raw)
    except ValueError:
        raise _unit_not_found() from None
    found = await session.scalar(
        select(Unit.id).where(
            Unit.id == unit_id, Unit.tenant_id == tenant.id, Unit.is_active.is_(True)
        )
    )
    if found is None:
        raise _unit_not_found()
    return unit_id


async def _lock(session: AsyncSession, tenant: Tenant, professional: Professional | None) -> None:
    """Serialize two saves of the same scope (SELECT ... FOR UPDATE; a no-op on SQLite)."""
    target = Tenant if professional is None else Professional
    pk = tenant.id if professional is None else professional.id
    await session.execute(select(target.id).where(target.id == pk).with_for_update())


def _row(
    tenant_id: UUID,
    professional: Professional | None,
    unit_id: UUID | None,
    service_id: UUID,
    plan_id: UUID,
) -> InsuranceAcceptanceException:
    return InsuranceAcceptanceException(
        tenant_id=tenant_id,
        professional_id=None if professional is None else professional.id,
        service_id=service_id,
        unit_id=unit_id,
        tenant_plan_id=plan_id if professional is None else None,
        professional_plan_id=None if professional is None else plan_id,
    )


async def _replace_slice(
    session: AsyncSession,
    tenant_id: UUID,
    professional: Professional | None,
    unit_id: UUID | None,
    cells: set[tuple[UUID, UUID]],
    keep_plan_ids: frozenset[UUID] = frozenset(),
) -> None:
    conditions = [*_scope_filter(tenant_id, professional), _unit_filter(unit_id)]
    if keep_plan_ids:
        # C7: the cells of an "Outro" switched OFF are not on the screen - keep them.
        plan_col = (
            InsuranceAcceptanceException.tenant_plan_id
            if professional is None
            else InsuranceAcceptanceException.professional_plan_id
        )
        conditions.append(or_(plan_col.is_(None), plan_col.not_in(keep_plan_ids)))
    await session.execute(delete(InsuranceAcceptanceException).where(*conditions))
    for service_id, plan_id in sorted(cells, key=lambda c: (str(c[0]), str(c[1]))):
        session.add(_row(tenant_id, professional, unit_id, service_id, plan_id))
    await session.flush()


async def _put(
    session: AsyncSession,
    tenant: Tenant,
    professional: Professional | None,
    unit_key: str,
    body: InsuranceMatrixSliceUpdate,
) -> InsuranceMatrixRead:
    tenant_id = tenant.id
    ids = {"tenant_id": str(tenant_id), "unit": unit_key}
    unit_id = await _resolve_unit(session, tenant, unit_key, allow_none=True)
    scope = await _scope_for(session, tenant, professional)
    if scope.slice_for(unit_id) is None:
        raise _unavailable(unit_id)
    cells = _validated_cells(body, scope)
    try:
        await _lock(session, tenant, professional)
        await _replace_slice(
            session, tenant_id, professional, unit_id, cells, scope.switched_off_plan_ids
        )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        logger.info("hub_insurance_matrix_conflict", **ids)
        raise _changed() from None
    logger.info("hub_insurance_matrix_saved", exceptions=len(cells), scope=scope.mode, **ids)
    return await _read(session, tenant_id, scope)


def _changed() -> HTTPException:
    return _error(
        status.HTTP_409_CONFLICT,
        "insurance_matrix_changed",
        "A matriz foi alterada por outra pessoa. Recarregue a página.",
    )


async def _copy(
    session: AsyncSession,
    tenant: Tenant,
    professional: Professional | None,
    body: InsuranceMatrixCopy,
) -> InsuranceMatrixRead:
    tenant_id = tenant.id
    targets_raw = body.to_unit_ids
    if (
        not targets_raw
        or len(set(targets_raw)) != len(targets_raw)
        or body.from_unit_id in targets_raw
    ):
        raise _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "copy_target_invalid",
            "Escolha ao menos uma unidade de destino, diferente da origem e sem repetir.",
        )
    source = await _resolve_unit(session, tenant, body.from_unit_id, allow_none=False)
    targets = [await _resolve_unit(session, tenant, raw, allow_none=False) for raw in targets_raw]
    scope = await _scope_for(session, tenant, professional)
    for unit_id in [source, *targets]:
        if scope.slice_for(unit_id) is None:
            raise _unavailable(unit_id)
    read = await _read(session, tenant_id, scope)
    source_cells = {
        (UUID(c.service_id), UUID(c.plan_id))
        for sl in read.slices
        if sl.unit_id == str(source)
        for c in sl.exceptions
    }
    try:
        await _lock(session, tenant, professional)
        for unit_id in targets:
            await _replace_slice(
                session, tenant_id, professional, unit_id, source_cells, scope.switched_off_plan_ids
            )
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise _changed() from None
    logger.info(
        "hub_insurance_matrix_copied",
        tenant_id=str(tenant_id),
        scope=scope.mode,
        targets=len(targets),
        exceptions=len(source_cells),
    )
    return await _read(session, tenant_id, scope)


# ---------------------------------------------------------------------------
# Routes - clinic (mode 1)
# ---------------------------------------------------------------------------


@router.get("/insurance-acceptance/clinic", response_model=InsuranceMatrixRead)
async def get_clinic_matrix(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceMatrixRead:
    await _clinic_guard(tenant)
    return await _read(session, tenant.id, await _clinic_scope(session, tenant))


@router.put(
    "/insurance-acceptance/clinic/units/{unit_key}",
    response_model=InsuranceMatrixRead,
    dependencies=[Depends(manager_only)],
)
async def put_clinic_matrix_slice(
    unit_key: str,
    body: InsuranceMatrixSliceUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceMatrixRead:
    await _clinic_guard(tenant)
    return await _put(session, tenant, None, unit_key, body)


@router.post(
    "/insurance-acceptance/clinic/copy",
    response_model=InsuranceMatrixRead,
    dependencies=[Depends(manager_only)],
)
async def post_clinic_matrix_copy(
    body: InsuranceMatrixCopy,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceMatrixRead:
    await _clinic_guard(tenant)
    return await _copy(session, tenant, None, body)


# ---------------------------------------------------------------------------
# Routes - doctor (mode 2)
# ---------------------------------------------------------------------------


@router.get(
    "/professionals/{professional_id}/insurance-acceptance",
    dependencies=[Depends(professional_reader)],
)
async def get_professional_matrix(
    professional_id: str,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceMatrixRead:
    professional = await _professional(session, tenant, professional_id)
    await _professional_guard(tenant)
    return await _read(session, tenant.id, await _professional_scope(session, tenant, professional))


@router.put(
    "/professionals/{professional_id}/insurance-acceptance/units/{unit_key}",
    dependencies=[Depends(professional_writer)],
)
async def put_professional_matrix_slice(
    professional_id: str,
    unit_key: str,
    body: InsuranceMatrixSliceUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceMatrixRead:
    professional = await _professional(session, tenant, professional_id)
    await _professional_guard(tenant)
    return await _put(session, tenant, professional, unit_key, body)


@router.post(
    "/professionals/{professional_id}/insurance-acceptance/copy",
    dependencies=[Depends(professional_writer)],
)
async def post_professional_matrix_copy(
    professional_id: str,
    body: InsuranceMatrixCopy,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceMatrixRead:
    professional = await _professional(session, tenant, professional_id)
    await _professional_guard(tenant)
    return await _copy(session, tenant, professional, body)


@router.put("/professionals/{professional_id}/insurance-acceptance/plan-order")
async def put_professional_plan_order(
    professional_id: str,
    body: PlanOrderUpdate,
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceMatrixRead:
    """Reorder the matrix columns (TASK-090 C3): the clinic's ONE convênio order (`independent`).

    `plan_ids` are this doctor's `plans[].id`, left to right, possibly a subset. The order is the
    clinic's (catalog plans share a place across doctors; each "Outro" has its own), and the plans
    this doctor does not see keep their places. 404 `professional_not_found` (another clinic's
    doctor or unknown) / `plan_not_found` (an id that is not one of this doctor's plans, nothing
    written); 409 `insurance_mode_not_applicable` outside `independent`.
    """
    try:
        professional = await hubcfg.resolve_professional(session, tenant, professional_id)
    except hubcfg.ProfessionalNotFound:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="professional_not_found") from None
    if tenant.insurance_mode != "independent":
        raise _error(
            status.HTTP_409_CONFLICT,
            "insurance_mode_not_applicable",
            "A ordem por profissional só existe quando os convênios estão no modo "
            "'lista por profissional'.",
            mode=tenant.insurance_mode,
        )
    keys = {
        row_id: key
        for row_id, _name, _custom, key in await _professional_plan_rows(
            session, tenant.id, professional.id
        )
    }
    wanted: list[UUID] = []
    for raw in dict.fromkeys(body.plan_ids):
        try:
            plan_id = UUID(raw)
        except ValueError:
            plan_id = None  # not a UUID: an unknown plan, same 404 as any other
        if plan_id is None or plan_id not in keys:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="plan_not_found")
        wanted.append(plan_id)
    wanted = list(dict.fromkeys(wanted))
    if wanted:
        await plan_order.save_subset_order(session, tenant.id, [keys[pid] for pid in wanted])
        await session.commit()
    logger.info(
        "hub_insurance_plan_order_saved",
        tenant_id=str(tenant.id),
        professional_id=str(professional.id),
        placed_count=len(wanted),
    )
    return await _read(session, tenant.id, await _professional_scope(session, tenant, professional))


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@router.get("/insurance-acceptance/summary", response_model=InsuranceMatrixSummary)
async def get_matrix_summary(
    tenant: Tenant = Depends(get_current_tenant),
    session: AsyncSession = Depends(get_session),
) -> InsuranceMatrixSummary:
    """Exception counts only (one grouped query). Counts the stored rows of the current mode;
    a row whose service/plan left the scope still counts until the slice is next saved."""
    mode = tenant.insurance_mode
    if mode not in ("shared", "independent"):
        raise _not_in_mode(mode, "shared")
    cols = (
        InsuranceAcceptanceException.professional_id,
        InsuranceAcceptanceException.unit_id,
        func.count(),
    )
    stmt = select(*cols).where(InsuranceAcceptanceException.tenant_id == tenant.id)
    if mode == "shared":
        stmt = stmt.where(
            InsuranceAcceptanceException.professional_id.is_(None),
            InsuranceAcceptanceException.tenant_plan_id.is_not(None),
        )
    else:
        active = select(Professional.id).where(
            Professional.tenant_id == tenant.id, Professional.is_active.is_(True)
        )
        stmt = stmt.where(
            InsuranceAcceptanceException.professional_id.in_(active),
            InsuranceAcceptanceException.professional_plan_id.is_not(None),
        )
    stmt = stmt.group_by(
        InsuranceAcceptanceException.professional_id, InsuranceAcceptanceException.unit_id
    )
    grouped: dict[UUID | None, list[tuple[UUID | None, int]]] = {}
    for professional_id, unit_id, count in (await session.execute(stmt)).all():
        grouped.setdefault(professional_id, []).append((unit_id, int(count)))

    def _count(entries: list[tuple[UUID | None, int]]) -> dict:
        by_unit = [
            MatrixUnitCount(unit_id=None if u is None else str(u), exception_count=n)
            for u, n in sorted(entries, key=lambda e: str(e[0]))
        ]
        return {"exception_count": sum(n for _u, n in entries), "by_unit": by_unit}

    if mode == "shared":
        return InsuranceMatrixSummary(
            mode="shared",
            clinic=MatrixCount(**_count(grouped.get(None, []))),
            professionals=[],
        )
    return InsuranceMatrixSummary(
        mode="independent",
        clinic=None,
        professionals=[
            ProfessionalMatrixCount(professional_id=str(pid), **_count(entries))
            for pid, entries in sorted(grouped.items(), key=lambda item: str(item[0]))
        ],
    )
