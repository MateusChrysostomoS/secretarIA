"""LangChain tools for the LangGraph agent.

Each tool wraps a CalendarService method. The active CalendarService is stored
in a ContextVar so the process-wide cached agent can serve different tenants
concurrently without interference. graph.py sets the ContextVars before invoking
the agent and resets them afterwards.

Side effect of a successful booking/cancellation: a row in the `appointments`
table is created/updated so the platform has a record independent of Google
Calendar. This is wrapped so a DB hiccup never fails the calendar operation.
"""

from collections.abc import Sequence
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import quote
from uuid import UUID

from langchain_core.tools import tool

from secretaria.config import get_settings
from secretaria.core.logging import get_logger
from secretaria.services.calendar import CalendarService
from secretaria.services.precheck import HandoffOutcome, request_precheck_handoff

if TYPE_CHECKING:
    from secretaria.services.tenant_config import TenantRuntimeConfig

logger = get_logger(__name__)

# Per-async-task CalendarService. Set by graph.run_agent before invoking the
# agent; each concurrent worker task has its own slot.
_calendar_ctx: ContextVar[CalendarService | None] = ContextVar("_calendar", default=None)

# Tenant + conversation the current agent turn belongs to, used to persist
# appointment rows. None in dev scripts that invoke the agent directly.
_tenant_id_ctx: ContextVar[UUID | None] = ContextVar("_tenant_id", default=None)
_conversation_id_ctx: ContextVar[UUID | None] = ContextVar("_conversation_id", default=None)

# Per-async-task TenantRuntimeConfig. Set by graph.run_agent; read here (and by
# plugin tools, e.g. plugins/multi_professional.py) to build a CalendarService
# scoped to something other than the tenant's own calendar_id (see
# `_calendar_for_calendar_id` below). Lives in this module (not graph.py) so a
# plugin tool module can import it without reaching into graph.py.
_tenant_config_ctx: ContextVar["TenantRuntimeConfig | None"] = ContextVar(
    "_tenant_config", default=None
)

# Per-async-task arq Redis pool, used ONLY to fire-and-forget enqueue
# `run_post_booking_hooks` after a successful booking (see
# `_persist_appointment` below and plugins/post_booking.py). Set by
# graph.run_agent from the `redis` it receives from workers/tasks.py;
# `None` (dev scripts, or no arq pool reachable) makes the enqueue a silent
# no-op rather than an error.
_redis_ctx: ContextVar[Any | None] = ContextVar("_redis", default=None)

# Note on calendar outages: a tool that hits CalendarUnavailableError simply
# lets it propagate. LangGraph's ToolNode re-raises it out of the agent's
# ainvoke (it is not a ToolInvocationError), and graph.run_agent catches it by
# type to return the CALENDAR_UNAVAILABLE sentinel. We deliberately do NOT use
# a ContextVar flag for this: LangGraph runs each node in a COPIED context
# (contextvars.copy_context()), so a flag set inside the tool would be invisible
# to run_agent.


def _get_calendar() -> CalendarService:
    cal = _calendar_ctx.get()
    if cal is None:
        # Fallback for dev scripts that call invoke_agent directly without
        # setting a tenant context (Fase A single-tenant convenience).
        return CalendarService()
    return cal


def _calendar_for_calendar_id(google_calendar_id: str | None) -> CalendarService:
    """Build a CalendarService scoped to a specific Google Calendar id.

    Used by plugin tools that book against something other than the tenant's
    own calendar (e.g. plugins/multi_professional.py's per-professional
    calendars). `google_calendar_id=None` (or falsy) is a deliberate no-op —
    the tenant's own calendar_id is kept, which is exactly the "falls back to
    the tenant's calendar" behavior the multi_professional addon wants when a
    professional has no calendar of their own configured.

    Falls back to `_get_calendar()` (the base ContextVar) when no
    TenantRuntimeConfig is set — same dev-script convenience as `_get_calendar`.
    """
    config = _tenant_config_ctx.get()
    if config is None:
        return _get_calendar()
    if google_calendar_id:
        config = replace(config, google_calendar_id=google_calendar_id)
    return CalendarService.from_tenant_config(config)


