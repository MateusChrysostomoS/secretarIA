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

from pydantic import BaseModel, Field

from secretaria.models.appointment import AppointmentStatus
from secretaria.models.message import MessageDirection, MessageSender


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
    # WhatsApp id (== the patient's phone in E.164-ish digits).
    #
    # OPTIONAL as of the Brain-Message channel, and this is a genuine bug fix,
    # not a widening for tidiness: a patient who reaches the clinic through the
    # web console has no phone number at all, so `wa_id` is NULL for them. While
    # this field was `str`, the FIRST such patient in a tenant would have made
    # `GET /internal/tenants/{id}/patients` raise a ValidationError and return
    # 500 — taking the whole doctor portal's patient list down for that clinic,
    # not just that one row. Flagged as the single hardest non-null assumption
    # in docs/CHECKPOINT_patient_channel_identity.md's inventory; this is where
    # it comes due, because this round is what first writes such a row.
    wa_id: str | None
    # WHICH surface this patient reaches the clinic on, and their id on it. The
    # portal needs both to know whether it can offer "message on WhatsApp".
    channel: str
    external_id: str | None
    created_at: datetime


class InternalPatientList(BaseModel):
    """`GET /internal/tenants/{tenant_id}/patients` response envelope."""

    data: list[InternalPatient]


# --------------------------------------------------------------------------
# Brain-Message channel (`/internal/brain-message/*`)
# --------------------------------------------------------------------------


class BrainMessageInbound(BaseModel):
    """One patient message arriving from the brain-api switchboard.

    `external_id` is the patient's identity ON this channel and the only handle
    secretaria has for them - there is no phone number. Its shape is the
    switchboard's business (a patient session id today); this service only has
    to store it, so it is validated for length against the column
    (`Patient.external_id`, VARCHAR(64)) and nothing else.
    """

    tenant_id: UUID
    external_id: str = Field(min_length=1, max_length=64)
    text: str | None = None
    patient_name: str | None = Field(default=None, max_length=255)
    # Optional idempotency key. Off by default: unlike Meta, the switchboard
    # calls once per patient action over an authenticated request. Supplying it
    # turns on the same `processed_events` claim the WhatsApp path uses.
    dedupe_id: str | None = Field(default=None, max_length=128)


class BrainMessageAck(BaseModel):
    """`POST /internal/brain-message/inbound` response.

    Deliberately carries no reply: the turn is processed on the arq worker, so
    by the time this is serialised the agent has not run. The caller polls the
    messages endpoint for what the bot said.
    """

    status: str


class BrainMessageMessage(BaseModel):
    """One message in a Brain-Message conversation, as the console renders it."""

    id: UUID
    direction: MessageDirection
    sender: MessageSender
    body: str | None
    created_at: datetime


class BrainMessageMessageList(BaseModel):
    """`GET /internal/brain-message/conversations/{external_id}/messages` response."""

    data: list[BrainMessageMessage]
