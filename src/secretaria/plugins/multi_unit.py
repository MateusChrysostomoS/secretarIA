"""multi_unit plugin: unit (physical location) awareness for booking.

Entitlement-gated addon (`multi_unit`). Units do not carry their own Calendar
credentials — they only record WHERE the patient should show up. Booking
itself keeps using the tenant's own calendar (or, when multi_professional is
ALSO active, the professional's calendar — see
plugins/multi_professional.py's `create_event_for_professional(unit_name=...)`,
which calls `_resolve_unit_or_error` below to share this exact resolution
logic rather than duplicating it).

Two tools:
  - `list_units`: names + addresses of active units, so the LLM can tell the
    patient where to go / ask them to choose one.
  - `create_event_at_unit`: for tenants that have multi_unit WITHOUT
    multi_professional — a single-calendar booking tool that records which
    unit the appointment is at. Tenants with BOTH addons use
    `create_event_for_professional`'s optional `unit_name` instead; this tool
    still works for them too (it just never sets professional_id), but the
    LLM is steered towards the professional-aware tool by the system prompt
    listing whichever tools are actually injected for the tenant's addons.

Same by-name resolution as multi_professional.py: the patient only ever gives
the LLM a name in conversation, so tools take `unit_name` (case-insensitive,
active only) rather than a unit_id. An unknown name returns a helpful
tool-error string listing the valid (active) unit names.
"""

from uuid import UUID

from langchain_core.tools import tool
from sqlalchemy import select

from secretaria.ai.tools import (
    _blocked_by_required_deposit,
    _blocked_by_toolset_v2,
    _blocked_tenant_level,
    _canonical_appointment_type,
    _conversation_attendee_name,
    _conversation_insurance,
    _create_booking_event,
    _effective_service_catalog,
    _get_calendar,
    _hand_back_if_slot_taken,
    _insurance_refusal,
    _localize_window,
    _match_by_name,
    _persist_appointment,
    _service_day_refusal,
    _service_unit_refusal,
    _sole_professional_id,
    _tenant_id_ctx,
)
from secretaria.ai.unit_scope import (
    outside_unit_hours,
    owner_service_catalog,
    professional_unit_options,
    resolve_tool_unit,
    service_id_of,
    sole_professional_row,
    valid_unit_options,
)
from secretaria.core.logging import get_logger
from secretaria.plugins.base import PluginSpec
from secretaria.plugins.registry import register
from secretaria.services.calendar import (
    build_event_description,
    build_patient_calendar_link,
    build_patient_link_details,
)
from secretaria.services.unit_booking import (
    event_unit_fields,
    scoped_calendar,
    unit_fields,
    unit_location,
)

logger = get_logger(__name__)


async def _active_units(tenant_id: UUID) -> list:
    """Active units for `tenant_id`, ordered by name. Never raises."""
    from secretaria.core.database import async_session_factory
    from secretaria.models.unit import Unit

    async with async_session_factory() as session:
        rows = await session.scalars(
            select(Unit)
            .where(Unit.tenant_id == tenant_id, Unit.is_active.is_(True))
            .order_by(Unit.name)
        )
        return list(rows)


def _unknown_unit_error(name: str, units: list) -> dict:
    names = ", ".join(u.name for u in units) or "nenhuma unidade cadastrada"
    return {"error": f"Unidade '{name}' não encontrada. Unidades disponíveis: {names}."}


async def _resolve_unit_or_error(
    tenant_id: UUID, unit_name: str
) -> tuple[object | None, dict | None]:
    """Resolve an active unit by name for `tenant_id`.

    Returns (unit, None) on success or (None, error_dict) on failure — the
    error_dict is the exact tool-error payload a caller (this module's own
    `create_event_at_unit`, or plugins/multi_professional.py's
    `create_event_for_professional`) should return verbatim to the LLM.
    """
    units = await _active_units(tenant_id)
    unit = _match_by_name(units, unit_name)
    if unit is None:
        return None, _unknown_unit_error(unit_name, units)
    return unit, None