def _calendar_for_professional(
    *,
    google_calendar_id: str | None,
    google_refresh_token: str | None,
    business_hours: dict | None,
    appointment_duration_min: int | None = None,
) -> CalendarService:
    """Build a CalendarService for ONE professional's own config (contract v1 §10 item C).

    `google_calendar_id`/`google_refresh_token`/`business_hours`/
    `appointment_duration_min` are already resolved by the caller
    (plugins/multi_professional.py) through the professional -> tenant
    fallback chain (services/tenant_config.py); this only substitutes them
    onto the CURRENT TenantRuntimeConfig via `CalendarService.for_professional`.
    Falls back to `_calendar_for_calendar_id` (which itself falls back to
    `_get_calendar()`) when no TenantRuntimeConfig is set — same dev-script
    convenience as the rest of this module.
    """
    config = _tenant_config_ctx.get()
    if config is None:
        return _calendar_for_calendar_id(google_calendar_id)
    return CalendarService.for_professional(
        config,
        google_calendar_id=google_calendar_id,
        google_refresh_token=google_refresh_token,
        business_hours=business_hours,
        appointment_duration_min=appointment_duration_min,
    )


def _match_by_name(items: Sequence[Any], name: str) -> Any | None:
    """Case-insensitive exact match of `name` against `item.name` in `items`.

    Shared by plugin tools that resolve a professional/unit by the name the
    LLM heard from the patient (plugins/multi_professional.py,
    plugins/multi_unit.py). Returns None when nothing matches — callers build
    their own "valid options" error message from the full `items` list.
    """
    target = name.strip().casefold()
    for item in items:
        if item.name.strip().casefold() == target:
            return item
    return None


def _event_window(
    event: dict, fallback_start: datetime, fallback_end: datetime
) -> tuple[datetime, datetime]:
    """Authoritative booked window from Google's response, else the inputs.

    Google's event payload carries RFC3339 datetimes with an offset, so they
    are timezone-aware (safe for a TIMESTAMPTZ column). The fallbacks must be
    made tz-aware by the caller.
    """
    try:
        start = datetime.fromisoformat(event["start"]["dateTime"])
        end = datetime.fromisoformat(event["end"]["dateTime"])
        return start, end
    except (KeyError, TypeError, ValueError):
        return fallback_start, fallback_end


def _localize_window(start: str, end: str, cal: CalendarService) -> tuple[datetime, datetime]:
    """Parse LLM-supplied ISO 8601 start/end and localize naive values to `cal`'s tz.

    Shared by every create_event*-shaped tool (base `create_event` here, plus
    plugins/multi_professional.py's `create_event_for_professional` and
    plugins/multi_unit.py's `create_event_at_unit`) so a fallback persist still
    gets tz-aware values for the TIMESTAMPTZ columns even if Google's response
    is unexpectedly missing the authoritative window.
    """
    parsed_start = datetime.fromisoformat(start)
    parsed_end = datetime.fromisoformat(end)
    if parsed_start.tzinfo is None:
        parsed_start = parsed_start.replace(tzinfo=cal.tzinfo)
    if parsed_end.tzinfo is None:
        parsed_end = parsed_end.replace(tzinfo=cal.tzinfo)
    return parsed_start, parsed_end


