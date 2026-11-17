"""Convênio acceptance per (professional, service, unit) - TASK-086 (SPEC E1/E6/E9).

The table `insurance_acceptance_exceptions` stores only what is NOT accepted; this module is the
single rule on top of it:

    no row for the cell  -> accepted (today's behaviour, nothing to backfill)

Everything here is pure except `load_acceptance_exceptions`, which reads a clinic's whole matrix
in ONE query (E9) so the flow resolves every question in memory (no N+1).

Ids are strings in the FLOW's id space for the plan (see `insurance_catalog._independent_plan_id`):
mode `shared` = `tenant_insurance_plans.id`; mode `independent` = the catalog id of a catalog pick
or the doctor's own `professional_insurance_plans.id` for an "Outro". Rows whose service, unit or
plan is no longer in scope are simply never asked for, so an orphan never matches anything.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models.insurance import ProfessionalInsurancePlan
from secretaria.models.insurance_acceptance_exception import InsuranceAcceptanceException
from secretaria.models.tenant import Tenant
from secretaria.services.flow_units import offered_units, units_for_service
from secretaria.services.insurance_catalog import _independent_plan_id, resolve_booking_plan_ids
from secretaria.services.service_catalog import entry_service_id

logger = get_logger(__name__)

# (service_id, unit_id | None, plan_id in the flow's id space) - all str.
Key = tuple[str, str | None, str]


@dataclass(frozen=True)
class AcceptanceExceptions:
    """A clinic's refused cells. Empty (the default) = accepts everything."""

    clinic: frozenset[Key] = frozenset()  # mode `shared`: one matrix for every doctor
    by_professional: Mapping[str, frozenset[Key]] = field(default_factory=dict)  # `independent`


def _s(value: Any) -> str | None:
    return None if value is None else str(value)


def is_accepted(
    exc: AcceptanceExceptions,
    professional_id: Any,
    service_id: Any,
    unit_id: Any,
    plan_id: Any,
) -> bool:
    """Whether `plan_id` is accepted for (`service_id`, `unit_id`) by this doctor.

    An unknown service or plan (not picked yet, Particular, a free-text "Outro", a legacy name)
    is never refused. `unit_id=None` asks about the "no unit" slice only - not "any unit".
    """
    service, plan = _s(service_id), _s(plan_id)
    if service is None or plan is None:
        return True
    key: Key = (service, _s(unit_id), plan)
    if key in exc.clinic:
        return False
    mine = exc.by_professional.get(str(professional_id))
    return not (mine and key in mine)


def accepting_units(
    exc: AcceptanceExceptions, professional: Any | None, service_id: Any, plan_id: Any
) -> list:
    """The bookable units for `service_id` where `plan_id` is accepted, in the doctor's order.

    Starts from `flow_units.units_for_service`, so the per-service unit marks (TASK-083) still
    apply. A doctor with no unit link has no units to return: use `accepts_anywhere`.
    """
    pid = getattr(professional, "id", None)
    service = None if service_id is None else _as_uuid(service_id)
    return [
        unit
        for unit in units_for_service(professional, service)
        if is_accepted(exc, pid, service_id, unit.id, plan_id)
    ]


def accepts_anywhere(
    exc: AcceptanceExceptions, professional: Any | None, service_id: Any, plan_id: Any
) -> bool:
    """Accepted at at least one place the service can be booked.

    With unit links: at least one `accepting_units`. Without any (`free` flow, no units): the
    "no unit" slice. A restricted service whose units are all closed yields False, like
    `units_for_service` does ("never all").
    """
    if not offered_units(professional):
        return is_accepted(exc, getattr(professional, "id", None), service_id, None, plan_id)
    return bool(accepting_units(exc, professional, service_id, plan_id))


def _as_uuid(value: Any) -> UUID:
    return value if isinstance(value, UUID) else UUID(str(value))


