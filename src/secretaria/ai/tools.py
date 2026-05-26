"""LangChain tools for the LangGraph agent.

Each tool wraps a CalendarService method. The CalendarService is built
lazily at module import and reused across calls — a process-wide singleton
that amortises the OAuth credential refresh.
"""

from datetime import datetime

from langchain_core.tools import tool

from secretaria.services.calendar import CalendarService

_calendar: CalendarService | None = None


def _get_calendar() -> CalendarService:
    global _calendar
    if _calendar is None:
        _calendar = CalendarService()
    return _calendar


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
