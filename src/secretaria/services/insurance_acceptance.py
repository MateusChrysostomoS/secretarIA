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

from secretaria.models.insurance import ProfessionalInsurancePlan
from secretaria.models.insurance_acceptance_exception import InsuranceAcceptanceException
from secretaria.services.flow_units import offered_units, units_for_service
from secretaria.services.insurance_catalog import _independent_plan_id

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
