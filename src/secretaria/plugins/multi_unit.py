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
    _blocked_tenant_level,
    _canonical_appointment_type,
    _get_calendar,
    _localize_window,
    _match_by_name,
    _persist_appointment,
    _tenant_id_ctx,
)
from secretaria.core.logging import get_logger
from secretaria.plugins.base import PluginSpec
from secretaria.plugins.registry import register

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
async def list_units() -> dict:
    """Lista as unidades/locais ativos da clínica, com nome e endereço. Use
    para informar ao paciente onde a clínica atende, ou antes de perguntar em
    qual unidade ele prefere ser atendido.
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"units": []}
    units = await _active_units(tenant_id)
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
    # This books on the CLINIC's own calendar with no professional in the
    # loop, so it is a tenant-level mutation exactly like the base
    # `create_event` — and just as wrong for a multi-professional tenant.
    # Entitlements only ADD tools, so a tenant holding multi_unit could reach
    # the wrong agenda through here; the shared guard refuses before any
    # Google call (ai/tools.py::_blocked_tenant_level).
    blocked = _blocked_tenant_level("create_event_at_unit")
    if blocked is not None:
        return blocked
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"error": "Nenhuma unidade configurada para esta clínica."}

    unit, error = await _resolve_unit_or_error(tenant_id, unit_name)
    if error is not None:
        return error

    canonical_type, service_error = _canonical_appointment_type(
        appointment_type, "create_event_at_unit"
    )
    if service_error is not None:
        return service_error

    cal = _get_calendar()
    fallback_start, fallback_end = _localize_window(start, end, cal)
    event = await cal.create_event(
        start=fallback_start,
        end=fallback_end,
        summary=summary,
        description=description,
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
        "htmlLink": event.get("htmlLink"),
    }


MULTI_UNIT_SPEC = PluginSpec(
    id="multi_unit",
    entitlement_keys=("multi_unit",),
    agent_tools=(list_units, create_event_at_unit),
)
register(MULTI_UNIT_SPEC)
