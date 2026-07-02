"""Response schemas for the internal service-to-service API (`/internal/*`).

These are deliberately LEAN projections of the ORM rows, built field-by-field by the
router (never `model_validate(orm)`), so internal-only columns are not handed to the
calling service. In particular an appointment's Google identifiers (`google_event_id`,
`google_event_link`) and its `conversation_id` are NOT exposed here — the doctor portal
that consumes this (via brain-api) has no use for them, and they are secretaria's
internal bookkeeping. Add a field only when a consumer genuinely needs it.

Every list is wrapped as `{"data": [...]}` (the agreed internal envelope).
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from secretaria.models.appointment import AppointmentStatus


class InternalAppointment(BaseModel):
    """One appointment, projected for a sibling service (no Google/internal ids)."""

    id: UUID
    patient_id: UUID | None
    appointment_type: str | None
    start_at: datetime | None
    end_at: datetime | None
    status: AppointmentStatus
    phone: str | None


class InternalAppointmentList(BaseModel):
    """`GET /internal/tenants/{tenant_id}/appointments` response envelope."""

    data: list[InternalAppointment]


class InternalPatient(BaseModel):
    """One patient, projected for a sibling service."""

    id: UUID
    name: str | None
    # WhatsApp id (== the patient's phone in E.164-ish digits) — the only contact
    # handle secretaria stores for a patient; the doctor portal needs it to reach them.
    wa_id: str
    created_at: datetime


class InternalPatientList(BaseModel):
    """`GET /internal/tenants/{tenant_id}/patients` response envelope."""

    data: list[InternalPatient]