async def load_acceptance_exceptions(
    session: AsyncSession, tenant_id: UUID, mode: str | None
) -> AcceptanceExceptions:
    """The tenant's whole matrix for the active mode in ONE query (E9).

    `shared` reads the clinic matrix, `independent` the per-doctor ones (translating each
    doctor-plan row to the flow's plan id); any other mode (`clinic_with_exceptions`, not chosen
    yet) has no matrix and costs no query. Always scoped by `tenant_id`.
    """
    if mode == "shared":
        rows = await session.execute(
            select(
                InsuranceAcceptanceException.service_id,
                InsuranceAcceptanceException.unit_id,
                InsuranceAcceptanceException.tenant_plan_id,
            ).where(
                InsuranceAcceptanceException.tenant_id == tenant_id,
                InsuranceAcceptanceException.professional_id.is_(None),
                InsuranceAcceptanceException.tenant_plan_id.is_not(None),
            )
        )
        return AcceptanceExceptions(
            clinic=frozenset((str(s), _s(u), str(p)) for s, u, p in rows.all())
        )
    if mode == "independent":
        rows = await session.execute(
            select(
                InsuranceAcceptanceException.professional_id,
                InsuranceAcceptanceException.service_id,
                InsuranceAcceptanceException.unit_id,
                ProfessionalInsurancePlan.catalog_id,
                ProfessionalInsurancePlan.id,
            )
            .join(
                ProfessionalInsurancePlan,
                ProfessionalInsurancePlan.id == InsuranceAcceptanceException.professional_plan_id,
            )
            .where(
                InsuranceAcceptanceException.tenant_id == tenant_id,
                InsuranceAcceptanceException.professional_id.is_not(None),
            )
        )
        grouped: dict[str, set[Key]] = {}
        for prof_id, service_id, unit_id, catalog_id, plan_row_id in rows.all():
            grouped.setdefault(str(prof_id), set()).add(
                (str(service_id), _s(unit_id), _independent_plan_id(catalog_id, plan_row_id))
            )
        return AcceptanceExceptions(by_professional={k: frozenset(v) for k, v in grouped.items()})
    return AcceptanceExceptions()


# --- Shared by the T4 call sites (flow, edit, IA, hub, workers) ---------------------------------

EMPTY = AcceptanceExceptions()


def has_exceptions(exc: AcceptanceExceptions | None) -> bool:
    """Whether the clinic has any refused cell at all (the cheap "matrix untouched" test)."""
    return exc is not None and bool(exc.clinic or exc.by_professional)


def snapshot_exceptions(tenant: Any) -> AcceptanceExceptions:
    """The matrix carried by the router's tenant snapshot (`insurance_refused`), or EMPTY.

    getattr on purpose: older/bare snapshots (tests, simulation inputs) simply have no matrix,
    which means "accepts everything" - today's behaviour.
    """
    exc = getattr(tenant, "insurance_refused", None)
    return exc if isinstance(exc, AcceptanceExceptions) else EMPTY


def refuses_everywhere(
    exc: AcceptanceExceptions, professional: Any | None, service_id: Any, plan_id: Any
) -> bool:
    """True only when the MATRIX is why this service cannot be booked with this plan.

    The matrix only ever subtracts: with no refused cell this is always False (the
    characterization guarantee - a clinic that never touched the matrix sees no change). A
    doctor with no unit link asks the "no unit" slice; a doctor with units asks every unit the
    service is bookable at (`units_for_service`, TASK-083 marks included). When there is no such
    unit at all (every unit closed / service not done anywhere) it is NOT the matrix's doing and
    the existing "no unit" path keeps handling it -> False.
    """
    if not has_exceptions(exc) or service_id is None or plan_id is None:
        return False
    pid = getattr(professional, "id", None)
    if not offered_units(professional):
        return not is_accepted(exc, pid, service_id, None, plan_id)
    units = units_for_service(professional, _as_uuid(service_id))
    if not units:
        return False
    return not any(is_accepted(exc, pid, service_id, unit.id, plan_id) for unit in units)


