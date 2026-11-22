"""TASK-091: is a professional ready to be booked, and what is still missing.

ONE rule, shared by the hub (the card's red warning) and the patient flow (who may be offered):

* ``servicos``  - at least one ACTIVE service of their own (`professional_appointment_types`);
* ``horarios``  - weekly hours of their own (`professional_business_hours`), or own hours on an
  active link to an active unit they attend (`unit_hours` offers those slots even when the
  professional's own hours are closed);
* ``unidades``  - only when the clinic has ACTIVE units: at least one active link to an active unit;
* ``convenios`` - only when ``Tenant.insurance_mode == "independent"``: at least one plan of their
  own. In `shared` / `clinic_with_exceptions` (and a clinic that never chose) the doctor accepts the
  clinic's list, so a convênio is never what is missing.

`booking_ready` is computed for EVERY professional. It is only used as a FILTER for professionals
flagged `requires_self_setup` (`list_bookable_professionals`): the owner and everyone created before
the flag existed behave exactly as before, whatever their readiness says.

Pure read: nothing here writes, and no function takes a clock or a channel.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Professional, ProfessionalInsurancePlan, Tenant
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.models.unit import Unit
from secretaria.services.tenant_config import (
    professional_appointment_types,
    professional_business_hours,
)

Missing = Literal["servicos", "horarios", "unidades", "convenios"]


@dataclass(frozen=True)
class Readiness:
    """`ready` is exactly `missing == ()`; `missing` keeps the stable order of the four items."""

    ready: bool
    missing: tuple[Missing, ...]


def _has_open_window(hours: dict | None) -> bool:
    """Same reading as `tenant_config._filter_active_hours`: a weekday with no windows is closed."""
    return any(windows for windows in (hours or {}).values())


async def professionals_readiness(
    session: AsyncSession, tenant: Tenant, professionals: Sequence[Professional]
) -> dict[UUID, Readiness]:
    """Readiness of every professional in `professionals` (one batch of queries, not one per row).

    Every professional must belong to `tenant`; the result is keyed by professional id.
    """
    if not professionals:
        return {}
    ids = [p.id for p in professionals]

    clinic_has_units = bool(
        await session.scalar(
            select(func.count())
            .select_from(Unit)
            .where(Unit.tenant_id == tenant.id, Unit.is_active.is_(True))
        )
    )

    # Active links to ACTIVE units of this tenant: professional -> [own link hours, ...]
    attended: dict[UUID, list[dict | None]] = {}
    if clinic_has_units:
        rows = await session.execute(
            select(ProfessionalUnit.professional_id, ProfessionalUnit.business_hours)
            .join(Unit, Unit.id == ProfessionalUnit.unit_id)
            .where(
                ProfessionalUnit.professional_id.in_(ids),
                ProfessionalUnit.is_active.is_(True),
                Unit.tenant_id == tenant.id,
                Unit.is_active.is_(True),
            )
        )
        for professional_id, link_hours in rows:
            attended.setdefault(professional_id, []).append(link_hours)

    needs_own_plan = getattr(tenant, "insurance_mode", None) == "independent"
    with_plan: set[UUID] = set()
    if needs_own_plan:
        with_plan = set(
            await session.scalars(
                select(ProfessionalInsurancePlan.professional_id)
                .where(ProfessionalInsurancePlan.professional_id.in_(ids))
                .distinct()
            )
        )

    result: dict[UUID, Readiness] = {}
    for professional in professionals:
        missing: list[Missing] = []
        if not professional_appointment_types(professional, tenant):
            missing.append("servicos")
        own_hours = bool(professional_business_hours(professional, tenant))
        unit_hours = any(_has_open_window(h) for h in attended.get(professional.id, []))
        if not (own_hours or unit_hours):
            missing.append("horarios")
        if clinic_has_units and professional.id not in attended:
            missing.append("unidades")
        if needs_own_plan and professional.id not in with_plan:
            missing.append("convenios")
        result[professional.id] = Readiness(ready=not missing, missing=tuple(missing))
    return result


async def professional_readiness(
    session: AsyncSession, tenant: Tenant, professional: Professional
) -> Readiness:
    """Readiness of ONE professional (the batch of one)."""
    return (await professionals_readiness(session, tenant, [professional]))[professional.id]
