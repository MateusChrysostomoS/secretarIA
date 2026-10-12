"""remove_context - split out of workers/tasks.py (TASK-023)."""

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger, wa_suffix
from secretaria.models import (
    AnalyticsEvent,
    Appointment,
    Conversation,
    Message,
    Patient,
    PixDeposit,
    ProcessedEvent,
)
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.greeting_template import (
    render_greeting,
)
from secretaria.services.tenant_config import (
    get_waba_token,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.db import (
    _event_already_processed,
    _get_or_create_conversation,
)
from secretaria.workers.shared.dispatch import (
    _send_consent_notice,
    _send_greeting,
)
from secretaria.workers.shared.greeting import (
    _fit_clinic_description,
)
from secretaria.workers.shared.sender import (
    _send_simple_text,
    _tenant_client,
)
from secretaria.workers.whatsapp.db import (
    _get_or_create_patient,
    _resolve_tenant,
)

logger = get_logger(__name__)


# Sent before the fresh greeting when the reset had to leave bookings behind
# (see `_handle_remove_context_command`). Only ever sent when the count is
# non-zero, so a clean slate still looks exactly like a brand-new first contact.
REMOVE_CONTEXT_PRESERVED_MESSAGE = (
    "Contexto removido. {count} agendamento(s) foram PRESERVADOS: eles continuam "
    "na agenda do Google, então não foram apagados aqui para os dois não "
    "divergirem. Use o menu para remarcar ou cancelar."
)

async def _handle_remove_context_command(
    *,
    phone_number_id: str | None,
    wa_id: str,
    patient_name: str | None,
    wam_id: str,
    redis=None,
) -> None:
    """`/dangerously-remove-context`: wipe this number's conversational trail.

    Deletes the patient row for this number, their conversation(s) and every
    message, then recreates an empty patient + conversation and replays the
    whole first-contact OPENING - the product frame with no buttons, then the
    LGPD notice with `✅ Concordo` - so the number is treated as a brand-new
    first contact. Both messages, not just the frame: the recreated patient's
    `lgpd_accepted_at` is NULL, so a real newcomer would be owed exactly this
    pair (see `_persist_inbound_message`'s consent gate), and a rehearsal that
    stops at the frame is not a rehearsal. This is the old `/menu` "dev reset",
    unchanged in intent and renamed to a string nobody types by accident
    (PROMPT_FIX_18) - `/menu` itself is now non-destructive.

    WHAT IT DELIBERATELY DOES **NOT** DELETE (the orphan fix):

      * **Appointments.** `appointments.google_event_id` is NOT NULL, so every
        appointment row mirrors a live Google Calendar event. Deleting the row
        would leave the database saying "no consultation" while Google still
        shows one and the doctor shows up for a slot the system forgot - the
        two silently diverging, which is exactly the failure this must not
        cause. So appointments are PRESERVED and DETACHED
        (`patient_id`/`conversation_id` set to NULL, explicitly rather than via
        FK cascade so Postgres and the SQLite test engine agree). `phone`,
        `status`, `start_at`/`end_at` and `google_event_id` are untouched: the
        booking stays a complete, actionable clinic record, and Google is never
        called from a chat command. The count is reported back to the sender.
      * **Pix deposits.** `pix_deposits.appointment_id` is ON DELETE CASCADE,
        so deleting an appointment would take a PAID deposit's record with it,
        leaving money with no owner. Because the appointment survives, so does
        the deposit; only its `patient_id` pointer is cleared. Refund/retention
        stays governed by the deposit lifecycle, never by this command.
      * **Consent events.** The LGPD ledger is append-only here.

    True erasure remains the job of the authorized privacy process
    (`api/internal_privacy.py::erase_subject`), never a chat command.

    GATES: rate limit (in `_handle_patient_messages`), tenant resolution,
    allowlist and - for the outbound only - entitlement, i.e. the same policy
    as any other turn. `tenant.is_active` is deliberately NOT required: it is a
    product-readiness flag ("has this clinic finished setup"), not a security
    boundary, and this command exists precisely to reset test context on a
    tenant that is still being set up. Everything it can reach is scoped to the
    resolved tenant, and to the sender's own data within it.

    Every invocation writes a durable, sanitized audit row (`analytics_events`,
    `event_type="context_removed"`): tenant, conversation, per-type counts and
    a timestamp - never the phone number and never any deleted content.
    """
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if await _event_already_processed(session, wam_id):
                    logger.info("worker_remove_context_duplicate", wam_id=wam_id)
                    return
                session.add(ProcessedEvent(event_id=wam_id))

                tenant = await _resolve_tenant(session, phone_number_id)
                if tenant is None:
                    logger.error(
                        "worker_remove_context_tenant_unresolved",
                        phone_number_id=phone_number_id,
                    )
                    return

                # Same allowlist boundary as a normal turn
                # (`_persist_inbound_message`): during the restricted
                # Coexistence window an off-allowlist number must not be able
                # to make the platform do ANYTHING - including recreating a
                # Patient/Conversation and sending it a greeting. The
                # ProcessedEvent claimed above is kept on purpose: the event
                # WAS seen, and is being discarded deliberately.
                allowlist = get_settings().bot_allowlist_wa_ids
                if allowlist:
                    digits_wa_id = "".join(filter(str.isdigit, wa_id))
                    if digits_wa_id not in allowlist:
                        logger.info(
                            "worker_wa_id_not_allowlisted",
                            wa_id_suffix=wa_suffix(digits_wa_id),
                            tenant_id=str(tenant.id),
                        )
                        return

                # Wipe the conversational trail. Explicit ordered statements
                # (not DB cascade) so this behaves identically on Postgres and
                # the SQLite test engine. We select only the id, so no stale
                # ORM object lingers in the identity map after the delete.
                counts = {
                    "patients": 0,
                    "conversations": 0,
                    "messages": 0,
                    "appointments_preserved": 0,
                    "deposits_preserved": 0,
                }
                removed_conversation_id = None
                existing_id = await session.scalar(
                    select(Patient.id).where(
                        Patient.tenant_id == tenant.id,
                        Patient.wa_id == wa_id,
                    )
                )
                if existing_id is not None:
                    conv_ids = (
                        await session.scalars(
                            select(Conversation.id).where(Conversation.patient_id == existing_id)
                        )
                    ).all()
                    # The conversation being DESTROYED - the id every earlier
                    # log line for this thread carries, so the audit trail
                    # joins up. The replacement's id is recorded separately.
                    removed_conversation_id = conv_ids[0] if conv_ids else None

                    # Bookings + money: DETACH, never delete (see docstring).
                    deposits = await session.execute(
                        update(PixDeposit)
                        .where(
                            PixDeposit.tenant_id == tenant.id,
                            PixDeposit.patient_id == existing_id,
                        )
                        .values(patient_id=None)
                    )
                    counts["deposits_preserved"] = deposits.rowcount or 0
                    appointments = await session.execute(
                        update(Appointment)
                        .where(
                            Appointment.tenant_id == tenant.id,
                            Appointment.patient_id == existing_id,
                        )
                        .values(patient_id=None, conversation_id=None)
                    )
                    counts["appointments_preserved"] = appointments.rowcount or 0
                    if conv_ids:
                        # Bookings made from a conversation this patient no
                        # longer owns (already detached by an earlier run):
                        # clear the dangling conversation pointer too, so the
                        # DELETE below can never orphan a live booking.
                        await session.execute(
                            update(Appointment)
                            .where(
                                Appointment.tenant_id == tenant.id,
                                Appointment.conversation_id.in_(conv_ids),
                            )
                            .values(conversation_id=None)
                        )
                        messages = await session.execute(
                            delete(Message).where(Message.conversation_id.in_(conv_ids))
                        )
                        counts["messages"] = messages.rowcount or 0
                        conversations = await session.execute(
                            delete(Conversation).where(Conversation.id.in_(conv_ids))
                        )
                        counts["conversations"] = conversations.rowcount or 0
                    await session.execute(delete(Patient).where(Patient.id == existing_id))
                    counts["patients"] = 1
                    await session.flush()

                # Recreate a clean patient + conversation. A fresh conversation
                # already defaults to BOT_ACTIVE + flow IDLE, so there is no
                # prior state left to reset.
                patient = await _get_or_create_patient(session, tenant, wa_id, patient_name)
                conversation = await _get_or_create_conversation(session, tenant, patient)
                conversation_id = conversation.id
                preserved = counts["appointments_preserved"]

                # Durable, sanitized audit record - this is what makes keeping a
                # destructive chat command defensible. Internal ids and counts
                # only: no wa_id, no phone, no deleted content. `created_at` is
                # the table's server-side timestamp. `analytics_events` is
                # queried strictly by `event_type` (api/hub/analytics.py reads
                # only "appointment_booked"), so this row is invisible to the
                # doctor hub, and the LGPD export never touches this table.
                audit_payload = {
                    "conversation_id": (
                        str(removed_conversation_id) if removed_conversation_id else None
                    ),
                    "replacement_conversation_id": str(conversation_id),
                    **counts,
                }
                session.add(
                    AnalyticsEvent(
                        tenant_id=tenant.id,
                        event_type="context_removed",
                        payload=audit_payload,
                    )
                )

                # Send the first-contact greeting (the "initial" one), NOT the
                # returning greeting - the whole point of deleting the patient.
                # Since the greeting-frame round this is the rendered product
                # frame, so it is ALWAYS non-empty: the "no greeting configured"
                # degrade below is now unreachable for a tenant with a name, and
                # this command is once again a faithful rehearsal of what a
                # brand-new patient sees.
                greeting = render_greeting(tenant.clinic_name, _fit_clinic_description(tenant))
                waba_token = await get_waba_token(session, tenant.id)
        except IntegrityError:
            logger.info("worker_remove_context_duplicate_race", wam_id=wam_id)
            return

    logger.warning("conversation_context_removed", tenant_id=str(tenant.id), **audit_payload)

    # Same entitlement policy as every other outbound: the wipe itself is the
    # operator acting on their own tenant's data (already committed and
    # audited above), but an unentitled tenant sends NOTHING - a send costs
    # money and the gate is server-side, never negotiable per surface.
    summary = await get_entitlements(tenant.id, redis)
    if summary is None or not (summary.active and summary.secretaria_enabled):
        logger.warning(
            "bot_reply_suppressed_unentitled",
            tenant_id=str(tenant.id),
            status=summary.status if summary is not None else None,
        )
        return

    client = _tenant_client(tenant, waba_token)
    if client is None:
        # Fail closed: the local wipe already committed (and is audited), but
        # nothing goes out on someone else's WhatsApp number.
        return

    if preserved:
        # Say what was left behind, so the operator never mistakes a partial
        # reset for a clean slate and gets surprised by a live Google event.
        await _send_simple_text(
            wa_id,
            REMOVE_CONTEXT_PRESERVED_MESSAGE.format(count=preserved),
            client=client,
        )

    if not greeting:
        # No first-contact greeting configured: the slate is clean and the next
        # patient message will get the LLM's improvised opener. Nothing to send.
        logger.info("worker_remove_context_no_greeting", conversation_id=str(conversation_id))
        return

    # Replay the opening EXACTLY as `_persist_inbound_message`'s consent gate
    # produces it for a real newcomer: the frame BUTTON-FREE, then the LGPD
    # notice carrying `✅ Concordo`. The recreated patient row has a NULL
    # `lgpd_accepted_at`, so the gate genuinely owes this pair - and an
    # operator resetting their own number is precisely the person who would
    # mistake this command's output for what a newcomer sees. Sending only the
    # frame, decorated with the [Agendar] trio, diverged in the two ways that
    # are visible from the phone: buttons the gate would refuse on the very
    # next tap, and no terms at all until the operator typed something else -
    # because the greeting this command sends is itself recorded as a Message,
    # so the next inbound no longer reads as `is_first_contact` and falls to
    # the gate's re-prompt branch instead of its first-contact branch.
    reply = _ReplyContext(
        conversation_id=conversation_id,
        tenant_id=tenant.id,
        patient_ref=wa_id,
        inbound_body="",
        greeting_override=greeting,
        greeting_buttons=[],
    )
    await _send_greeting(reply, tenant=tenant, waba_token=waba_token)
    await _send_consent_notice(reply, tenant=tenant, waba_token=waba_token)
