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
from secretaria.services.booking_scope import PatientRoster
from secretaria.services.calendar import CalendarService
from secretaria.services.hub_configuration import (
    PROFESSIONAL_CONFIG_FIELDS,
    tenant_scalar_updates,
)
from secretaria.services.insurance_catalog import load_tenant_insurance
from secretaria.services.list_options import needs_unit_options
from secretaria.services.professional_units import (
    patient_hidden_professional_ids,
    usable_units,
)
from secretaria.services.service_catalog import load_service_catalog, resolve_entries
from secretaria.services.tenant_config import (
    list_active_professionals,
    professional_business_hours,
)
from secretaria.services.unit_booking import attach_unit_options
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
    for name, value in tenant_scalar_updates(draft or {}).items():
        setattr(view, name, value)
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
    # TASK-090 Adendo 1: the simulation and the Portal options OFFER only the doctors a patient
    # may book with; every active doctor still decides the topology (owner rulings, I1/C1).
    hidden = await patient_hidden_professional_ids(session, tenant.id, [p.id for p in rows])
    blocked = {p.id for p in rows if getattr(p, "booking_blocked", False)}
    catalog = await load_service_catalog(session, tenant.id)
    insurance = await load_tenant_insurance(session, tenant.id)

    tenant_view = overlay_tenant(tenant, draft_tenant)
    professional_views = [
        overlay_professional(p, draft_professional, applies=(p.id == professional_id))
        for p in rows
    ]
    # TASK-094 D4: the address a booking without a unit shows comes from the usable units.
    snapshot = _flow_tenant_snapshot(
        tenant_view,
        professional_views,
        catalog,
        insurance,
        usable_units=await usable_units(session, tenant.id),
    )
    everyone = [
        SimpleNamespace(
            id=p.id,
            name=p.name,
            specialty=p.specialty,
            about=p.about,
            context_doctor_message=p.context_doctor_message,
            min_booking_notice_hours=getattr(p, "min_booking_notice_hours", None),
            buffer_minutes=getattr(p, "buffer_minutes", None),
            appointment_types=(
                resolve_entries(p.appointment_types, catalog)
                if p.appointment_types
                else p.appointment_types
            ),
            business_hours=p.business_hours,  # verbatim: NULL means "inherits the clinic's hours"
            # TASK-091: the column copies lose the readiness mark `list_active_professionals` set.
            booking_blocked=p.id in blocked,
        )
        for p in professional_views
    ]
    # Junção 090+091: neither a hidden doctor nor an unready invitee is OFFERED; the shape is
    # `booking_scope.topology_roster` (the hidden doctor counts, the invitee does not).
    flow_professionals = PatientRoster(
        [p for p in everyone if p.id not in hidden and not p.booking_blocked], everyone=everyone
    )
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


async def attach_unit_options_for_scopes(
    session: AsyncSession, tenant: Tenant, inputs: SimulationInputs, scopes: list
) -> None:
    """Give `inputs.professionals` their units and service x unit marks when a card's scope
    needs them (`list_options.needs_unit_options`: the services-at-unit list, TASK-083).

    `load_simulation_inputs` itself stays as it is, so the clinic-setup simulation shows what
    it showed before; only the Portal's options route and tap check call this, with the stored
    scopes of the cards they resolve. `attach_unit_options` reads THIS tenant's units only.
    """
    if not any(needs_unit_options(scope) for scope in scopes):
        return
    rows = await list_active_professionals(session, tenant.id)
    await attach_unit_options(session, tenant, rows, inputs.professionals)
