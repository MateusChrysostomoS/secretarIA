"""LangChain tools for the LangGraph agent.

Each tool wraps a CalendarService method. The active CalendarService is stored
in a ContextVar so the process-wide cached agent can serve different tenants
concurrently without interference. graph.py sets the ContextVar before invoking
the agent and resets it afterwards.
"""

from contextvars import ContextVar
from datetime import date, datetime

from langchain_core.tools import tool

from secretaria.services.calendar import CalendarService

# Per-async-task CalendarService. Set by graph.run_agent before invoking the
# agent; each concurrent worker task has its own slot.
_calendar_ctx: ContextVar[CalendarService | None] = ContextVar("_calendar", default=None)


def _get_calendar() -> CalendarService:
    cal = _calendar_ctx.get()
    if cal is None:
        # Fallback for dev scripts that call invoke_agent directly without
        # setting a tenant context (Fase A single-tenant convenience).
        return CalendarService()
    return cal


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
    event = await _get_calendar().create_event(
        start=datetime.fromisoformat(start),
        end=datetime.fromisoformat(end),
        summary=summary,
        description=description,
    )
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
    return {"status": "cancelled"}
