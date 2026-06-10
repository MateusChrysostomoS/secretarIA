"""LangChain tools for the LangGraph agent.

Each tool wraps a CalendarService method. The active CalendarService is stored
in a ContextVar so the process-wide cached agent can serve different tenants
concurrently without interference. graph.py sets the ContextVars before invoking
the agent and resets them afterwards.

Side effect of a successful booking/cancellation: a row in the `appointments`
table is created/updated so the platform has a record independent of Google
Calendar. This is wrapped so a DB hiccup never fails the calendar operation.
"""

from contextvars import ContextVar
from datetime import date, datetime
from uuid import UUID

from langchain_core.tools import tool

from secretaria.core.logging import get_logger
from secretaria.services.calendar import CalendarService

logger = get_logger(__name__)

# Per-async-task CalendarService. Set by graph.run_agent before invoking the
# agent; each concurrent worker task has its own slot.
_calendar_ctx: ContextVar[CalendarService | None] = ContextVar("_calendar", default=None)

# Tenant + conversation the current agent turn belongs to, used to persist
# appointment rows. None in dev scripts that invoke the agent directly.
_tenant_id_ctx: ContextVar[UUID | None] = ContextVar("_tenant_id", default=None)
_conversation_id_ctx: ContextVar[UUID | None] = ContextVar("_conversation_id", default=None)

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


async def _persist_appointment(
    event: dict,
    fallback_start: datetime,
    fallback_end: datetime,
    appointment_type: str,
) -> None:
    """Record a bot-created appointment. Best-effort: never raises.

    Skipped silently when no tenant context is set (dev scripts). A DB failure
    is logged but does not undo the (already successful) Google Calendar event.
    """
    tenant_id = _tenant_id_ctx.get()
    if tenant_id is None:
        return
    conversation_id = _conversation_id_ctx.get()
    # Imported lazily to keep this module importable without a DB/ORM in the
    # dev terminal, and to avoid an import cycle through models -> services.
    from secretaria.core.database import async_session_factory
    from secretaria.models import Appointment, AppointmentStatus, Conversation, Patient

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
                session.add(
                    Appointment(
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
                    )
                )
        logger.info("tool_appointment_persisted", event_id=event.get("id"))
    except Exception as exc:
        # The calendar event already exists; a missing DB row is recoverable
        # and must not break the booking the patient just made.
        logger.warning(
            "tool_appointment_persist_failed", error=str(exc), event_id=event.get("id")
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
        logger.warning(
            "tool_appointment_cancel_persist_failed", error=str(exc), event_id=event_id
        )


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
    event = await cal.create_event(
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        summary=summary,
        description=description,
    )

    # Localize the LLM-supplied (naive) times so a fallback persist still gets
    # tz-aware values for the TIMESTAMPTZ columns.
    fallback_start = datetime.fromisoformat(start)
    fallback_end = datetime.fromisoformat(end)
    if fallback_start.tzinfo is None:
        fallback_start = fallback_start.replace(tzinfo=cal.tzinfo)
    if fallback_end.tzinfo is None:
        fallback_end = fallback_end.replace(tzinfo=cal.tzinfo)
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
