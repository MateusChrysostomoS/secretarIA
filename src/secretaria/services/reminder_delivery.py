"""Deliver ONE appointment reminder on the patient's own channel (TASK-032 R2).

The engine (workers/reminder_engine.py) claims a row, builds a `ReminderJob`
and calls `deliver_reminder`; this module decides HOW, in one place (skill
channel-aware-dispatch: a Portal patient has `wa_id=None` by design and must
never reach the Graph API):

* WhatsApp inside Meta's 24h window: free interactive card with the three
  buttons (or plain text once the patient confirmed twice).
* WhatsApp outside the window: a Meta-approved template, billed. The v2
  template (6 variables + Confirmar/Cancelar/Outro) once
  `REMINDER_V2_TEMPLATE_APPROVED`, the Pix deposit template for a paid deposit,
  and the plain one-variable template otherwise - also as the fallback when Meta
  refuses either of the other two (precedent: plugins/reminders.py's deposit
  fallback). The fallback fires on ANY send error, so an ambiguous failure
  (timeout/5xx after Meta already accepted the template) can in rare cases
  produce a second reminder: at-least-once delivery, an accepted trade-off
  against losing the reminder.
* Portal: the reminder is written into the conversation (where it waits as
  the first thing the patient sees) and mailed with the clinic's link. The
  chat copy is written once: the engine finds out from the data whether the
  conversation already holds it (`ReminderJob.chat_written`), so a retry
  repeats the e-mail but never the card - and a first attempt that died
  before the card was written does not lose it.

Returns a `DeliveryOutcome`; never raises. The engine books the outcome
(status, retries, warnings, the WhatsApp history row, usage). Logs carry ids
and codes only - never a phone number, an address or the text.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import httpx

from secretaria.config import get_settings
from secretaria.core import database as core_database
from secretaria.core.logging import get_logger
from secretaria.models import Patient, Tenant
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_EMAIL,
    REMINDER_CHANNEL_WHATSAPP,
)
from secretaria.services import cancellation_notice
from secretaria.services.brain_patients import fetch_patient_email_result
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    BrainMessageSender,
    interactive_history_body,
)
from secretaria.services.email import EmailOutcome, send_transactional_email_result
from secretaria.services.reminder_text import (
    ReminderContent,
    build_reminder_body,
    button_payloads,
    deposit_reminder_buttons,
    local_start,
    portal_conversation_link,
    reminder_buttons,
    single_line_text,
    template_variables,
)
from secretaria.services.whatsapp import (
    TenantWhatsAppCredentialMissing,
    WhatsAppClient,
    interactive_buttons_record,
)

logger = get_logger(__name__)

EMAIL_TEMPLATE = "appointment_reminder_patient"
NO_LINK_LINE = "Entre no portal da clínica e abra a conversa.\n"


@dataclass(frozen=True)
class ReminderJob:
    """Everything one delivery needs, resolved by the engine inside its session."""

    reminder_id: UUID
    kind: str
    attempt: int
    tenant: Tenant
    patient: Patient
    appointment_id: UUID
    content: ReminderContent
    with_prompt: bool
    deposit_paid: bool
    conversation_id: UUID | None
    waba_token: str | None
    last_inbound_at: datetime | None
    now: datetime
    # Portal only: the conversation already holds this reminder's card (found
    # by the engine from the data, not inferred from the attempt number).
    chat_written: bool = False


@dataclass(frozen=True)
class DeliveryOutcome:
    """What happened. `permanent` failures are never retried."""

    ok: bool
    channel: str
    error_code: str | None = None
    permanent: bool = False
    billable: bool = False
    # WhatsApp only: the history copy the ENGINE writes as an outbound Message
    # (the Portal sender writes its own row - `persists_outbound`).
    history_body: str | None = None
    history_interactive: dict | None = None
    wam_id: str | None = None


def _error_code(exc: Exception) -> str:
    """A short code for `last_error_code` - never the exception text (may echo PII)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"http_{exc.response.status_code}"
    return type(exc).__name__[:64]


def _wam_id(response) -> str | None:
    try:
        return response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


def _buttons(job: ReminderJob) -> list[tuple[str, str]] | None:
    if not job.with_prompt:
        return None
    if job.deposit_paid:
        return deposit_reminder_buttons(job.reminder_id, job.appointment_id)
    return reminder_buttons(job.reminder_id)


def _whatsapp_ok(
    response, body: str, buttons, *, billable: bool, error_code=None
) -> DeliveryOutcome:
    if buttons:
        history = interactive_history_body(body, [label for _, label in buttons])
        interactive = interactive_buttons_record(body, buttons)
    else:
        history, interactive = body, None
    return DeliveryOutcome(
        ok=True,
        channel=REMINDER_CHANNEL_WHATSAPP,
        billable=billable,
        history_body=history,
        history_interactive=interactive,
        wam_id=_wam_id(response),
        error_code=error_code,
    )


async def deliver_reminder(job: ReminderJob) -> DeliveryOutcome:
    """Send one reminder. Never raises: every failure is an outcome."""
    try:
        if job.patient.channel == CHANNEL_BRAIN_MESSAGE:
            return await _deliver_portal(job)
        return await _deliver_whatsapp(job)
    except Exception as exc:  # defensive: a bug here must not strand the row
        logger.warning(
            "reminder_delivery_crashed",
            reminder_id=str(job.reminder_id),
            error_type=type(exc).__name__,
        )
        channel = (
            REMINDER_CHANNEL_EMAIL
            if job.patient.channel == CHANNEL_BRAIN_MESSAGE
            else REMINDER_CHANNEL_WHATSAPP
        )
        return DeliveryOutcome(ok=False, channel=channel, error_code=_error_code(exc))


