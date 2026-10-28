"""Build the inputs `flow_router.route()` needs from a clinic's SAVED data plus an unsaved DRAFT.

Mirrors what `workers/orchestrator.py` assembles for a real turn (the active professionals as plain
objects, the clinic's service catalog, the convênio catalog, `_flow_tenant_snapshot`), so the
simulation cannot drift from production. The difference is the draft: the manager's unsaved edits
are laid over IN-MEMORY COPIES. Nothing here writes or commits, and no ORM object is ever mutated
(`SimpleNamespace` copies only) - the tests prove both.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Professional, Tenant
from secretaria.services.calendar import CalendarService
from secretaria.services.hub_configuration import (
    PROFESSIONAL_CONFIG_FIELDS,
    TENANT_SCALAR_FIELDS,
)
from secretaria.services.insurance_catalog import load_tenant_insurance
from secretaria.services.service_catalog import load_service_catalog, resolve_entries
from secretaria.services.tenant_config import (
    list_active_professionals,
    professional_business_hours,
)
from secretaria.workers.shared.flow_simulation import SimCalendar
from secretaria.workers.shared.greeting import _flow_tenant_snapshot


@dataclass(frozen=True)
class SimulationInputs:
    tenant: SimpleNamespace  # what route() receives as `tenant`
    professionals: list[SimpleNamespace]  # what route() receives as `professionals`
    calendar_for: Any  # Callable[[str | None], CalendarService]


def _copy_of(row: Any) -> SimpleNamespace:
    """A detached, attribute-only copy of an ORM row (columns only, no session)."""
    return SimpleNamespace(**{c.name: getattr(row, c.name) for c in row.__table__.columns})


def overlay_tenant(tenant: Tenant, draft: dict | None) -> SimpleNamespace:
    view = _copy_of(tenant)
    for name in TENANT_SCALAR_FIELDS:
        if draft and name in draft:
            setattr(view, name, draft[name])
    return view


def overlay_professional(
    professional: Professional, draft: dict | None, applies: bool
) -> SimpleNamespace:
    view = _copy_of(professional)
    if applies and draft:
        for name in PROFESSIONAL_CONFIG_FIELDS:
            if name in draft:
                setattr(view, name, draft[name])
    return view


async def load_simulation_inputs(
    session: AsyncSession,
    tenant: Tenant,
    *,
    draft_tenant: dict | None = None,
    professional_id: UUID | None = None,
    draft_professional: dict | None = None,
) -> SimulationInputs:
    """READ-ONLY. Never commits; never touches the ORM objects it is given."""
    rows = await list_active_professionals(session, tenant.id)
    catalog = await load_service_catalog(session, tenant.id)
    insurance = await load_tenant_insurance(session, tenant.id)

    tenant_view = overlay_tenant(tenant, draft_tenant)
    professional_views = [
        overlay_professional(p, draft_professional, applies=(p.id == professional_id))
        for p in rows
    ]
    snapshot = _flow_tenant_snapshot(tenant_view, professional_views, catalog, insurance)
    flow_professionals = [
        SimpleNamespace(
            id=p.id,
            name=p.name,
            specialty=p.specialty,
            about=p.about,
            context_doctor_message=p.context_doctor_message,
            min_booking_notice_hours=getattr(p, "min_booking_notice_hours", None),
            appointment_types=(
                resolve_entries(p.appointment_types, catalog)
                if p.appointment_types
                else p.appointment_types
            ),
            business_hours=p.business_hours,  # verbatim: NULL means "inherits the clinic's hours"
        )
        for p in professional_views
    ]
    by_id = {str(p.id): p for p in professional_views}

    def calendar_for(selected_id: str | None) -> CalendarService:
        # The selected doctor's own opening hours, exactly like the real flow's per-doctor agenda;
        # otherwise the clinic's (a sole doctor's hours are already folded into the snapshot).
        professional = by_id.get(str(selected_id)) if selected_id else None
        hours = (
            professional_business_hours(professional, tenant_view)
            if professional is not None
            else snapshot.business_hours
        )
        return SimCalendar(
            timezone=tenant_view.timezone or "America/Sao_Paulo",
            business_hours=hours,
            slot_minutes=tenant_view.appointment_duration_min or 30,
        )

    return SimulationInputs(snapshot, flow_professionals, calendar_for)
