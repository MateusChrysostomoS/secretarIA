"""multi_professional plugin: professional-aware availability + booking.

Entitlement-gated addon (`multi_professional`). Adds four agent tools on top
of the base set (ai/tools.py): `list_professionals` (so the LLM can name the
tenant's active professionals to the patient — with specialty/about for
reasoning about fit), a professional-aware pair mirroring `list_free_slots` /
`create_event` — `list_free_slots_for_professional` and
`create_event_for_professional` — that resolve the professional BY NAME
(case-insensitive, active only) instead of taking a professional_id, because
the patient only ever gives the LLM a name in conversation, and
`select_professional_and_continue`, the hand-back that returns a confirmed
choice to the deterministic button flow (exception->sentinel, see
ai/tools.SelectProfessionalRequested).

Design choice: rather than a stateful `set_professional` tool (which would
need extra ContextVar plumbing and a "current professional" the LLM could
forget to set), every professional-aware tool call is self-contained — the
LLM passes `professional_name` on every call, same shape as the base tools'
`day`/`start`/`end` args. An unknown name returns a helpful tool-error string
listing the valid (active) names instead of raising, so the LLM can recover
within the same turn by asking the patient to pick again.

Per-professional resolution (contract v1 §10 item C/D, via `_professional_calendar`
below): calendar id, Google credential, business hours and default slot
duration ALL resolve professional-first, tenant-second, env-last —
`google_calendar_id`/business hours/services use the professional's own JSON
when set (services/tenant_config.py's `professional_business_hours` /
`professional_appointment_types`, already doing that fallback), the Google
refresh token uses the professional's own encrypted credential
(`professional_credentials`) when connected, else the tenant's. This is the
SAME CalendarService machinery the base tools use (`CalendarService.for_professional`,
via `ai.tools._calendar_for_professional`) — no separate credential path.
Once a professional is resolved by name, its `context_doctor_message` (when
set) rides along in the tool result under `professional_context` — this is
how the LLM "loads" that professional's context for the rest of the turn.

`create_event_for_professional` also accepts an optional `unit_name` (used
only when the multi_unit addon is ALSO active for this tenant — see
plugins/multi_unit.py). When provided, the unit is resolved the same
case-insensitive way and its id is persisted on the Appointment row alongside
professional_id. An empty/omitted `unit_name` behaves exactly as before unit
support existed.
"""

from uuid import UUID

from langchain_core.tools import tool

from secretaria.ai.tools import (
    SelectProfessionalRequested,
    _calendar_for_professional,
    _localize_window,
    _match_by_name,
    _persist_appointment,
    _tenant_config_ctx,
    _tenant_id_ctx,
)
from secretaria.core.logging import get_logger
from secretaria.plugins.base import PluginSpec
from secretaria.plugins.registry import register

logger = get_logger(__name__)


async def _active_professionals(tenant_id: UUID) -> list:
    """Active professionals for `tenant_id`, ordered by name. Never raises."""
    # Imported lazily, same rationale as ai/tools.py's _persist_appointment:
    # keeps this module importable without a DB/ORM in dev scripts, and avoids
    # an import cycle through models -> services.
    from secretaria.core.database import async_session_factory
    from secretaria.services.tenant_config import list_active_professionals

    async with async_session_factory() as session:
        return await list_active_professionals(session, tenant_id)


async def _professional_calendar(tenant_id: UUID, professional) -> "CalendarService":  # noqa: F821
    """Build a CalendarService using THIS professional's own config (contract v1 §10).

    Thin wrapper: the professional -> tenant -> env resolution chain (item C —
    own credential/hours/services/calendar-id first, tenant second, env last,
    with the first active resolved service's duration as the slot length)
    lives in services/tenant_config.py::resolve_professional_calendar, shared
    with the deterministic flow router's booking path (workers/tasks.py).
    This wrapper only supplies the LLM-turn specifics: the ContextVar
    TenantRuntimeConfig as the fallback-token source, and
    ai.tools._calendar_for_professional as the construction seam, so the
    ContextVar-scoped CalendarService construction stays exactly as before.
    """
    from secretaria.core.database import async_session_factory
    from secretaria.models import Tenant
    from secretaria.services.tenant_config import resolve_professional_calendar

    async with async_session_factory() as session:
        tenant = await session.get(Tenant, tenant_id)
        return await resolve_professional_calendar(
            session,
            tenant,
            professional,
            tenant_config=_tenant_config_ctx.get(),
            calendar_factory=_calendar_for_professional,
        )


def _unknown_professional_error(name: str, professionals: list) -> dict:
    names = ", ".join(p.name for p in professionals) or "nenhum profissional cadastrado"
    return {"error": f"Profissional '{name}' não encontrado. Profissionais disponíveis: {names}."}


def _with_professional_context(result: dict, professional) -> dict:
    """Attach `professional_context` to a tool result once resolved by name.

    This is how the LLM "loads" a professional's context_doctor_message once
    it has resolved them (contract v1 §10 item D) — omitted entirely when the
    professional has none set, so the tool output shape is unchanged for
    professionals without one.
    """
    if professional.context_doctor_message:
        result["professional_context"] = professional.context_doctor_message
    return result