def refuses_all_services(
    exc: AcceptanceExceptions, professional: Any | None, services: list | None, plan_id: Any
) -> bool:
    """True when the doctor takes `plan_id` in NONE of `services` because of the matrix.

    `services` are the doctor's resolved entries (`professional_appointment_types`). An entry
    without `service_id` (legacy JSON) is outside the matrix and always accepts -> False. No
    services at all -> False (nothing to judge; the existing "no services" path handles it).
    """
    if not has_exceptions(exc) or plan_id is None or not services:
        return False
    for entry in services:
        service_id = entry_service_id(entry) if isinstance(entry, dict) else None
        if service_id is None:
            return False
        if not refuses_everywhere(exc, professional, service_id, plan_id):
            return False
    return True


async def booking_refused_by_ids(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    professional_id: UUID | None,
    service_id: UUID | None,
    unit_id: UUID | None,
    tenant_plan_id: UUID | None,
    professional_plan_id: UUID | None,
) -> bool:
    """Write-time guard straight from the database (fresh, never a stale snapshot).

    True when the cell (service, unit, plan) is a stored exception for the tenant's CURRENT mode:
    `shared` -> the clinic matrix row of `tenant_plan_id`; `independent` -> the doctor's row of
    `professional_plan_id`. Unknown service or no plan (Particular, typed "Outro", legacy name)
    -> False. Any other mode has no matrix -> False. Always scoped by `tenant_id`.
    """
    if service_id is None or (tenant_plan_id is None and professional_plan_id is None):
        return False
    mode = await session.scalar(select(Tenant.insurance_mode).where(Tenant.id == tenant_id))
    unit_clause = (
        InsuranceAcceptanceException.unit_id.is_(None)
        if unit_id is None
        else InsuranceAcceptanceException.unit_id == unit_id
    )
    if mode == "shared" and tenant_plan_id is not None:
        scope = (
            InsuranceAcceptanceException.professional_id.is_(None),
            InsuranceAcceptanceException.tenant_plan_id == tenant_plan_id,
        )
    elif mode == "independent" and professional_plan_id is not None and professional_id:
        scope = (
            InsuranceAcceptanceException.professional_id == professional_id,
            InsuranceAcceptanceException.professional_plan_id == professional_plan_id,
        )
    else:
        return False
    found = await session.scalar(
        select(InsuranceAcceptanceException.id)
        .where(
            InsuranceAcceptanceException.tenant_id == tenant_id,
            InsuranceAcceptanceException.service_id == service_id,
            unit_clause,
            *scope,
        )
        .limit(1)
    )
    return found is not None


async def booking_refused(
    session: AsyncSession,
    *,
    tenant_id: UUID,
    professional_id: UUID | None,
    service_id: UUID | None,
    unit_id: UUID | None,
    insurance: str | None,
) -> bool:
    """`booking_refused_by_ids` from the convênio NAME a booking carries.

    Resolves the name exactly like the appointment row will (`resolve_booking_plan_ids`), so the
    guard and the stored plan can never disagree. Callers log refusals with ids only, never the
    plan name (`log_refusal`).
    """
    if service_id is None or not insurance or not str(insurance).strip():
        return False
    tenant_plan_id, professional_plan_id = await resolve_booking_plan_ids(
        session, tenant_id, insurance, professional_id
    )
    return await booking_refused_by_ids(
        session,
        tenant_id=tenant_id,
        professional_id=professional_id,
        service_id=service_id,
        unit_id=unit_id,
        tenant_plan_id=tenant_plan_id,
        professional_plan_id=professional_plan_id,
    )


def log_refusal(event: str, **ids: Any) -> None:
    """One structured log line for a matrix refusal - ids only (no plan/patient names, LGPD)."""
    logger.info(
        event, **{key: (None if value is None else str(value)) for key, value in ids.items()}
    )
