"""Which conversations are "my patients" for a doctor (TASK-046 R8, spec 2026-10-09 §5.D).

The console's "Meus pacientes" chip is a FILTER, never a permission: every person at the
clinic sees every conversation; a doctor may narrow the list to patients who have or had
an appointment with him. This module is the one definition of that relation, as a SQL
clause the hub list adds to its own query (one round trip, no per-row lookups).

"Have or had" = an appointment of the same clinic, for this professional, in any status of
`models/appointment.py::DOCTOR_PATIENT_STATUSES` (everything but CANCELLED). The match is
by patient: phone-only bookings and Google-only events have no patient and never count.
"""

from uuid import UUID

from sqlalchemy import ColumnElement, select

from secretaria.models.appointment import DOCTOR_PATIENT_STATUSES, Appointment
from secretaria.models.conversation import Conversation


def is_patient_of(professional_id: UUID) -> ColumnElement[bool]:
    """EXISTS clause, correlated to `Conversation`: its patient is this doctor's patient."""
    return (
        select(Appointment.id)
        .where(
            Appointment.tenant_id == Conversation.tenant_id,
            Appointment.patient_id == Conversation.patient_id,
            Appointment.professional_id == professional_id,
            Appointment.status.in_(DOCTOR_PATIENT_STATUSES),
        )
        .correlate(Conversation)
        .exists()
    )
