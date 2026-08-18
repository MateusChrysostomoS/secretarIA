"""Request/response schemas for the calendar platform endpoints."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from secretaria.models.appointment import AppointmentStatus


class CalendarEventRead(BaseModel):
    """A Google Calendar event as returned by the agenda view.

    `id` is GOOGLE's event id. It is NOT accepted by the write endpoints
    (cancel / reschedule), which key on the local `Appointment.id` — that
    mismatch is exactly why the agenda's cancel button sat disabled: the read
    model handed the UI an id no write endpoint would take.

    `appointment_id` closes it. `None` means "this event has no local
    Appointment row" — a block, or something the doctor typed straight into
    Google Calendar. The UI must keep the write actions disabled for those
    rather than guessing, because a wrong id here would cancel the wrong
    consultation and the Google deletion is irreversible.

    Typed `str` rather than `UUID` to match `AppointmentRead.id`, which is what
    the frontend sends straight back in the cancel/reschedule URL path. Both
    serialise identically over JSON; the difference would only ever be a trap.
    """

    id: str
    summary: str | None
    start: str
    end: str
    appointment_id: str | None = None


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
    """POST /appointments/{id}/cancel.

    `custom_message` is GONE, replaced by `justification` — deliberately a
    replacement and not a second field beside it, because the two would have
    read as synonyms while meaning opposite things: the old one WAS the whole
    body the patient received, the new one is a fragment quoted inside a
    standard sentence the server composes. Two fields, one of which silently
    suppresses the other's wording, is the kind of ambiguity that ships a
    cancellation saying the wrong thing.

    Safe to drop outright rather than deprecate: the only client is the hub
    agenda, whose "Cancelar consulta" button had never been enabled (its modal
    was never mounted), so nothing in production ever sent this field.
    """

    # Explicit confirmation guard — the frontend must send true.
    confirm: bool
    # The doctor's REASON, quoted into the standard notice. None/blank simply
    # omits the justification line; the patient is notified either way.
    justification: str | None = Field(default=None, max_length=4000)
    # Authorises the PAID template send when the patient is outside Meta's 24h
    # window. Defaults False so the expensive path is never taken by accident:
    # a client that does not know about the cost cannot incur it. Ignored
    # inside the window, where the notice is free.
    notify_outside_window: bool = False


class CancelPreviewRead(BaseModel):
    """GET /appointments/{id}/cancel-preview — what cancelling would cost.

    Read BEFORE the doctor confirms, so the modal can offer the §3.1 choice
    with real numbers instead of asking them to guess. Purely informational:
    it mutates nothing and sends nothing.
    """

    # False = Meta will not accept free-form text; notifying costs a template.
    inside_window: bool
    # Name rendered into "O médico {name} desmarcou a sua consulta!".
    professional_name: str | None
    # Empty string when unconfigured — the UI must then avoid quoting a price.
    template_cost_brl: str
    cost_is_estimate: bool
    # `https://wa.me/...` so the doctor can write from their own phone for free.
    whatsapp_link: str | None


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
    conversation_id: str | None = None
    google_event_id: str
    google_event_link: str | None = None
    appointment_type: str | None = None
    start_at: datetime | None = None
    end_at: datetime | None = None
    phone: str | None
    status: AppointmentStatus
    created_at: datetime
    updated_at: datetime
    # The PixDeposit status VALUE for this appointment (e.g. "confirmado_pago"),
    # or None when there is no deposit at all — see models/pix_deposit.py's
    # PixDepositStatus. Read-only: never set via a request body.
    deposit_status: str | None = None
    # The one-time deposit_lifecycle outcome ("voided"/"refunded"/
    # "partial_refund"/"retained"/"refund_failed") of THIS request, populated
    # only by POST /cancel and PATCH /status (CANCELLED/NO_SHOW) — every other
    # endpoint (including a plain GET-shaped re-read) leaves it None. Not a
    # persistent appointment attribute like `deposit_status` above; it exists
    # purely so the hub can show what just happened to the money.
    deposit_outcome: str | None = None
