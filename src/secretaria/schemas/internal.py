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

from pydantic import BaseModel, ConfigDict, Field

from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_REPLY_ID_CHARS
from secretaria.models.appointment import AppointmentStatus
from secretaria.models.message import MessageDirection, MessageSender, MessageStatus
from secretaria.schemas.conversation import AttachmentRead, InteractiveRead, MessagesReadMark


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
    # The id of the button / list row the patient tapped in the portal, when
    # `text` is that control's title rather than something typed. UNTRUSTED:
    # unlike a WhatsApp tap it was not signed by Meta - it comes from the
    # patient's own browser through the switchboard - so the worker only
    # honours it after finding it among the options its own recent cards
    # offered on this conversation (workers/tasks.py::
    # _validated_brain_message_reply_id); anything else is routed as plain
    # text. Bounded by the longest id a card can carry.
    interactive_reply_id: str | None = Field(
        default=None, min_length=1, max_length=MAX_INTERACTIVE_REPLY_ID_CHARS
    )
    # Optional idempotency key. Off by default: unlike Meta, the switchboard
    # calls once per patient action over an authenticated request. Supplying it
    # turns on the same `processed_events` claim the WhatsApp path uses.
    dedupe_id: str | None = Field(default=None, max_length=128)


class BrainMessageInboundForm(BrainMessageInbound):
    """The text fields of `POST /internal/brain-message/inbound` when it carries a FILE.

    The JSON body's fields, sent as multipart form fields next to the part `file`
    (brain-api/docs/CHECKPOINT_brain_message_anexos.md §4.1); `text` becomes an
    optional caption. Strict, unlike the JSON model: this encoding is new, so refusing
    an unknown field costs no existing caller and turns contract drift into a clear
    422 instead of a silently dropped value (`frozen-contract-migration`).
    """

    model_config = ConfigDict(extra="forbid")

    # No idempotency key on this encoding: brain-api's contract (§4.1) never sends one
    # with a file, and a replayed key would make the worker drop a job whose file is
    # already stored - bytes in the bucket with nothing pointing at them. Sending one
    # is a 422 on `dedupe_id`, never a silent drop.
    dedupe_id: None = None


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
    # Same two fields, same shape, as the staff console's `MessageRead`
    # (schemas/conversation.py): the card an outbound row went out as, and on
    # an inbound row the id of the option the patient tapped. Both optional
    # and None by default, so a caller built against the text-only wire keeps
    # working (`frozen-contract-migration`: additive, never required).
    interactive: InteractiveRead | None = None
    interactive_reply_id: str | None = None
    # The file this message carries: what it IS, never where it is stored - no object
    # key and no URL, here or in any other field (brain-api rewrites exactly this key
    # for the patient and passes every other field through). Additive and optional.
    attachment: AttachmentRead | None = None
    # Delivery state, same derivation and meaning as the staff console's `MessageRead`
    # (schemas/conversation.py). On this channel a row is "entregue" from birth; the
    # clinic's rows turn "lido" when the patient's read mark reaches them, the
    # patient's when the staff's does. `updated_at` is what `since` compares against:
    # a row comes back on the next poll when its status changes. Additive, defaulted.
    status: MessageStatus = "enviado"
    delivered_at: datetime | None = None
    read_at: datetime | None = None
    updated_at: datetime | None = None


class BrainMessageReadMark(MessagesReadMark):
    """`POST /internal/brain-message/messages/read` - the PATIENT has seen their
    conversation up to a cursor, so the clinic's messages up to it become read.

    Same scope keys as every other Brain-Message route (tenant + external_id) plus
    exactly one cursor; strict, like the base (`extra="forbid"`)."""

    tenant_id: UUID
    external_id: str = Field(min_length=1, max_length=64)


class BrainMessageMessageList(BaseModel):
    """`GET /internal/brain-message/conversations/{external_id}/messages` response."""

    data: list[BrainMessageMessage]
