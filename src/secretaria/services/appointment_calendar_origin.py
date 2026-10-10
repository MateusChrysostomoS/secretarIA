"""Physical event origin, independent of the doctor who may see an appointment."""

CALENDAR_SOURCE_CLINIC = "clinic"
CALENDAR_SOURCE_PROFESSIONAL = "professional"


class CalendarOriginUnresolved(ValueError):
    """Explicit provenance cannot identify a calendar; never guess the clinic."""


def calendar_professional_id(appointment):
    """None means the clinic calendar; NULL source preserves legacy doctor inference.

    Accept row objects and internal appointment snapshots. The source is written
    by calendar creation/movement only, never editable by a patient or hub caller.
    """
    if isinstance(appointment, dict):
        source = appointment.get("google_calendar_source")
        professional_id = appointment.get("professional_id")
    else:
        source = getattr(appointment, "google_calendar_source", None)
        professional_id = appointment.professional_id
    if source == CALENDAR_SOURCE_CLINIC:
        return None
    if source is not None and (source != CALENDAR_SOURCE_PROFESSIONAL or professional_id is None):
        raise CalendarOriginUnresolved("calendar_unresolved")
    return professional_id