async def _deliver_whatsapp(job: ReminderJob) -> DeliveryOutcome:
    to = (job.patient.wa_id or "").strip()
    if not to:
        return DeliveryOutcome(
            ok=False, channel=REMINDER_CHANNEL_WHATSAPP, error_code="no_channel", permanent=True
        )
    try:
        client = WhatsAppClient.for_tenant(job.tenant, job.waba_token)
    except TenantWhatsAppCredentialMissing:
        return DeliveryOutcome(
            ok=False,
            channel=REMINDER_CHANNEL_WHATSAPP,
            error_code="whatsapp_credential_missing",
            permanent=True,
        )
    buttons = _buttons(job)
    try:
        if cancellation_notice.is_inside_window(job.last_inbound_at, now=job.now):
            body = build_reminder_body(job.content)
            if buttons:
                response = await client.send_buttons(to, body, buttons)
            else:
                response = await client.send_text_message(to=to, body=body)
            return _whatsapp_ok(response, body, buttons, billable=False)
        return await _send_outside_window(client, job, to, buttons)
    except Exception as exc:
        return DeliveryOutcome(
            ok=False, channel=REMINDER_CHANNEL_WHATSAPP, error_code=_error_code(exc)
        )


async def _send_outside_window(client, job: ReminderJob, to: str, buttons) -> DeliveryOutcome:
    """Template send (billed). The plain template is both the default and the fallback
    (taken on any send error, so delivery is at-least-once)."""
    settings = get_settings()
    lang = cancellation_notice.meta_language_code(job.tenant.language)
    line = single_line_text(job.content)
    if buttons:
        template = None
        if job.deposit_paid:
            template, variables = settings.REMINDER_DEPOSIT_TEMPLATE_NAME, [line]
        elif settings.REMINDER_V2_TEMPLATE_APPROVED:
            template, variables = (
                settings.REMINDER_V2_TEMPLATE_NAME,
                template_variables(job.content),
            )
        if template is not None:
            try:
                response = await client.send_template(
                    to=to,
                    template=template,
                    lang=lang,
                    variables=variables,
                    button_payloads=button_payloads(buttons),
                )
                return _whatsapp_ok(response, line, buttons, billable=True)
            except Exception as exc:
                logger.warning(
                    "reminder_template_fallback",
                    reminder_id=str(job.reminder_id),
                    template=template,
                    error_type=type(exc).__name__,
                )
    response = await client.send_template(
        to=to, template=settings.REMINDER_TEMPLATE_NAME, lang=lang, variables=[line]
    )
    return _whatsapp_ok(
        response, line, None, billable=True, error_code="plain_template" if buttons else None
    )


async def _portal_email(job: ReminderJob) -> str | None:
    """brain-api is the identity authority; the stored copy only when it is down."""
    stored = (job.patient.email or "").strip() or None
    if not job.patient.external_id:
        return stored
    fetched = await fetch_patient_email_result(job.tenant.id, job.patient.external_id)
    return fetched.email if fetched.available else stored


async def _deliver_portal(job: ReminderJob) -> DeliveryOutcome:
    if job.conversation_id is None:
        return DeliveryOutcome(
            ok=False, channel=REMINDER_CHANNEL_EMAIL, error_code="no_conversation", permanent=True
        )
    body = build_reminder_body(job.content)
    if not job.chat_written:
        sender = BrainMessageSender(
            conversation_id=job.conversation_id,
            session_factory=core_database.async_session_factory,
        )
        buttons = _buttons(job)
        to = job.patient.external_id or ""
        try:
            if buttons:
                await sender.send_buttons(to, body, buttons)
            else:
                await sender.send_text_message(to, body)
        except Exception as exc:
            # Our own database refused the row: not something a retry fixes
            # without a human, and retrying would risk a second chat copy.
            return DeliveryOutcome(
                ok=False,
                channel=REMINDER_CHANNEL_EMAIL,
                error_code=f"chat_{_error_code(exc)}"[:64],
                permanent=True,
            )
    email = await _portal_email(job)
    if not email:
        return DeliveryOutcome(
            ok=False, channel=REMINDER_CHANNEL_EMAIL, error_code="no_email", permanent=True
        )
    link = portal_conversation_link(job.tenant.id)
    variables = {
        "clinic_name": job.tenant.clinic_name,
        "when": local_start(job.content).strftime("%d/%m/%Y às %H:%M"),
        "reminder_text": body,
        "link_line": f"{link}\n" if link else NO_LINK_LINE,
    }
    outcome = await send_transactional_email_result(
        to=email, template=EMAIL_TEMPLATE, variables=variables
    )
    if outcome is EmailOutcome.SENT:
        return DeliveryOutcome(ok=True, channel=REMINDER_CHANNEL_EMAIL)
    return DeliveryOutcome(
        ok=False,
        channel=REMINDER_CHANNEL_EMAIL,
        error_code=f"email_{outcome.value}",
        permanent=not outcome.is_transient,
    )