async def _persist_appointment(
    event: dict,
    fallback_start: datetime,
    fallback_end: datetime,
    appointment_type: str,
    professional_id: UUID | None = None,
    unit_id: UUID | None = None,
) -> None:
    """Record a bot-created appointment. Best-effort: never raises.

    Skipped silently when no tenant context is set (dev scripts). A DB failure
    is logged but does not undo the (already successful) Google Calendar event.

    `professional_id`/`unit_id` are optional — set only by the
    multi_professional/multi_unit plugin tools (plugins/multi_professional.py,
    plugins/multi_unit.py); the base tools never pass them, so a plain
    booking still gets NULL in both columns exactly as before this addon
    support was added.
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return
    conversation_id = _conversation_id_ctx.get()
    # Imported lazily to keep this module importable without a DB/ORM in the
    # dev terminal, and to avoid an import cycle through models -> services.
    from secretaria.core.database import async_session_factory
    from secretaria.models import Appointment, AppointmentStatus, Conversation, Patient

    appointment: Any | None = None
    try:
        async with async_session_factory() as session:
            async with session.begin():
                patient_id: UUID | None = None
                phone: str | None = None
                if conversation_id is not None:
                    conversation = await session.get(Conversation, conversation_id)
                    if conversation is not None:
                        patient_id = conversation.patient_id
                        if patient_id is not None:
                            patient = await session.get(Patient, patient_id)
                            if patient is not None:
                                phone = patient.wa_id
                start_at, end_at = _event_window(event, fallback_start, fallback_end)
                appointment = Appointment(
                    tenant_id=tenant_id,
                    patient_id=patient_id,
                    conversation_id=conversation_id,
                    google_event_id=event.get("id") or "",
                    google_event_link=event.get("htmlLink"),
                    appointment_type=(appointment_type or "").strip()[:120] or None,
                    start_at=start_at,
                    end_at=end_at,
                    phone=phone,
                    status=AppointmentStatus.SCHEDULED,
                    professional_id=professional_id,
                    unit_id=unit_id,
                )
                session.add(appointment)
        logger.info("tool_appointment_persisted", event_id=event.get("id"))
    except Exception as exc:
        # The calendar event already exists; a missing DB row is recoverable
        # and must not break the booking the patient just made.
        logger.warning("tool_appointment_persist_failed", error=str(exc), event_id=event.get("id"))
        return

    # Fire-and-forget: enqueue post_booking plugin hooks (EHR push, Pix
    # deposit ask, analytics event, ...) off the hot path. Never awaited
    # inline and never allowed to affect the booking reply — see
    # plugins/post_booking.py.
    if appointment is not None:
        from secretaria.plugins.post_booking import enqueue_post_booking_hooks

        await enqueue_post_booking_hooks(
            _redis_ctx.get(), tenant_id, appointment.id, source="agent"
        )


async def _mark_appointment_cancelled(event_id: str) -> None:
    """Flip the matching appointment row(s) to CANCELLED. Best-effort."""
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        # No tenant context (dev scripts): skip rather than issue a
        # tenant-unscoped UPDATE that could touch other tenants' rows
        # (google_event_id is indexed but not globally unique).
        return
    if not event_id:
        return
    from sqlalchemy import update

    from secretaria.core.database import async_session_factory
    from secretaria.models import Appointment, AppointmentStatus

    try:
        async with async_session_factory() as session:
            async with session.begin():
                result = await session.execute(
                    update(Appointment)
                    .where(
                        Appointment.google_event_id == event_id,
                        Appointment.tenant_id == tenant_id,
                    )
                    .values(status=AppointmentStatus.CANCELLED)
                )
        logger.info("tool_appointment_cancelled", event_id=event_id, rows=result.rowcount)
    except Exception as exc:
        logger.warning("tool_appointment_cancel_persist_failed", error=str(exc), event_id=event_id)


@tool
async def check_availability(start: str, end: str) -> dict:
    """Lista eventos que conflitam com o intervalo [start, end) no calendário
    da clínica. Cada item tem id, summary, start, end (ISO 8601). Lista vazia
    significa que a janela está totalmente livre. Eventos de dia inteiro e
    eventos marcados como 'livre' são ignorados.

    Args:
        start: Início da janela em ISO 8601 (ex: 2026-05-27T14:00:00).
        end: Fim da janela em ISO 8601.
    """
    busy = await _get_calendar().check_availability(
        datetime.fromisoformat(start),
        datetime.fromisoformat(end),
    )
    return {"busy": busy}


@tool
async def list_free_slots(day: str, max_slots: int = 6) -> dict:
    """Lista até `max_slots` horários livres dentro do horário comercial da
    clínica para o dia especificado. Use quando o paciente pedir um dia inteiro
    (\"tem horário sexta?\") ou quando quiser oferecer alternativas. Renderize
    a resposta usando o marcador [SLOTS] do sistema.

    Args:
        day: Dia no formato YYYY-MM-DD (ex: 2026-05-29).
        max_slots: Quantidade máxima de slots a retornar (default 6, máx 10).
    """
    target_day = date.fromisoformat(day)
    day_dt = datetime.combine(target_day, datetime.min.time())
    slots = await _get_calendar().list_free_slots(
        day=day_dt,
        max_slots=min(max(max_slots, 1), 10),
    )
    return {"slots": slots}


@tool
async def create_event(
    start: str,
    end: str,
    summary: str,
    description: str = "",
) -> dict:
    """Cria um evento (consulta) no calendário da clínica. Use SOMENTE depois
    de check_availability E confirmação explícita do paciente.

    Args:
        start: Início em ISO 8601 (ex: 2026-05-27T14:00:00).
        end: Fim em ISO 8601.
        summary: Título do evento, ex: 'Consulta - João Silva'.
        description: Notas adicionais (opcional).
    """
    cal = _get_calendar()
    fallback_start, fallback_end = _localize_window(start, end, cal)
    event = await cal.create_event(
        start=fallback_start,
        end=fallback_end,
        summary=summary,
        description=description,
    )
    await _persist_appointment(event, fallback_start, fallback_end, summary)

    return {
        "id": event.get("id"),
        "status": event.get("status"),
        "htmlLink": event.get("htmlLink"),
    }


@tool
async def cancel_event(event_id: str) -> dict:
    """Cancela (deleta) um evento existente pelo seu id.

    Args:
        event_id: ID do evento no Google Calendar.
    """
    await _get_calendar().cancel_event(event_id)
    await _mark_appointment_cancelled(event_id)
    return {"status": "cancelled"}


async def _resolve_patient_phone() -> str | None:
    """Best-effort patient WhatsApp id (phone) for the current turn.

    Mirrors `_persist_appointment`'s phone resolution: `_conversation_id_ctx`
    (set by graph.run_agent) -> Conversation.patient_id -> Patient.wa_id.
    Returns None when there is no conversation context (dev scripts) or
    nothing resolves — callers must treat that as "can't proceed", not crash.
    """
    conversation_id = _conversation_id_ctx.get()
    if conversation_id is None:
        return None
    # Imported lazily, same reason as _persist_appointment above.
    from secretaria.core.database import async_session_factory
    from secretaria.models import Conversation, Patient

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None or conversation.patient_id is None:
            return None
        patient = await session.get(Patient, conversation.patient_id)
        return patient.wa_id if patient is not None else None


# Precheck hand-off tool guidance text (PT-BR), one per `HandoffOutcome` plus
# the "feature not configured for this clinic" pre-check. Kept as module
# constants so the outcome->text mapping is easy to audit/test in one place.
_PRECHECK_UNAVAILABLE_TEXT = (
    "A pré-consulta ainda não está disponível para esta clínica no momento. "
    "Você pode seguir normalmente com o agendamento."
)
_PRECHECK_NOT_ENTITLED_TEXT = "Esse recurso de pré-consulta não está disponível para esta clínica."
_PRECHECK_CONFLICT_TEXT = (
    "O paciente já tem uma pré-consulta em andamento no número da PreCheck. "
    "Oriente-o a continuar por lá em vez de abrir uma nova conversa."
)
_PRECHECK_TEMPORARY_FAILURE_TEXT = (
    "Não foi possível iniciar a pré-consulta agora (instabilidade temporária). "
    "Tente novamente em alguns instantes."
)


@tool
async def iniciar_pre_consulta() -> str:
    """Inicia a pré-consulta (anamnese) do paciente desta clínica, encaminhando-o
    por WhatsApp para o número da PreCheck. Use SOMENTE quando o paciente
    precisar preencher o questionário de pré-consulta desta clínica antes da
    consulta marcada — este recurso é restrito a essa tarefa específica desta
    clínica (política da Meta), não é uma ferramenta de mensageria genérica.

    Não recebe argumentos: o telefone do paciente e a clínica são resolvidos
    automaticamente a partir da conversa atual.
    """
    settings = get_settings()
    if not settings.PRECHECK_WHATSAPP_NUMBER:
        return _PRECHECK_UNAVAILABLE_TEXT

    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        # Dev scripts / no tenant context: nothing to check entitlement
        # against, so behave exactly like "not configured for this clinic".
        logger.warning("tool_precheck_handoff_no_tenant_context")
        return _PRECHECK_UNAVAILABLE_TEXT

    phone = await _resolve_patient_phone()
    if not phone:
        logger.warning("tool_precheck_handoff_no_patient_phone", tenant_id=str(tenant_id))
        return _PRECHECK_TEMPORARY_FAILURE_TEXT

    result = await request_precheck_handoff(tenant_id, phone)

    if result.outcome in (HandoffOutcome.SEEDED, HandoffOutcome.ALREADY_ACTIVE):
        link = (
            f"https://wa.me/{settings.PRECHECK_WHATSAPP_NUMBER}"
            f"?text={quote(settings.PRECHECK_HANDOFF_PREFILL)}"
        )
        return (
            "Pré-consulta liberada para o paciente. Envie este link para que ele "
            f"continue no WhatsApp da pré-consulta: {link}"
        )
    if result.outcome in (HandoffOutcome.NOT_ENTITLED, HandoffOutcome.NO_CLINIC):
        return _PRECHECK_NOT_ENTITLED_TEXT
    if result.outcome is HandoffOutcome.CONFLICT:
        return _PRECHECK_CONFLICT_TEXT
    return _PRECHECK_TEMPORARY_FAILURE_TEXT
