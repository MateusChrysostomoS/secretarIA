"""Request/response schemas for the calendar platform endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from secretaria.models.appointment import AppointmentStatus


class CalendarEventRead(BaseModel):
    """A Google Calendar event as returned by the agenda view."""

    id: str
    summary: str | None
    start: str
    end: str


class AppointmentCreate(BaseModel):
    """POST /appointments — create a consultation with patient linking."""

    start: datetime
    end: datetime
    summary: str = Field(min_length=1, max_length=500)
    description: str = ""
    # Patient phone (E.164 or local) — stored to notify on cancel/reschedule.
    phone: str | None = Field(default=None, max_length=32)
    # Patient DB id (optional — the hub may not know it).
    patient_id: str | None = None


class BlockCreate(BaseModel):
    """POST /blocks — block a time slot without patient notification."""

    start: datetime
    end: datetime
    summary: str = Field(default="Bloqueado", min_length=1, max_length=500)
    description: str = ""


class AppointmentCancel(BaseModel):
    """POST /appointments/{id}/cancel."""

    # Explicit confirmation guard — the frontend must send true.
    confirm: bool
    # Optional override for the notification message sent to the patient.
    custom_message: str | None = Field(default=None, max_length=4000)


class AppointmentReschedule(BaseModel):
    """POST /appointments/{id}/reschedule."""

    new_start: datetime
    new_end: datetime
    custom_message: str | None = Field(default=None, max_length=4000)


class AppointmentStatusUpdate(BaseModel):
    """PATCH /appointments/{id}/status."""

    status: AppointmentStatus


class AppointmentRead(BaseModel):
    """Appointment response."""

    id: str
    tenant_id: str
    patient_id: str | None
    google_event_id: str
    phone: str | None
    status: AppointmentStatus
    created_at: datetime
    updated_at: datetime
