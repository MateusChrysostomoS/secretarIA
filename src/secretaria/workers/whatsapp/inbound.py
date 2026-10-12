"""inbound - split out of workers/tasks.py (TASK-023)."""

from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger, wa_suffix
from secretaria.models import (
    ConsentEvent,
    Patient,
    ProcessedEvent,
)
from secretaria.schemas.webhook import (
    WebhookPayload,
    WebhookStatus,
    WebhookValue,
    extract_action_button,
    extract_greeting_button,
    extract_inbound_body,
    extract_inbound_reply_id,
)
from secretaria.services.channel_sender import (
    CHANNEL_WHATSAPP,
)
from secretaria.services.message_status import apply_whatsapp_statuses
from secretaria.workers.orchestrator import (
    _send_bot_reply,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.db import (
    _event_already_processed,
)
from secretaria.workers.shared.text import (
    is_remove_context_command,
)
from secretaria.workers.turn_router import (
    _route_inbound_turn,
)
from secretaria.workers.whatsapp.coexistence import (
    _handle_history,
    _handle_human_echoes,
    _handle_smb_app_state_sync,
)
from secretaria.workers.whatsapp.db import (
    _resolve_tenant,
)
from secretaria.workers.whatsapp.rate_limit import (
    _is_rate_limited,
)
from secretaria.workers.whatsapp.remove_context import (
    _handle_remove_context_command,
)

logger = get_logger(__name__)


async def process_webhook_event(ctx: dict, payload: dict) -> None:
    """arq job: route a WhatsApp webhook event to the right handler.

    Registered as the `process_webhook_event` job and enqueued by the webhook
    POST handler.

    Error handling: a malformed payload is swallowed (it would never succeed
    on retry). Processing errors are allowed to propagate so arq can retry the
    job - the idempotency claims (`processed_events`) make retries safe.
    """
    try:
        event = WebhookPayload.model_validate(payload)
    except Exception as exc:
        logger.error("worker_payload_invalid", error=str(exc))
        return

    for entry in event.entry:
        for change in entry.changes:
            value = change.value
            if value is None:
                continue
            field = change.field or ""
            if field == "messages":
                await _handle_patient_messages(value, redis=ctx.get("redis"))
                # Delivery receipts ride the same field: a status-only event has
                # `messages` empty and `statuses` populated. Dropped here, before,
                # which is why no WhatsApp message ever showed past "enviado".
                if value.statuses:
                    await _handle_message_statuses(value, redis=ctx.get("redis"))
            elif field == "smb_message_echoes":
                # Coexistence: the human secretary replied from the app.
                await _handle_human_echoes(value)
            elif field == "history":
                # Coexistence: chat-history sync (progress-only, no content).
                await _handle_history(value)
            elif field == "smb_app_state_sync":
                # Coexistence: business contact list sync (counts only).
                await _handle_smb_app_state_sync(value)
            else:
                logger.info("worker_field_ignored", field=field)

# A receipt can overtake the row it is about: the outbound row is written only AFTER
# the Graph API answers (`_record_outbound`), in its own transaction. Receipts that
# match nothing get ONE deferred second look; what still matches nothing then is a
# message this service did not record (sent from elsewhere on the same number).
_STATUS_RETRY_DELAY = timedelta(seconds=30)

async def _apply_statuses(phone_number_id: str, statuses: list[WebhookStatus]):
    async with async_session_factory() as session:
        async with session.begin():
            return await apply_whatsapp_statuses(
                session, phone_number_id=phone_number_id, statuses=statuses
            )

async def _handle_message_statuses(value: WebhookValue, redis=None) -> None:
    """Write Meta's delivered / read / failed receipts onto their messages.

    A DB error propagates, so arq retries the whole event - safe, because every write
    is conditional (services/message_status.py) and inbound messages are claimed in
    `processed_events`.
    """
    phone_number_id = value.metadata.phone_number_id if value.metadata else None
    if not phone_number_id:
        # Without the receiving number there is no tenant to scope the match to.
        logger.warning("worker_statuses_without_phone_number_id", count=len(value.statuses))
        return
    result = await _apply_statuses(phone_number_id, value.statuses)
    if not result.unmatched:
        return
    if redis is None:
        logger.info("whatsapp_status_unmatched", count=len(result.unmatched), retried=False)
        return
    await redis.enqueue_job(
        "process_message_statuses",
        phone_number_id,
        [status.model_dump(exclude_none=True) for status in result.unmatched],
        _defer_by=_STATUS_RETRY_DELAY,
    )

async def process_message_statuses(ctx: dict, phone_number_id: str, statuses: list[dict]) -> None:
    """arq job: the one deferred second look at receipts that matched no message.

    Enqueued only by `_handle_message_statuses`, with receipts already reduced to
    `WebhookStatus` fields (no recipient). Never re-enqueues itself.
    """
    parsed = [WebhookStatus.model_validate(status) for status in statuses]
    result = await _apply_statuses(phone_number_id, parsed)
    if result.unmatched:
        logger.info("whatsapp_status_unmatched", count=len(result.unmatched), retried=True)

async def _handle_patient_messages(value: WebhookValue, redis=None) -> None:
    """Persist inbound patient messages and, if the bot is active, reply.

    `redis` is the arq Redis pool (ctx["redis"]); when present it backs the
    per-sender inbound rate limit. None disables rate limiting (e.g. tests).
    """
    phone_number_id = value.metadata.phone_number_id if value.metadata else None
    contacts = {c.wa_id: c for c in value.contacts if c.wa_id}

    for msg in value.messages:
        if not msg.id or not msg.from_:
            logger.warning("worker_message_missing_fields", message_id=msg.id)
            continue

        # Audio is handled by the dedicated transcribe_audio_message job (enqueued
        # by the webhook with a minimal payload). Skip it here so the body=None
        # path below doesn't claim the ProcessedEvent id first — that would make
        # the transcript look like a duplicate and get dropped. The rate limit is
        # also skipped so the audio job's own check is the single increment.
        # Audio without a media id falls through to today's quiet body=None path.
        if msg.type == "audio" and msg.audio is not None and msg.audio.id:
            continue

        # Flood protection: silently drop once a sender exceeds the window cap.
        # Silence is intentional - replying would reward the spammer and still
        # cost an outbound send.
        if await _is_rate_limited(redis, phone_number_id, msg.from_):
            logger.info("worker_rate_limited", wa_id_suffix=wa_suffix(msg.from_))
            continue

        contact = contacts.get(msg.from_)
        patient_name = contact.profile.name if contact and contact.profile else None
        # A tap arrives as two strings and stays two: the title the patient saw
        # (`body`, the only text stored and shown to staff) and the id of the
        # row/button tapped, which rides separately so its payload can route
        # without ever landing in a bubble (see `_route_inbound_turn`).
        body = extract_inbound_body(msg)
        interactive_reply_id = extract_inbound_reply_id(msg)
        action_button = extract_action_button(msg)
        greeting_button = extract_greeting_button(msg)

        # The ONE command that still bypasses the normal turn: the explicit,
        # exact-match destructive reset (PROMPT_FIX_18). `/menu` and its
        # aliases deliberately do NOT short-circuit here any more — they flow
        # through `_persist_inbound_message` like every other message, so the
        # allowlist / tenant-active / handover / entitlement gates all apply
        # to them, and the menu itself is rendered by `_handle_show_main_menu`.
        if is_remove_context_command(body):
            await _handle_remove_context_command(
                phone_number_id=phone_number_id,
                wa_id=msg.from_,
                patient_name=patient_name,
                wam_id=msg.id,
                redis=redis,
            )
            continue

        reply = await _persist_inbound_message(
            phone_number_id=phone_number_id,
            wa_id=msg.from_,
            patient_name=patient_name,
            wam_id=msg.id,
            body=body,
            interactive_reply_id=interactive_reply_id,
            action_button=action_button,
            greeting_button=greeting_button,
        )
        if reply is not None:
            await _send_bot_reply(reply, redis=redis)

async def _persist_inbound_message(
    *,
    phone_number_id: str | None,
    wa_id: str,
    patient_name: str | None,
    wam_id: str,
    body: str | None,
    action_button: tuple[str, str] | None = None,
    greeting_button: str | None = None,
    interactive_reply_id: str | None = None,
) -> _ReplyContext | None:
    """Record an inbound message in its own transaction.

    Returns a `_ReplyContext` when the bot should reply, or None when the
    message is a duplicate, the tenant is unknown, or a human is active.

    `action_button` (decoded by schemas/webhook.py::extract_action_button) is
    captured and returned as-is on the `_ReplyContext`, BEFORE the
    human-handover check below: a tap on a reminder's Confirmar/Reagendar/
    Cancelar button is a structured, unambiguous command tied to one
    appointment id, not a free-form conversational turn, so it is handled
    (by `_handle_action_button`, dispatched from `_send_bot_reply`) even
    while a human has taken the conversation over.

    `greeting_button` (decoded by schemas/webhook.py::extract_greeting_button)
    is checked AFTER the handover check (unlike `action_button` above - a
    greeting-button tap is not time/money-sensitive, so it respects an active
    human takeover like a normal message would): when set AND this tenant has
    flows disabled AND it isn't the "outro" LLM-escape suffix, short-circuits
    to `greeting_button_unavailable` on the returned context instead of
    falling through to the normal dispatch below - see that field's docstring
    on `_ReplyContext` and the inline comment at the check.
    """
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if await _event_already_processed(session, wam_id):
                    logger.info("worker_message_duplicate", wam_id=wam_id)
                    return None
                session.add(ProcessedEvent(event_id=wam_id))

                tenant = await _resolve_tenant(session, phone_number_id)
                if tenant is None:
                    logger.error("worker_tenant_unresolved", phone_number_id=phone_number_id)
                    return None

                # Coexistence test-window allowlist (config.py::bot_allowlist_wa_ids):
                # an empty allowlist means no restriction (production default,
                # untouched below). When non-empty, silently drop anyone not on
                # it - no reply at all, not even service_unavailable, and no
                # Patient/Conversation/ConsentEvent. The ProcessedEvent row added
                # above is intentionally kept: this event WAS seen, it is being
                # discarded on purpose, not lost to a retry.
                #
                # Checked BEFORE the tenant-active gate below (PROMPT_FIX_21):
                # the allowlist is the hard boundary of the Coexistence test
                # window, so it must not be possible to get ANY outbound
                # message - not even the "em configuração" fallback - by
                # talking to a tenant that happens to be inactive.
                allowlist = get_settings().bot_allowlist_wa_ids
                if allowlist:
                    digits_wa_id = "".join(filter(str.isdigit, wa_id))
                    if digits_wa_id not in allowlist:
                        logger.info(
                            "worker_wa_id_not_allowlisted",
                            wa_id_suffix=wa_suffix(digits_wa_id),
                            tenant_id=str(tenant.id),
                        )
                        return None

                # Bot not activated for this tenant (Calendar/types/hours not
                # set up): send a single polite fallback and do NOT create a
                # conversation, persist the message, or invoke the LLM. The
                # tenant id rides along so the fallback still goes out on THIS
                # tenant's own WhatsApp credentials (there is no conversation
                # to resolve it from) - see `_handle_service_unavailable`.
                if not tenant.is_active:
                    logger.info("worker_bot_not_active", tenant_id=str(tenant.id))
                    return _ReplyContext(
                        conversation_id=None,
                        tenant_id=tenant.id,
                        patient_ref=wa_id,
                        inbound_body="",
                        service_unavailable=True,
                    )

                # A patient row that already existed means this person has
                # contacted the clinic before (robust to /menu wiping history).
                patient = await session.scalar(
                    select(Patient).where(
                        Patient.tenant_id == tenant.id,
                        Patient.wa_id == wa_id,
                    )
                )
                is_returning_patient = patient is not None
                if patient is None:
                    patient = Patient(tenant_id=tenant.id, wa_id=wa_id, name=patient_name)
                    session.add(patient)
                    await session.flush()
                    # Consent groundwork (LGPD): exactly ONE event per new
                    # patient row, never per message. See models/consent_event.py.
                    session.add(
                        ConsentEvent(
                            tenant_id=tenant.id,
                            wa_id=wa_id,
                            kind="first_contact_service",
                            legal_basis=(
                                "TODO_LAWYER: execução de contrato vs consentimento — "
                                "pendencias_advogado.md item pendente"
                            ),
                        )
                    )
                elif patient_name and not patient.name:
                    patient.name = patient_name

                return await _route_inbound_turn(
                    session,
                    tenant=tenant,
                    patient=patient,
                    # For WhatsApp the two are the same string, by construction
                    # (models/patient.py: external_id mirrors wa_id). Passing
                    # `wa_id` rather than `patient.external_id` keeps this call
                    # correct even for a row written by the not-yet-redeployed
                    # worker, whose external_id is NULL.
                    patient_ref=wa_id,
                    channel=CHANNEL_WHATSAPP,
                    is_returning_patient=is_returning_patient,
                    body=body,
                    inbound_wam_id=wam_id,
                    action_button=action_button,
                    greeting_button=greeting_button,
                    interactive_reply_id=interactive_reply_id,
                )
        except IntegrityError:
            # A concurrent worker already claimed this event id.
            logger.info("worker_message_duplicate_race", wam_id=wam_id)
            return None
