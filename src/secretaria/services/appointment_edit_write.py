"""Write an edit onto the SAME appointment row (TASK-032 R6, shared with R7).

Two callers, one writer, so the patient's "Alterar Dados"
(workers/shared/appointment_edit_apply.py) and the clinic's "Editar/Remarcar"
(services/staff_appointment_edit.py) can never disagree about what an edit does to a
row: the convênio plan ids re-resolved by the TASK-006/008 catalogue rules, the
service / doctor / attendee / event fields, the end, and - only when the start moved -
the new start plus the RESCHEDULED status (a logged transition with the caller's
source). Each caller keeps its own guards (the patient's Pix limits; the clinic's
live/tenant checks), its own outbox decision and its own transaction: this function
never commits.
"""

from datetime import UTC
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Appointment, AppointmentStatus
from secretaria.services.appointment_edit import EditDraft
from secretaria.services.appointment_status import log_status_transition
from secretaria.services.insurance_catalog import resolve_booking_plan_ids
from secretaria.services.patient_context import as_utc


def row_draft(appointment, timezone: str | None) -> EditDraft:
    """The appointment row as an `EditDraft` in the clinic's zone (current == original)."""
    return EditDraft.from_appointment(
        {
            "id": str(appointment.id),
            "appointment_type": appointment.appointment_type,
            "professional_id": appointment.professional_id,
            "start_at": appointment.start_at,
            "end_at": appointment.end_at,
            "insurance": appointment.insurance,
            "attendee_name": appointment.attendee_name,
        },
        ZoneInfo(timezone or "America/Sao_Paulo"),
    )


async def write_appointment_edit(
    session: AsyncSession,
    appointment: Appointment,
    *,
    tenant_id: UUID,
    timezone: str | None,
    edit: dict,
    source: str,
    idempotency_key: str,
) -> list[str]:
    """Apply `edit` (R6's dict shape) to the caller-locked row; returns what changed.

    The names are R6's (`EditDraft.changed()`: data, horário, serviço, médico,
    convênio, paciente) - what the doctor e-mail outbox records.
    """
    before = row_draft(appointment, timezone)
    previous = appointment.status
    plan_id, professional_plan_id = await resolve_booking_plan_ids(
        session, tenant_id, edit["insurance"], edit["professional_id"]
    )
    appointment.appointment_type = edit["appointment_type"] or appointment.appointment_type
    appointment.professional_id = edit["professional_id"]
    appointment.insurance = edit["insurance"]
    appointment.insurance_plan_id = plan_id
    appointment.insurance_professional_plan_id = professional_plan_id
    appointment.attendee_name = edit["attendee_name"]
    appointment.google_event_id = edit["google_event_id"]
    if edit.get("calendar_changed", edit["doctor_changed"]):
        appointment.google_event_link = edit.get("google_event_link")
    appointment.end_at = as_utc(edit["end_at"]).astimezone(UTC)
    if edit["time_changed"]:
        appointment.start_at = as_utc(edit["start_at"]).astimezone(UTC)
        appointment.status = AppointmentStatus.RESCHEDULED
        log_status_transition(
            appointment_id=appointment.id,
            tenant_id=tenant_id,
            old_status=previous,
            new_status=AppointmentStatus.RESCHEDULED,
            source=source,
            idempotency_key=idempotency_key,
        )
    after = row_draft(appointment, timezone)
    return EditDraft(str(appointment.id), after.current, before.current).changed()
