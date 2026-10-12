"""coexistence - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError

from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Message,
    MessageDirection,
    MessageSender,
    ProcessedEvent,
    Tenant,
)
from secretaria.schemas.webhook import (
    WebhookValue,
    extract_echo_body,
    history_item_is_final,
)
from secretaria.services.handoff_notification import activate_human_handoff, notify_human_handoff
from secretaria.services.handover import HandoverManager
from secretaria.workers.shared.db import (
    _event_already_processed,
    _get_or_create_conversation,
)
from secretaria.workers.whatsapp.db import (
    _get_or_create_patient,
    _resolve_tenant,
)

logger = get_logger(__name__)


async def _handle_human_echoes(value: WebhookValue) -> None:
    """Process `smb_message_echoes`: the human secretary replied from the app.

    Confirmed against the official Coexistence API documentation:
    `smb_message_echoes` fires ONLY for messages sent from the WhatsApp
    Business app / linked device, never for our own Cloud API sends, so no
    bot-echo-loop guard is needed here. The patient wa_id comes from the
    echo's `to` field, falling back to the contacts list.
    """
    phone_number_id = value.metadata.phone_number_id if value.metadata else None
    fallback_wa_id = next((c.wa_id for c in value.contacts if c.wa_id), None)

    for echo in value.message_echoes:
        patient_wa_id = echo.to or fallback_wa_id
        if not echo.id or not patient_wa_id:
            logger.warning("worker_echo_missing_fields", echo_id=echo.id)
            continue
        await _persist_human_echo(
            phone_number_id=phone_number_id,
            patient_wa_id=patient_wa_id,
            wam_id=echo.id,
            body=extract_echo_body(echo),
        )

async def _persist_human_echo(
    *,
    phone_number_id: str | None,
    patient_wa_id: str,
    wam_id: str,
    body: str | None,
) -> None:
    """Record a human echo and switch the conversation to HUMAN_ACTIVE."""
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if await _event_already_processed(session, wam_id):
                    logger.info("worker_echo_duplicate", wam_id=wam_id)
                    return
                session.add(ProcessedEvent(event_id=wam_id))

                tenant = await _resolve_tenant(session, phone_number_id)
                if tenant is None:
                    logger.error("worker_tenant_unresolved", phone_number_id=phone_number_id)
                    return

                # smb_message_echoes is one of the three signals that Coexistence
                # mode resolved for this tenant (contract v1 §10) - see also
                # _handle_history / _handle_smb_app_state_sync below. This runs
                # BEFORE the allowlist guard below on purpose: the echo is still
                # proof that Coexistence resolved for this tenant even when the
                # conversation itself gets discarded for being off-allowlist, so
                # `mode_resolved_at` must be set either way. `session.begin()`
                # commits on a clean exit from the `async with` block, including
                # the early `return` below - the tenant mutation is not lost.
                _mark_mode_resolved(tenant)

                # Coexistence test-window allowlist (config.py::bot_allowlist_wa_ids):
                # same rule as `_persist_inbound_message` - an empty allowlist
                # means no restriction. When non-empty, drop echoes for a patient
                # not on it: no Patient/Conversation, no Message. The
                # ProcessedEvent added above stays (event seen, discarded on
                # purpose).
                allowlist = get_settings().bot_allowlist_wa_ids
                if allowlist:
                    digits_patient_wa_id = "".join(filter(str.isdigit, patient_wa_id))
                    if digits_patient_wa_id not in allowlist:
                        logger.info(
                            "worker_wa_id_not_allowlisted",
                            wa_id_suffix=digits_patient_wa_id[-4:],
                            tenant_id=str(tenant.id),
                        )
                        return

                patient = await _get_or_create_patient(session, tenant, patient_wa_id, None)
                conversation = await _get_or_create_conversation(session, tenant, patient)

                session.add(
                    Message(
                        conversation_id=conversation.id,
                        direction=MessageDirection.OUTBOUND,
                        sender=MessageSender.HUMAN,
                        wam_id=wam_id,
                        body=body,
                    )
                )
                occurrence_id = await activate_human_handoff(
                    session, conversation, manager=HandoverManager(session)
                )
            if occurrence_id is not None:
                await notify_human_handoff(
                    tenant=tenant, conversation_id=conversation.id,
                    professional_id=conversation.flow_selected_professional_id,
                    reason="could_not_help", occurrence_id=occurrence_id,
                )
        except IntegrityError:
            logger.info("worker_echo_duplicate_race", wam_id=wam_id)

def _mark_mode_resolved(tenant: Tenant) -> None:
    """Set `mode_resolved_at=now` the first time ANY Coexistence signal arrives.

    Shared by `_persist_human_echo` (smb_message_echoes), `_handle_history`
    and `_handle_smb_app_state_sync`. A no-op once already set - Coexistence
    mode resolution happens once per tenant, not on every subsequent event.
    """
    if tenant.mode_resolved_at is None:
        tenant.mode_resolved_at = datetime.now(UTC)

async def _handle_history(value: WebhookValue) -> None:
    """Process the `history` field: WhatsApp Coexistence chat-history sync.

    Meta streams the business's WhatsApp history in one or more chunks after
    Coexistence connects. We NEVER ingest message content - only track sync
    progress on the tenant row: `history_sync_status` flips to "in_progress"
    on the first chunk ever observed, and to "done" (+ `history_synced_at`)
    once any chunk signals completion (`history_item_is_final` - handles the
    documented shape defensively, including an unknown/renamed variant).
    """
    phone_number_id = value.metadata.phone_number_id if value.metadata else None
    chunk_count = len(value.history)
    if chunk_count == 0:
        logger.info("worker_history_empty_payload")
        return

    is_final = any(history_item_is_final(item) for item in value.history)

    async with async_session_factory() as session:
        async with session.begin():
            tenant = await _resolve_tenant(session, phone_number_id)
            if tenant is None:
                logger.error("worker_history_tenant_unresolved")
                return
            tenant_id = tenant.id

            if tenant.history_sync_status == "none":
                tenant.history_sync_status = "in_progress"
            if is_final:
                tenant.history_sync_status = "done"
                tenant.history_synced_at = datetime.now(UTC)
            _mark_mode_resolved(tenant)

    logger.info(
        "worker_history_processed",
        tenant_id=str(tenant_id),
        chunks=chunk_count,
        final=is_final,
    )

async def _handle_smb_app_state_sync(value: WebhookValue) -> None:
    """Process `smb_app_state_sync`: business contact list sync (Coexistence).

    Contact names/phone numbers ride in this payload but are NEVER read,
    persisted, or logged (see schemas/webhook.py's `WebhookStateSyncItem`) -
    this only records that Coexistence mode resolved for the tenant. Logs a
    COUNT only.
    """
    phone_number_id = value.metadata.phone_number_id if value.metadata else None
    sync_count = len(value.state_sync)

    async with async_session_factory() as session:
        async with session.begin():
            tenant = await _resolve_tenant(session, phone_number_id)
            if tenant is None:
                logger.error("worker_state_sync_tenant_unresolved")
                return
            tenant_id = tenant.id
            _mark_mode_resolved(tenant)

    logger.info("worker_state_sync_processed", tenant_id=str(tenant_id), count=sync_count)
