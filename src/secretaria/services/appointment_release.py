"""Telling a patient the clinic freed their slot (TASK-032 R4, spec 4.4).

Two channels, two mechanisms, one rule: the release itself has already been
committed and must never be undone by a notice that cannot be delivered. This
module therefore never raises; it returns a short code the hub passes to the
front (`AppointmentReleaseRead.patient_notice`) so the clinic knows whether the
patient was told, and can write to them by hand when not.

* **WhatsApp** - reuses `workers/whatsapp/notifications.py::send_cancellation_notice`
  through arq, exactly like the hub's cancel endpoint: rebooking buttons inside
  the 24 h window, the billed template outside it (only with the clinic's
  `allow_paid`), idempotent through `cancelnotice:<appointment_id>`, retried and
  escalated by e-mail when every attempt fails. Nothing of that is re-implemented.
* **Portal (Brain-Message)** - the job is WhatsApp-only (it addresses `wa_id`/
  `phone`, and a Portal appointment's `phone` can hold the portal id), so the
  notice is a plain chat message written in the caller's session plus the generic
  e-mail nudge. No buttons yet: the Portal does not decode the `rebook*|` ids
  (R3 adds that); the text tells the patient to just write back.

Never logs a phone number, an e-mail address or the message text.
"""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, MessageSender, Patient, Tenant
from secretaria.services import cancellation_notice
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.staff_patient_message import (
    EMAIL_SENT,
    conversation_id_for,
    nudge_portal_patient,
)

logger = get_logger(__name__)

# Quoted by `build_cancellation_text` as the doctor's justification when the
# clinic typed none: it reads "Justificativa do médico: "a consulta não foi ...".
RELEASE_JUSTIFICATION = "a consulta não foi confirmada a tempo e o horário foi liberado."
PORTAL_REBOOK_LINE = "Para marcar um novo horário, é só me escrever por aqui."

NOTICE_WHATSAPP_QUEUED = "whatsapp_queued"
NOTICE_WHATSAPP_OUTSIDE_WINDOW = "whatsapp_outside_window"
NOTICE_PORTAL_CHAT = "portal_chat"
NOTICE_PORTAL_CHAT_EMAIL = "portal_chat_email"
NOTICE_NO_CHANNEL = "no_channel"
NOTICE_QUEUE_UNAVAILABLE = "queue_unavailable"
NOTICE_FAILED = "notice_failed"


async def notify_released_patient(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient | None,
    *,
    professional_name: str | None,
    justification: str | None,
    deposit_notice: str | None,
    allow_paid: bool,
    arq_pool,
    now: datetime | None = None,
) -> str:
    """Tell the patient; returns one `NOTICE_*` code. Never raises."""
    # Read once, up front: a rollback inside the Portal path expires the ORM rows,
    # and the error logging below must not trigger a lazy load.
    appointment_id = appointment.id
    try:
        reason = (justification or "").strip() or RELEASE_JUSTIFICATION
        if patient is None or patient.tenant_id != tenant.id:
            return NOTICE_NO_CHANNEL
        if patient.channel == CHANNEL_BRAIN_MESSAGE:
            return await _notify_portal(
                session, tenant, appointment, patient, professional_name, reason, deposit_notice
            )

        to = patient.wa_id or appointment.phone
        if not to:
            return NOTICE_NO_CHANNEL
        last_inbound = await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
        if not cancellation_notice.is_inside_window(last_inbound, now=now) and not allow_paid:
            logger.info("release_notice_not_queued", reason="outside_window_not_authorised")
            return NOTICE_WHATSAPP_OUTSIDE_WINDOW
        if arq_pool is None:
            logger.warning("release_notice_not_queued", reason="no_queue")
            return NOTICE_QUEUE_UNAVAILABLE
        try:
            await arq_pool.enqueue_job(
                "send_cancellation_notice",
                str(tenant.id),
                str(appointment_id),
                professional_name,
                reason,
                deposit_notice,
                allow_paid,
            )
        except Exception as exc:
            logger.error(
                "release_notice_enqueue_failed",
                appointment_id=str(appointment_id),
                error_type=type(exc).__name__,
            )
            return NOTICE_QUEUE_UNAVAILABLE
        return NOTICE_WHATSAPP_QUEUED
    except Exception as exc:
        logger.error(
            "release_notice_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return NOTICE_FAILED


async def _notify_portal(
    session: AsyncSession,
    tenant: Tenant,
    appointment: Appointment,
    patient: Patient,
    professional_name: str | None,
    reason: str,
    deposit_notice: str | None,
) -> str:
    appointment_id = appointment.id  # before a rollback can expire the row
    conversation_id = await conversation_id_for(session, tenant.id, appointment, patient)
    if conversation_id is None:
        return NOTICE_NO_CHANNEL
    text = cancellation_notice.join_blocks(
        cancellation_notice.build_cancellation_text(professional_name, reason),
        deposit_notice,
        PORTAL_REBOOK_LINE,
    )
    try:
        sender = BrainMessageSender(
            conversation_id=conversation_id, session=session, author=MessageSender.HUMAN
        )
        await sender.send_text_message(to=patient.external_id or "", body=text)
        await session.commit()
    except Exception as exc:
        await session.rollback()
        logger.error(
            "release_portal_notice_failed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return NOTICE_FAILED
    nudge = await nudge_portal_patient(tenant, patient)
    return NOTICE_PORTAL_CHAT_EMAIL if nudge == EMAIL_SENT else NOTICE_PORTAL_CHAT
