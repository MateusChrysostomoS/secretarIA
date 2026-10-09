"""Request/response schemas for the calendar platform endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from secretaria.models.appointment import AppointmentStatus


class CalendarInsurancePlanRead(BaseModel):
    """The convênio plan an appointment resolved to, as the agenda shows it.

    Deliberately three fields. `id` is the ROW the booking pointed at: a
    `tenant_insurance_plans.id` (shared / clinic_with_exceptions modes) or, in
    `independent` mode, a `professional_insurance_plans.id`. `charge_deposit` is
    that row's own flag - a clinic policy for the plan, NOT proof that a Pix
    deposit was charged (that also needs the Pix add-on, a readable price and an
    Asaas key). The clinic's payment note and the catalog metadata stay out.
    """

    id: str
    name: str
    charge_deposit: bool


class CalendarDepositRead(BaseModel):
    """The Pix deposit (sinal) of an appointment, as the agenda shows it.

    Deliberately two fields, both read straight from `pix_deposits`:
    `status` is the VALUE of `models/pix_deposit.py::PixDepositStatus`
    ("aguardando_sinal", "confirmado_pago", "expirado", ...) and `amount_cents`
    the charge. It is the STATE OF THE MONEY, not of the appointment: today a
    paid deposit does not change `Appointment.status`, so nothing here may be
    read as "the consultation is confirmed". The Pix copy-paste payload, the
    Asaas payment id, the patient and the dates stay out. `status` is a plain
    string, not an enum, so a status added later never breaks this wire.
    """

    status: str
    amount_cents: int
    # Deliberately NOT here yet: spec F (2026-09-29, section 10A.5) adds a third,
    # additive key `needs_attention: bool` in TASK-017. Until then the wire is these
    # two keys, and a consumer must ignore any key it does not know.


class CalendarReminderRead(BaseModel):
    """One reminder of the appointment's CURRENT start, as the agenda shows it.

    Datetimes are always aware UTC on the wire (skill naive-timestamp-
    serialization). `kind`/`status`/`answer`/`warn_kind` are plain strings (the
    constants of models/appointment_reminder.py) so a value added later never
    breaks an older client. No message text, no phone, no error text: the only
    failure detail is a code kept server-side.
    """

    kind: str
    status: str
    due_at: datetime
    sent_at: datetime | None = None
    answered_at: datetime | None = None
    answer: str | None = None
    warned_at: datetime | None = None
    warn_kind: str | None = None


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
    # Convênio the patient chose ("Unimed", "Particular", or free text), trimmed;
    # None when the appointment has none or the event has no local Appointment.
    insurance: str | None = None
    # The plan row that label resolved to at booking time; None for "Particular",
    # a typed "Outro convênio", rows older than the catalog, a plan since removed,
    # and any id that does not belong to THIS clinic. Additive: every existing
    # consumer of this wire keeps working with the two new keys ignored.
    insurance_plan: CalendarInsurancePlanRead | None = None
    # The deposit row of this appointment (models/pix_deposit.py); None when it has
    # none: no local Appointment, a manual booking (the hub create route never
    # asks for a deposit), a clinic/plan without deposit, or a charge that could
    # not be created. The backend does not tell those apart and neither does the UI.
    deposit: CalendarDepositRead | None = None
    # TASK-032 (spec 4.4). Additive; all None for an event with no local
    # Appointment (a block, or something typed into Google). `status` is the
    # appointment status VALUE; `display_state` is one of "unconfirmed",
    # "confirmed", "confirmed_twice", "attention" (terminal appointments are
    # always "unconfirmed" - colour them by `status`); `attention` is
    # `display_state == "attention"`; `reminders` lists only the rows of the
    # appointment's current start, ordered by due time. A consumer must ignore
    # any key it does not know.
    status: AppointmentStatus | None = None
    confirmation_count: int | None = None
    display_state: str | None = None
    attention: bool | None = None
    reminders: list[CalendarReminderRead] | None = None


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
    # TASK-032: how many distinct confirmations (0..2) the booking has. Additive;
    # 0 for every row from before the counter existed.
    confirmation_count: int = 0
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


# The four answers of services/tenant_config.py::calendar_credential_health.
CalendarCredentialStatusWire = Literal["disconnected", "ok", "reconnect_required", "unavailable"]


class ProfessionalCalendarHealthRead(BaseModel):
    """One professional's OWN Google credential, checked live.

    Never "disconnected": only a professional that HAS an own token is checked.
    """

    professional_id: str
    status: Literal["ok", "reconnect_required", "unavailable"]


class CalendarHealthRead(BaseModel):
    """GET /tenants/me/calendar/health — do the stored Google credentials still work?

    `calendar_connected` (GET /tenants/me/config), `has_calendar` and
    `calendar_source` (GET /tenants/me/professionals) are PRESENCE flags: a token
    Google has expired or revoked is still stored, so they stay true while every
    booking fails. This is the live answer:

      - `ok` — Google accepted the credential and the calendar answered.
      - `reconnect_required` — Google rejected the refresh token itself
        (`invalid_grant`); only reconnecting the account fixes it.
      - `unavailable` — could not confirm right now (Google outage, network,
        timeout). NOT evidence of a broken connection.
      - `disconnected` — no token stored (clinic only).

    `professionals` lists only the ones checked (see calendar_credential_health
    for who that is). Categories only: no token, calendar id or Google message
    is ever serialised here.
    """

    clinic: CalendarCredentialStatusWire
    professionals: list[ProfessionalCalendarHealthRead]


class AppointmentRelease(BaseModel):
    """POST /appointments/{id}/release (TASK-032 R4, spec 4.4).

    Freeing the slot of an unconfirmed appointment. Every flag defaults to the
    cautious side, so a client that does not know a flag can never trigger the
    risky behavior by accident.
    """

    # The clinic has read the retention text for a PAID Pix deposit and agrees.
    acknowledge_retention: bool = False
    # The patient confirmed in the meantime; release anyway (the clinic's call).
    release_confirmed: bool = False
    # Authorises the BILLED template notice when the patient is outside Meta's
    # 24 h window (same meaning as AppointmentCancel.notify_outside_window).
    notify_outside_window: bool = False
    # Optional reason quoted in the patient notice; blank = the standard sentence.
    justification: str | None = Field(default=None, max_length=1000)


class AppointmentReleaseRead(AppointmentRead):
    """The released appointment, plus what happened to the patient notice.

    `patient_notice` is one of: whatsapp_queued, whatsapp_outside_window,
    portal_chat, portal_chat_email, no_channel, queue_unavailable, notice_failed.
    """

    patient_notice: str = "not_attempted"


class StaffMessageRequest(BaseModel):
    """POST /appointments/{id}/message (TASK-032 R4, spec 4.4)."""

    text: str = Field(min_length=1, max_length=1000)
    # Authorises the BILLED template when a WhatsApp patient is outside Meta's
    # 24 h window (same meaning as AppointmentCancel.notify_outside_window).
    notify_outside_window: bool = False

    @field_validator("text")
    @classmethod
    def _trimmed_and_not_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("text must not be blank")
        return value


class StaffMessageRead(BaseModel):
    """What happened to the staff message.

    `delivery`: whatsapp_text | whatsapp_template | portal_chat.
    `email_nudge`: only for portal_chat - sent | no_email | not_sent.
    """

    delivery: str
    email_nudge: str | None = None
    message_id: str | None = None