@tool
async def list_professionals() -> dict:
    """Lista os profissionais ativos da clínica disponíveis para agendamento,
    com especialidade e descrição quando cadastradas. Use para informar ao
    paciente quais profissionais existem, antes de perguntar com qual ele quer
    marcar, ou para raciocinar sobre qual profissional atende melhor o que o
    paciente descreveu (sintoma, motivo da consulta, necessidade).
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"professionals": []}
    professionals = await _active_professionals(tenant_id)
    out = []
    for p in professionals:
        entry = {"name": p.name}
        if p.specialty:
            entry["specialty"] = p.specialty
        # `about` is the hub-editable, patient-facing bio — enough signal to
        # reason about fit. context_doctor_message stays persona-only (never
        # listed here, never used for matching).
        if p.about:
            entry["about"] = p.about
        out.append(entry)
    return {"professionals": out}


@tool
async def select_professional_and_continue(professional_name: str) -> dict:
    """Confirma a escolha de UM profissional e devolve o paciente ao fluxo
    guiado de botões (a saudação do profissional + a lista de serviços dele).
    Use SOMENTE depois que o paciente confirmar com qual profissional quer
    seguir — a partir daí o agendamento continua pelos botões, não pelo chat.

    Args:
        professional_name: Nome do profissional confirmado (ex: 'Dra. Ana').
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"error": "Nenhum profissional configurado para esta clínica."}
    professionals = await _active_professionals(tenant_id)
    professional = _match_by_name(professionals, professional_name)
    if professional is None:
        # Same recoverable error shape as the other professional-aware tools,
        # so the LLM can re-ask within the same turn.
        return _unknown_professional_error(professional_name, professionals)
    # Exception->sentinel hand-back (see ai/tools.SelectProfessionalRequested):
    # the worker re-enters the deterministic flow at this doctor's greeting.
    raise SelectProfessionalRequested(professional.id, professional.name)


@tool
async def list_free_slots_for_professional(
    professional_name: str, day: str, max_slots: int = 6
) -> dict:
    """Lista até `max_slots` horários livres de UM profissional específico,
    dentro do horário comercial da clínica, para o dia especificado. Use
    list_professionals antes se ainda não souber os nomes válidos.

    Args:
        professional_name: Nome do profissional (ex: 'Dra. Ana').
        day: Dia no formato YYYY-MM-DD (ex: 2026-05-29).
        max_slots: Quantidade máxima de slots a retornar (default 6, máx 10).
    """
    from datetime import date, datetime

    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"error": "Nenhum profissional configurado para esta clínica."}

    professionals = await _active_professionals(tenant_id)
    professional = _match_by_name(professionals, professional_name)
    if professional is None:
        return _unknown_professional_error(professional_name, professionals)

    cal = await _professional_calendar(tenant_id, professional)
    target_day = date.fromisoformat(day)
    day_dt = datetime.combine(target_day, datetime.min.time())
    slots = await cal.list_free_slots(day=day_dt, max_slots=min(max(max_slots, 1), 10))
    return _with_professional_context({"slots": slots}, professional)


@tool
async def create_event_for_professional(
    professional_name: str,
    start: str,
    end: str,
    summary: str,
    description: str = "",
    unit_name: str = "",
) -> dict:
    """Cria uma consulta na agenda de UM profissional específico. Use SOMENTE
    depois de list_free_slots_for_professional (ou check_availability) E
    confirmação explícita do paciente.

    Args:
        professional_name: Nome do profissional (ex: 'Dra. Ana').
        start: Início em ISO 8601 (ex: 2026-05-27T14:00:00).
        end: Fim em ISO 8601.
        summary: Título do evento, ex: 'Consulta - João Silva'.
        description: Notas adicionais (opcional).
        unit_name: Nome da unidade/local, se a clínica tiver mais de uma
            (opcional — deixe em branco se não houver múltiplas unidades).
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return {"error": "Nenhum profissional configurado para esta clínica."}

    professionals = await _active_professionals(tenant_id)
    professional = _match_by_name(professionals, professional_name)
    if professional is None:
        return _unknown_professional_error(professional_name, professionals)

    unit_id: UUID | None = None
    if unit_name.strip():
        # Imported lazily to avoid a hard dependency on the multi_unit plugin
        # module when it isn't installed/entitled for this tenant.
        from secretaria.plugins.multi_unit import _resolve_unit_or_error

        unit, error = await _resolve_unit_or_error(tenant_id, unit_name)
        if error is not None:
            return error
        unit_id = unit.id

    cal = await _professional_calendar(tenant_id, professional)
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
        summary,
        professional_id=professional.id,
        unit_id=unit_id,
    )
    return _with_professional_context(
        {
            "id": event.get("id"),
            "status": event.get("status"),
            "htmlLink": event.get("htmlLink"),
        },
        professional,
    )


MULTI_PROFESSIONAL_SPEC = PluginSpec(
    id="multi_professional",
    entitlement_keys=("multi_professional",),
    agent_tools=(
        list_professionals,
        list_free_slots_for_professional,
        create_event_for_professional,
        select_professional_and_continue,
    ),
)
register(MULTI_PROFESSIONAL_SPEC)
