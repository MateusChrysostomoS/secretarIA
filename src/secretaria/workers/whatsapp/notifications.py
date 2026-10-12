"""notifications - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime
from uuid import UUID

from arq import Retry

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    Patient,
    Tenant,
)
from secretaria.services import cancellation_notice
from secretaria.services.email import (
    send_cancellation_escalation_alert,
)
from secretaria.services.tenant_config import (
    get_waba_token,
)
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import (
    WhatsAppClient,
)
from secretaria.workers.shared.jobs import (
    _claim_event,
    _release_event,
)

logger = get_logger(__name__)


async def send_patient_notification(ctx: dict, tenant_id: str, phone: str, message: str) -> None:
    """arq job: send a text message to a patient on behalf of a specific tenant.

    Triggered by the doctor hub calendar endpoints (cancel / reschedule) when
    the doctor opts in to notifying the patient. The `phone` is the patient's
    WhatsApp ID (wa_id / E.164 digits only).
    """
    async with async_session_factory() as session:
        tenant = await session.get(Tenant, UUID(tenant_id))
        if tenant is None:
            logger.error("send_patient_notification_tenant_not_found", tenant_id=tenant_id)
            return
        # Decrypt inside the session (single seam); only the in-memory value travels.
        waba_token = await get_waba_token(session, tenant.id)

    try:
        await WhatsAppClient.for_tenant(tenant, waba_token).send_text_message(
            to=phone, body=message
        )
        logger.info(
            "send_patient_notification_sent",
            tenant_id=tenant_id,
            message_len=len(message),
        )
    except Exception as exc:
        logger.error(
            "send_patient_notification_failed",
            tenant_id=tenant_id,
            error=str(exc),
        )

# How hard `send_cancellation_notice` tries before handing the problem to a
# human. Documented and justified in `_cancellation_retry_decision`; kept here,
# next to the job, so the numbers are visible at the call site rather than
# buried in a helper.
CANCEL_NOTICE_MAX_TRIES = 4

CANCEL_NOTICE_RETRY_DEFER_S = 60

CANCEL_NOTICE_VALIDITY_S = 15 * 60

async def send_cancellation_notice(
    ctx: dict,
    tenant_id: str,
    appointment_id: str,
    professional_name: str | None,
    justification: str | None,
    extra_notice: str | None,
    allow_paid: bool,
) -> None:
    """arq job: tell a patient their doctor cancelled, and offer a way back.

    Split out from `send_patient_notification` rather than bolted onto it,
    because a cancellation is the one notice that must not be fire-and-forget
    text:

    * It carries REBOOKING BUTTONS, so it is an interactive send, not a text one.
    * Outside Meta's 24h window it is a BILLED template — and only with the
      doctor's explicit authorisation (`allow_paid`), collected in the hub with
      the price shown. Without it the job deliberately sends nothing and says
      so; the hub offers a free `wa.me` link instead.
    * It must never go out twice. A duplicate is not just inbox noise here —
      outside the window it is a second charge. Claimed through the same
      `processed_events` ledger the webhook pipeline uses, key
      `cancelnotice:<appointment_id>`.

    A claim whose send then FAILS is RELEASED and the job then raises
    `arq.Retry`, so this same job really does run again and can still reach
    the patient. Releasing WITHOUT raising — which is what this job used to do
    — hands the key back to a retry that never comes: arq considers a job that
    returns normally to be finished, so the patient was simply never told. See
    `_cancellation_retry_decision` for the attempt budget and the validity
    window, and `_escalate_cancellation_failure` for what happens when they
    run out.

    Never logs a phone number, the justification, or any patient content.
    """
    key = f"cancelnotice:{appointment_id}"

    async with async_session_factory() as session:
        tenant = await session.get(Tenant, UUID(tenant_id))
        if tenant is None:
            logger.error("cancellation_notice_tenant_not_found", tenant_id=tenant_id)
            return
        appointment = await session.get(Appointment, UUID(appointment_id))
        if appointment is None or appointment.tenant_id != tenant.id:
            # Tenant mismatch is the isolation guard, not a formality.
            logger.error(
                "cancellation_notice_appointment_not_found",
                tenant_id=tenant_id,
                appointment_id=appointment_id,
            )
            return

        to = None
        last_inbound = None
        if appointment.patient_id is not None:
            patient = await session.get(Patient, appointment.patient_id)
            if patient is not None:
                to = patient.wa_id
                last_inbound = await cancellation_notice.last_inbound_at(
                    session, tenant.id, patient.id
                )
        to = to or appointment.phone
        waba_token = await get_waba_token(session, tenant.id)

    if not to:
        logger.info(
            "cancellation_notice_skipped", reason="no_number", appointment_id=appointment_id
        )
        return

    inside = cancellation_notice.is_inside_window(last_inbound)
    if not inside and not allow_paid:
        # The doctor declined the paid send (or the client never offered it).
        # Saying nothing is correct here — but it must be VISIBLE, because the
        # patient has not been told.
        logger.warning(
            "cancellation_notice_not_sent",
            reason="outside_window_not_authorised",
            appointment_id=appointment_id,
            tenant_id=tenant_id,
        )
        return

    if not await _claim_event(key):
        logger.info(
            "cancellation_notice_skipped", reason="already_sent", appointment_id=appointment_id
        )
        return

    body = cancellation_notice.build_cancellation_text(professional_name, justification)
    if extra_notice:
        body = cancellation_notice.join_blocks(body, extra_notice)

    client = WhatsAppClient.for_tenant(tenant, waba_token)
    settings = get_settings()
    try:
        if inside:
            await client.send_buttons(
                to=to,
                body=cancellation_notice.join_blocks(
                    body, cancellation_notice.rebooking_invitation()
                ),
                buttons=cancellation_notice.rebook_buttons(appointment_id),
            )
        else:
            # Outside the window: only an approved template is accepted, and it
            # is billed. Its quick-reply buttons carry the same ids the flow
            # router routes on, so tapping one both reopens the window and
            # lands in the deterministic rebooking branch.
            await client.send_template(
                to=to,
                template=settings.CANCEL_TEMPLATE_NAME,
                lang=cancellation_notice.meta_language_code(tenant.language),
                variables=[body],
                button_payloads=cancellation_notice.rebook_payloads(appointment_id),
            )
    except Exception as exc:
        # The send did NOT happen, so the key was not consumed: give it back
        # BEFORE deciding what to do, because the retry below re-enters this
        # same job from the top and has to be able to claim it again.
        await _release_event(key)
        retry_in = _cancellation_retry_decision(ctx)
        logger.error(
            "cancellation_notice_failed",
            tenant_id=tenant_id,
            appointment_id=appointment_id,
            inside_window=inside,
            error=str(exc),
            retry_in_s=retry_in,
        )
        if retry_in is None:
            await _escalate_cancellation_failure(tenant, appointment_id, to)
            return
        # The ONLY thing arq re-runs a job for. A bare `raise` would be logged
        # as a permanent failure and the patient would never be told.
        raise Retry(defer=retry_in) from exc

    # Metering is deliberately OUTSIDE the try: it is fail-open by contract
    # (see `_emit_cancellation_usage`), and keeping it here makes it
    # structurally impossible for a metering hiccup to reach the retry path
    # above — which, after a send that already went out, would mean a SECOND
    # billed template for the same cancellation.
    if not inside:
        await _emit_cancellation_usage(appointment_id, tenant.id)

    logger.info(
        "cancellation_notice_sent",
        tenant_id=tenant_id,
        appointment_id=appointment_id,
        inside_window=inside,
    )

def _cancellation_retry_decision(ctx: dict) -> int | None:
    """Seconds to wait before re-running the notice, or None to give up.

    Two bounds, both deliberate:

    * ATTEMPTS — `CANCEL_NOTICE_MAX_TRIES` in total (this one included), so at
      most `CANCEL_NOTICE_MAX_TRIES - 1` retries. Kept at or under arq's own
      default `max_tries` (5, `arq.worker.Worker`) so the budget that runs out
      is THIS one, with an escalation, rather than arq's, which just logs
      "max retries exceeded" and drops the job silently. `workers/arq_worker.py`
      sets no `max_tries`, so raising that default here would be invisible.
    * VALIDITY — a cancellation notice is time-critical in a way most
      notifications are not: one that lands six hours late reaches a patient
      who has already left for a consultation that does not exist, and reads
      as a system that cannot be trusted. Past `CANCEL_NOTICE_VALIDITY_S` from
      the ORIGINAL enqueue (arq preserves `enqueue_time` across retries) the
      job stops trying and escalates to a human instead.

    With the constants below that is 4 attempts, ~60s apart, all inside a
    15-minute window: the blip cases (a Meta 5xx, a network hiccup) are
    covered, and the structural ones (a template Meta never approved) fail
    fast and loudly rather than retrying into the void.

    `ctx` is arq's job context. An empty dict — a direct call from a test or a
    script — reads as "first attempt, no deadline known" and gets a retry.
    """
    job_try = ctx.get("job_try") or 1
    if job_try >= CANCEL_NOTICE_MAX_TRIES:
        return None

    enqueued_at = ctx.get("enqueue_time")
    if isinstance(enqueued_at, datetime):
        if enqueued_at.tzinfo is None:
            enqueued_at = enqueued_at.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - enqueued_at).total_seconds()
        # The NEXT attempt has to land inside the window too — retrying at
        # 14m59s only to deliver at 16m helps nobody.
        if age + CANCEL_NOTICE_RETRY_DEFER_S >= CANCEL_NOTICE_VALIDITY_S:
            return None

    return CANCEL_NOTICE_RETRY_DEFER_S

async def _escalate_cancellation_failure(tenant, appointment_id: str, phone: str | None) -> None:
    """Last resort: every retry spent and the patient still has not been told.

    This is the one failure in this module that must leave the machine. A
    patient who does not know their consultation was cancelled will travel to
    it; nobody is watching a WARNING line closely enough to prevent that. So
    the clinic gets an email with the `wa.me` link the hub already offers, and
    can tell the patient in one tap.

    Fail-open: the escalation itself never raises: it is the end of the line,
    and a failed alert must not turn into an unhandled exception in the worker.
    Logs the alarm with a STABLE `alarm` field (`cancellation_notice_undelivered`)
    so an ops filter can key on it — and never the phone or the link.
    """
    logger.error(
        "cancellation_notice_abandoned",
        alarm="cancellation_notice_undelivered",
        tenant_id=str(tenant.id),
        appointment_id=appointment_id,
        notified_clinic=bool(tenant.contact_email),
    )
    if not tenant.contact_email:
        return
    try:
        await send_cancellation_escalation_alert(
            tenant.contact_email,
            tenant.clinic_name,
            cancellation_notice.whatsapp_deep_link(phone),
        )
    except Exception as exc:  # pragma: no cover - defensive, the alert is fail-open
        logger.warning(
            "cancellation_escalation_failed", appointment_id=appointment_id, error=str(exc)
        )

async def _emit_cancellation_usage(appointment_id: str, tenant_id) -> None:
    """Best-effort meter tick for one BILLED (outside-window) cancellation send.

    Fail-open, like `plugins/reminders.py::_emit_reminder_usage`: the message
    already went out, so a metering hiccup must not raise and trigger a retry
    that would send — and charge — a second time.
    """
    try:
        recorded = await emit_usage_event(
            tenant_id=str(tenant_id),
            feature="reminders",
            amount=1,
            event_id=f"cancelnotice:{appointment_id}",
        )
        if not recorded:
            logger.warning("usage_emit_failed", event_id=f"cancelnotice:{appointment_id}")
    except Exception as exc:
        logger.warning("usage_emit_failed", error=str(exc), appointment_id=appointment_id)