@tool
async def list_units(service_name: str = "", professional_name: str = "") -> dict:
    """Lista as unidades/locais ativos da clínica, com nome e endereço. Use
    para informar ao paciente onde a clínica atende, ou antes de perguntar em
    qual unidade ele prefere ser atendido.

    Args:
        service_name: Nome EXATO do serviço que o paciente quer marcar (opcional). Quando
            informado, só vêm as unidades onde esse serviço é feito — ofereça só essas.
        professional_name: Nome do médico (opcional). Quando informado, só vêm as unidades
            onde ELE atende (e, com service_name, onde ele faz esse serviço).
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"units": []}
    catalog = None
    if (professional_name or "").strip():
        # A named doctor (follow-up A1): tenant-scoped lookup, the same one the
        # professional-aware tools use; the doctor's OWN catalog names their services.
        from secretaria.plugins.multi_professional import (
            _active_professionals,
            _professional_services,
            _unknown_professional_error,
        )

        professionals = await _active_professionals(tenant_id)
        owner = _match_by_name(professionals, professional_name)
        if owner is None:
            return _unknown_professional_error(professional_name, professionals)
        catalog = await _professional_services(tenant_id, owner)
    else:
        # TASK-083: a clinic whose doctor attends at some of its units offers only those.
        owner = await sole_professional_row(tenant_id, _sole_professional_id())
    if (service_name or "").strip():
        # ... and, for a named service, only the units where the doctor does that service.
        canonical, error = _canonical_appointment_type(
            service_name,
            "list_units",
            _effective_service_catalog() if catalog is None else catalog,
        )
        if error is not None:
            return error
        # TASK-085 final review: the service id comes from the DOCTOR's stored catalog - the
        # turn's effective one (one-doctor clinic) holds `RuntimeAppointmentType` objects.
        if owner is not None:
            owned = (
                catalog if catalog is not None else await owner_service_catalog(tenant_id, owner)
            )
            service_id = service_id_of(owned, canonical)
        else:
            service_id = None
        if owner is not None and service_id is not None:
            if await professional_unit_options(tenant_id, owner):
                units = await valid_unit_options(tenant_id, owner, service_id)
                return {"units": [{"name": u.name, "address": u.address} for u in units]}
    offered = [u for u in await professional_unit_options(tenant_id, owner) if u.bookable]
    if not offered and (professional_name or "").strip():
        # Follow-up final review: a NAMED doctor without an open unit never gets every unit of
        # the clinic (that would invent places where he does not attend).
        return {
            "units": [],
            "note": (
                f"{owner.name} não tem unidade aberta para agendamento; "
                "não ofereça unidade com esse médico."
            ),
        }
    units = offered or await _active_units(tenant_id)
    return {"units": [{"name": u.name, "address": u.address} for u in units]}


@tool
async def create_event_at_unit(
    unit_name: str,
    start: str,
    end: str,
    summary: str,
    description: str = "",
    appointment_type: str = "",
) -> dict:
    """Cria uma consulta na agenda da clínica, associada a uma unidade
    específica. Use SOMENTE depois de check_availability E confirmação
    explícita do paciente.

    Args:
        unit_name: Nome da unidade/local (ex: 'Unidade Centro').
        start: Início em ISO 8601 (ex: 2026-05-27T14:00:00).
        end: Fim em ISO 8601.
        summary: Título do evento no Google Agenda, ex: 'Consulta - João
            Silva'. É só o título da agenda — NÃO use este campo para dizer
            qual é o serviço.
        description: Notas adicionais (opcional).
        appointment_type: Nome EXATO do serviço da clínica que está sendo
            agendado. Não invente e não use o nome do paciente. Se a clínica
            tiver só um serviço, pode deixar em branco.
    """
    blocked = _blocked_by_toolset_v2("create_event_at_unit")
    if blocked is not None:
        return blocked
    # This books on the CLINIC's own calendar with no professional in the
    # loop, so it is a tenant-level mutation exactly like the base
    # `create_event` — and just as wrong for a multi-professional tenant.
    # Entitlements only ADD tools, so a tenant holding multi_unit could reach
    # the wrong agenda through here; the shared guard refuses before any
    # Google call (ai/tools.py::_blocked_tenant_level).
    blocked = _blocked_tenant_level("create_event_at_unit")
    if blocked is not None:
        return blocked
    blocked = await _blocked_by_required_deposit("create_event_at_unit")
    if blocked is not None:
        return blocked
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"error": "Nenhuma unidade configurada para esta clínica."}

    # TASK-083: when the clinic's doctor attends at units, only one of THEIR units, within its
    # hours; otherwise any active unit of the clinic, exactly as before.
    owner = await sole_professional_row(tenant_id, _sole_professional_id())
    unit, error = await resolve_tool_unit(tenant_id, owner, unit_name)
    if error is None and unit is None:
        unit, error = await _resolve_unit_or_error(tenant_id, unit_name)
    if error is not None:
        return error

    canonical_type, service_error = _canonical_appointment_type(
        appointment_type, "create_event_at_unit"
    )
    if service_error is not None:
        return service_error
    # The service must be one the doctor does at this unit (spec §6.1, §9.13).
    refused = await _service_unit_refusal("create_event_at_unit", unit, owner, None, canonical_type)
    if refused is not None:
        return refused
    # TASK-086 A14 (review): the convênio matrix at this unit, before any calendar call.
    refused = await _insurance_refusal(
        "create_event_at_unit",
        unit,
        _effective_service_catalog(),
        canonical_type,
        professional_id=getattr(owner, "id", None),
    )
    if refused is not None:
        return refused

    cal = scoped_calendar(_get_calendar(), unit)
    fallback_start, fallback_end = _localize_window(start, end, cal)
    # TASK-087 G3: a day the service is blocked on (this unit's own list, else the base).
    refused = await _service_day_refusal(
        "create_event_at_unit",
        unit,
        owner,
        None,
        canonical_type,
        fallback_start.astimezone(cal.tzinfo),
    )
    if refused is not None:
        return refused
    outside = outside_unit_hours(unit, fallback_start, fallback_end, cal.tzinfo)
    if outside is not None:
        return outside
    attendee_name = await _conversation_attendee_name()
    insurance = await _conversation_insurance()
    # TASK-083 follow-up: the event (and the patient's link) say where and with whom.
    auto_description = build_event_description(
        service=canonical_type,
        insurance=insurance,
        attendee_name=attendee_name,
        **event_unit_fields(unit, owner),
    )
    link_details = build_patient_link_details(
        service=canonical_type,
        insurance=insurance,
        start=fallback_start,
        tz=cal.tzinfo,
        attendee_name=attendee_name,
        professional=getattr(owner, "name", None),
        **unit_fields(unit),
    )
    if description:
        link_details = f"{link_details}\n\n{description}".strip()
    description = (
        f"{auto_description}\n\n{description}".strip() if description else auto_description
    )
    await _hand_back_if_slot_taken(
        cal,
        fallback_start,
        fallback_end,
        tool_name="create_event_at_unit",
        appointment_type=canonical_type,
    )
    event = await _create_booking_event(
        cal,
        fallback_start,
        fallback_end,
        summary=summary,
        description=description,
        tool_name="create_event_at_unit",
        appointment_type=canonical_type,
    )
    await _persist_appointment(
        event,
        fallback_start,
        fallback_end,
        canonical_type,
        unit_id=unit.id,
        source="agent_unit",
    )
    return {
        "id": event.get("id"),
        "status": event.get("status"),
        # The event on the clinic's calendar (right for the clinic) and the
        # public add-to-calendar link (the only one the PATIENT can open).
        # This tool is the sole booking tool for a tenant with multi_unit but
        # no multi_professional, so omitting the second one would leave those
        # clinics with no link at all — see
        # services/calendar.py::build_patient_calendar_link.
        "htmlLink": event.get("htmlLink"),
        "patient_calendar_link": build_patient_calendar_link(
            fallback_start,
            fallback_end,
            summary,
            link_details,
            tz=cal.tzinfo,
            location=unit_location(unit.name, unit.address),
        ),
    }


MULTI_UNIT_SPEC = PluginSpec(
    id="multi_unit",
    entitlement_keys=("multi_unit",),
    agent_tools=(list_units, create_event_at_unit),
)
register(MULTI_UNIT_SPEC)
