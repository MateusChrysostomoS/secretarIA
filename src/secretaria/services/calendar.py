"""Google Calendar integration - STUB.

The real implementation (OAuth, free/busy queries, event creation) is a
future phase. Every method raises NotImplementedError on purpose.
"""

from datetime import datetime


class CalendarService:
    """Stub interface for the clinic's Google Calendar."""

    async def list_available_slots(self, day: datetime, duration_minutes: int = 30) -> list[dict]:
        """Return bookable time slots for a given day."""
        raise NotImplementedError("Google Calendar integration is a future phase.")

    async def create_event(
        self,
        start: datetime,
        end: datetime,
        summary: str,
        description: str = "",
    ) -> dict:
        """Create a calendar event (an appointment)."""
        raise NotImplementedError("Google Calendar integration is a future phase.")

    async def cancel_event(self, event_id: str) -> None:
        """Cancel a previously created event."""
        raise NotImplementedError("Google Calendar integration is a future phase.")
