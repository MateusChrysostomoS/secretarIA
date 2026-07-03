"""Response schemas for the LGPD privacy endpoints (api/internal_privacy.py).

Every field here is a lean, field-by-field projection of the matching ORM
row (never `model_validate(orm)` blindly for the same reason as
schemas/internal.py — internal bookkeeping the export contract doesn't
promise stays out unless explicitly listed). Unlike schemas/internal.py this
export is deliberately FULL for the patient/appointment/message/consent rows
themselves — this endpoint's entire purpose is "everything we hold about
this subject" for an LGPD data-portability request.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from secretaria.models.appointment import AppointmentStatus


class PrivacyPatient(BaseModel):
    """The patient row, in full."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    wa_id: str
    name: str | None
    reminder_opt_out: bool
    created_at: datetime


class PrivacyAppointment(BaseModel):
    """One appointment row, in full."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    patient_id: UUID | None
    conversation_id: UUID | None
    google_event_id: str
    google_event_link: str | None
    appointment_type: str | None
    start_at: datetime | None
    end_at: datetime | None
    phone: str | None
    professional_id: UUID | None
    unit_id: UUID | None
    status: AppointmentStatus
    created_at: datetime
    updated_at: datetime


class PrivacyConversation(BaseModel):
    """One conversation: id, state, timestamps (no message bodies here)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    handover_state: str
    flow_state: str
    created_at: datetime
    updated_at: datetime


class PrivacyMessage(BaseModel):
    """One message: direction/sender, body, timestamps."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    direction: str
    sender: str
    body: str | None
    created_at: datetime


class PrivacyConsentEvent(BaseModel):
    """One consent/legal-basis record for this subject."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    kind: str
    legal_basis: str
    created_at: datetime


class PrivacyExportBundle(BaseModel):
    """GET /internal/privacy/tenants/{tenant_id}/subjects/{wa_id}/export response.

    `patient=None` + every list empty is the EMPTY BUNDLE returned for a
    valid tenant with an unknown wa_id (never a 404 — see
    api/internal_privacy.py's module docstring). `analytics_events` is
    always `[]`: analytics events never carry a patient reference (see
    models/analytics_event.py) so there is nothing to include, by
    construction.
    """

    tenant_id: UUID
    wa_id: str
    patient: PrivacyPatient | None
    appointments: list[PrivacyAppointment]
    conversations: list[PrivacyConversation]
    messages: list[PrivacyMessage]
    consent_events: list[PrivacyConsentEvent]
    analytics_events: list[dict] = []


class PrivacyEraseResult(BaseModel):
    """DELETE /internal/privacy/tenants/{tenant_id}/subjects/{wa_id} response.

    `erased` counts rows actually removed/anonymized THIS call — a repeat
    call for an already-erased subject returns every count as 0 (idempotent).
    """

    erased: dict[str, int]
