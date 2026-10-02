"""handover - split out of workers/tasks.py (TASK-023)."""

from types import SimpleNamespace
from uuid import UUID

from sqlalchemy import select

from secretaria.ai.formatter import (
    TextBubble,
)
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    Patient,
    ProcessedEvent,
    Professional,
    Tenant,
)
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.email import (
    send_calendar_alert,
    send_professional_config_incomplete_alert,
)
from secretaria.services.flow_router import (
    SCOPED_HELP_ESCALATE_MESSAGE,
    FlowRouterResult,
)
from secretaria.services.handoff_notification import activate_human_handoff, notify_human_handoff
from secretaria.services.handover import HandoverManager
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.dispatch import (
    _dispatch_bubbles,
)
from secretaria.workers.shared.sender import (
    _reply_sender,
    _send_simple_text,
)
from secretaria.workers.shared.text import (
    CALENDAR_UNAVAILABLE_MESSAGE,
)

logger = get_logger(__name__)


async def _handle_human_handoff(
    reply: _ReplyContext,
    reason: str,
    tenant: Tenant | None,
    flow_snapshot: tuple[SimpleNamespace, SimpleNamespace] | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """Commit human ownership, notify staff, then confirm the handoff to the patient.

    Same order as `_handle_calendar_unavailable` (if a later step fails, a human
    is already on it). The mail is best-effort: `notify_human_handoff` never
    raises and logs its own alarm.
    """
    await _set_conversation_human_active(reply.conversation_id, reason=reason)
    await _dispatch_bubbles(
        reply, [TextBubble(body=SCOPED_HELP_ESCALATE_MESSAGE)], tenant=tenant, waba_token=waba_token
    )

async def _set_conversation_human_active(
    conversation_id: UUID | None, *, reason: str = "could_not_help"
) -> bool:
    """Commit human ownership before notifying. State failure is explicit to callers."""
    if conversation_id is None:
        raise RuntimeError("handoff_state_not_committed")
    try:
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None:
                    raise RuntimeError("handoff_conversation_missing")
                occurrence_id = await activate_human_handoff(
                    session, conversation, manager=HandoverManager(session)
                )
                if occurrence_id is None:
                    occurrence_id = await session.scalar(
                        select(ProcessedEvent.id).where(
                            ProcessedEvent.event_id == f"humanhandoff-active:{conversation_id}"
                        )
                    )
                tenant = await session.get(Tenant, conversation.tenant_id)
                professional_id = conversation.flow_selected_professional_id
    except Exception as exc:
        logger.error(
            "worker_handoff_state_failed", error_type=type(exc).__name__,
            conversation_id=str(conversation_id),
        )
        raise RuntimeError("handoff_state_not_committed") from None
    if occurrence_id is not None and tenant is not None:
        try:
            await notify_human_handoff(
                tenant=tenant, conversation_id=conversation_id,
                professional_id=professional_id, reason=reason,
                occurrence_id=occurrence_id,
            )
        except Exception as exc:
            logger.error(
                "human_handoff_notification_undelivered",
                alarm="human_handoff_notification_undelivered",
                conversation_id=str(conversation_id), error_type=type(exc).__name__,
            )
    return True

async def _handle_calendar_unavailable(
    reply: _ReplyContext,
    redis=None,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
) -> None:
    """Degrade gracefully on a calendar outage: notify patient + hand off + alert tenant.

    `tenant`/`waba_token` (already loaded by the caller, if any) select the
    per-tenant WhatsApp client for the patient-facing message below. The
    clinic-owner calendar alert re-fetches its own tenant row after the
    handover commits, independent of what the caller passed in.
    """
    logger.error("worker_calendar_unavailable", conversation_id=str(reply.conversation_id))
    await _set_conversation_human_active(reply.conversation_id)
    async with async_session_factory() as session:
        alert_tenant = await session.scalar(
            select(Tenant).join(Conversation, Conversation.tenant_id == Tenant.id)
            .where(Conversation.id == reply.conversation_id)
        )

    # Fail closed (PROMPT_FIX_21): the handover above already happened, so a
    # human still sees the conversation even when the patient-facing notice
    # can't be sent on this tenant's own credentials.
    client = _reply_sender(reply, tenant, waba_token)
    if client is not None:
        await _send_simple_text(reply.patient_ref, CALENDAR_UNAVAILABLE_MESSAGE, client=client)

    # Alert the clinic owner by email (at most once every CALENDAR_ALERT_SILENCE_SECONDS).
    if alert_tenant is not None and alert_tenant.contact_email:
        settings = get_settings()
        alert_key = f"calendar:alert:{alert_tenant.id}"
        should_send = True
        if redis is not None:
            try:
                already_sent = await redis.exists(alert_key)
                if already_sent:
                    should_send = False
                else:
                    await redis.setex(alert_key, settings.CALENDAR_ALERT_SILENCE_SECONDS, "1")
            except Exception as exc:
                logger.warning("worker_calendar_alert_redis_failed", error=str(exc))
        if should_send:
            await send_calendar_alert(alert_tenant.contact_email, alert_tenant.clinic_name)

async def _handle_professional_config_incomplete(
    reply: _ReplyContext,
    result: FlowRouterResult,
    redis=None,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
) -> None:
    """A patient reached a doctor nobody can book: tell them, then alert the humans.

    Deliberately shaped after `_handle_calendar_unavailable` above — patient
    message first, then a debounced owner email — with three differences that
    are the whole point of this handler:

      * NO handover. A calendar outage breaks the entire clinic and a human
        secretary can take over; a doctor with no configured hours breaks only
        that doctor, and no human in the chat can invent a schedule. What is
        needed is a config change, which is what the email asks for.
      * TWO recipients: the clinic (`tenants.contact_email`) and the doctor
        themselves, each only when KNOWN. The doctor's address is asked of
        brain-api per alert (services/brain_professionals.py), never stored
        here: brain-api is the single writer of identity, so `users.email` is
        the only copy, and a local column would keep mailing the old address
        the day a doctor changed it. Either half may be missing — a clinic that
        never set `contact_email`, a doctor with no linked user — and neither
        being present is a no-op, never an error: the patient has already been
        answered, so there is nothing left to fail.
      * Its OWN Redis key and silence window. Sharing
        `calendar:alert:{tenant}` would let a Google outage mute a config gap
        for four hours; the key is scoped per tenant AND professional AND gap
        so one broken doctor never silences the alert for another, and a
        missing-hours alert never suppresses a missing-services one.

    The patient's name and number are read here and passed to the email BODY.
    They are never logged: the `logger.info` below carries ids and the gap
    category only, matching the rule in services/email.py.
    """
    gap = result.professional_config_gap or "unknown"
    logger.info(
        "worker_professional_config_incomplete",
        conversation_id=str(reply.conversation_id),
        professional_id=str(result.flow_selected_professional_id),
        gap=gap,
    )

    # Tell the patient first, on this tenant's own credentials (fail-closed:
    # `_dispatch_bubbles` sends nothing rather than fall back to a global
    # scaffold). The alert below still runs if this send fails — the clinic
    # needs to know either way.
    if result.bubbles:
        await _dispatch_bubbles(reply, result.bubbles, tenant=tenant, waba_token=waba_token)

    if reply.conversation_id is None or result.flow_selected_professional_id is None:
        return

    # One short read-only txn for everything the email needs. Re-fetched here
    # (rather than trusting what the caller passed) for the same reason
    # `_handle_calendar_unavailable` re-fetches its tenant: `contact_email` may
    # have changed since this turn started.
    alert_tenant: Tenant | None = None
    professional: Professional | None = None
    patient_name: str | None = None
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is None:
                return
            alert_tenant = await session.get(Tenant, conversation.tenant_id)
            professional = await session.get(Professional, result.flow_selected_professional_id)
            patient = await session.get(Patient, conversation.patient_id)
            patient_name = patient.name if patient is not None else None
    except Exception as exc:
        logger.error(
            "worker_professional_config_alert_load_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )
        return

    if alert_tenant is None or professional is None:
        return
    # A professional row from ANOTHER tenant could only arrive through a bug,
    # but mailing one clinic about another's doctor is not a mistake worth
    # risking - the check is one comparison.
    if professional.tenant_id != alert_tenant.id:
        return

    # The doctor's own address, asked AFTER the session above closes — a
    # network call has no business holding a DB connection open. Exactly the
    # three-way read plugins/professional_notification.py does:
    #   None       -> brain-api could not answer. "We do not know" is not
    #                 "nobody", so it earns a warning; the clinic still hears
    #                 about it, which is the half that can act on the gap.
    #   key absent -> this doctor has no linked user, so there is no address to
    #                 reach them at. Silent and honest, not a failure.
    professional_email: str | None = None
    emails = await fetch_professional_emails(alert_tenant.id)
    if emails is None:
        logger.warning(
            "worker_professional_config_alert_lookup_failed",
            tenant_id=str(alert_tenant.id),
            professional_id=str(professional.id),
            gap=gap,
        )
    else:
        professional_email = emails.get(str(professional.id))

    # Both addresses, deduped, each only when actually known. Nobody to write
    # to is a normal outcome (no clinic address, and a doctor brain-api cannot
    # place), not a failure: the patient has already been answered.
    recipients: list[str] = []
    for candidate in (alert_tenant.contact_email, professional_email):
        address = (candidate or "").strip()
        if address and address not in recipients:
            recipients.append(address)
    if not recipients:
        logger.info(
            "worker_professional_config_alert_no_recipient",
            tenant_id=str(alert_tenant.id),
            professional_id=str(professional.id),
            gap=gap,
        )
        return

    # Debounce AFTER resolving recipients, so a clinic with no address on file
    # does not burn a four-hour silence window on an email nobody received.
    settings = get_settings()
    alert_key = f"professional_config:alert:{alert_tenant.id}:{professional.id}:{gap}"
    should_send = True
    if redis is not None:
        try:
            already_sent = await redis.exists(alert_key)
            if already_sent:
                should_send = False
            else:
                await redis.setex(
                    alert_key, settings.PROFESSIONAL_CONFIG_ALERT_SILENCE_SECONDS, "1"
                )
        except Exception as exc:
            logger.warning("worker_professional_config_alert_redis_failed", error=str(exc))
    if not should_send:
        return

    for address in recipients:
        await send_professional_config_incomplete_alert(
            address,
            alert_tenant.clinic_name,
            professional.name,
            gap,
            patient_name=patient_name,
            patient_phone=reply.patient_ref,
        )
