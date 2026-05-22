"""LangGraph tools - STUBS.

These will become real LangChain tools bound to the AI graph. For now each
one raises NotImplementedError.
"""

# TODO(ai): turn these into real `@tool`-decorated callables and bind them to
#   the graph in ai/graph.py. They will delegate to:
#     - secretaria.services.calendar.CalendarService
#     - secretaria.services.precheck.PrecheckClient


async def check_calendar_availability(day: str) -> dict:
    """Tool: list free appointment slots for a given day (ISO date string)."""
    raise NotImplementedError("Calendar tool is a future phase.")


async def book_appointment(day: str, time: str, patient_id: str) -> dict:
    """Tool: book an appointment slot for a patient."""
    raise NotImplementedError("Calendar tool is a future phase.")


async def start_precheck(patient_id: str) -> dict:
    """Tool: kick off the Precheck anamnese flow for a patient."""
    raise NotImplementedError("Precheck tool is a future phase.")
