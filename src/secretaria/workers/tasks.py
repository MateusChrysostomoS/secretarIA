"""arq job functions - all async webhook processing happens here.

This code runs OUTSIDE the HTTP request/response cycle, so it may safely do
database writes, handover logic and outbound Cloud API calls.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.ai.graph import run_agent  # noqa: F401  # TEMP: re-enable on _send_bot_reply
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    ProcessedEvent,
    Tenant,
)
from secretaria.schemas.webhook import WebhookPayload, WebhookValue
from secretaria.services.handover import HandoverManager
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)


@dataclass(frozen=True)
class _ReplyContext:
    """Minimal data needed to send a bot reply once the inbound DB txn commits."""

    conversation_id: UUID
    patient_wa_id: str
    inbound_body: str


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
                await _handle_patient_messages(value)
            elif field == "smb_message_echoes":
                # Coexistence: the human secretary replied from the app.
                await _handle_human_echoes(value)
            else:
                logger.info("worker_field_ignored", field=field)


# --------------------------------------------------------------------------
# Inbound patient messages
# --------------------------------------------------------------------------


async def _handle_patient_messages(value: WebhookValue) -> None:
    """Persist inbound patient messages and, if the bot is active, reply."""
    phone_number_id = value.metadata.phone_number_id if value.metadata else None
    contacts = {c.wa_id: c for c in value.contacts if c.wa_id}

    for msg in value.messages:
        if not msg.id or not msg.from_:
            logger.warning("worker_message_missing_fields", message_id=msg.id)
            continue

        contact = contacts.get(msg.from_)
        patient_name = contact.profile.name if contact and contact.profile else None
        body = msg.text.body if msg.text else None

        reply = await _persist_inbound_message(
            phone_number_id=phone_number_id,
            wa_id=msg.from_,
            patient_name=patient_name,
            wam_id=msg.id,
            body=body,
        )
        if reply is not None:
            await _send_bot_reply(reply)


async def _persist_inbound_message(
    *,
    phone_number_id: str | None,
    wa_id: str,
    patient_name: str | None,
    wam_id: str,
    body: str | None,
) -> _ReplyContext | None:
    """Record an inbound message in its own transaction.

    Returns a `_ReplyContext` when the bot should reply, or None when the
    message is a duplicate, the tenant is unknown, or a human is active.
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

                patient = await _get_or_create_patient(session, tenant, wa_id, patient_name)
                conversation = await _get_or_create_conversation(session, tenant, patient)

                session.add(
                    Message(
                        conversation_id=conversation.id,
                        direction=MessageDirection.INBOUND,
                        sender=MessageSender.PATIENT,
                        wam_id=wam_id,
                        body=body,
                    )
                )

                handover = HandoverManager(session)
                if not handover.is_bot_active(conversation):
                    # Human secretary is handling it - record only, stay quiet.
                    logger.info(
                        "worker_bot_paused_human_active",
                        conversation_id=str(conversation.id),
                    )
                    return None

                return _ReplyContext(
                    conversation_id=conversation.id,
                    patient_wa_id=wa_id,
                    inbound_body=body or "",
                )
        except IntegrityError:
            # A concurrent worker already claimed this event id.
            logger.info("worker_message_duplicate_race", wam_id=wam_id)
            return None


async def _send_bot_reply(reply: _ReplyContext) -> None:
    """Generate a reply, send it via the Cloud API, and record it."""
    # TEMP(connectivity-test): bypass LangGraph and send a fixed string so we can
    # validate the end-to-end webhook -> worker -> Cloud API path. Restore the
    # `run_agent(...)` call below once the smoke test is green.
    reply_text = "✅ SecretarIA online — teste de conexão com a Cloud API OK."
    # reply_text = await run_agent(
    #     reply.inbound_body,
    #     context={"conversation_id": str(reply.conversation_id)},
    # )

    try:
        result = await WhatsAppClient().send_text_message(to=reply.patient_wa_id, body=reply_text)
    except Exception as exc:
        # MVP: do not retry. A transient send failure means this one auto-reply
        # is lost; the human secretary still sees the patient's message.
        # TODO(reliability): add an outbox + retry for guaranteed delivery.
        logger.error(
            "worker_bot_reply_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )
        return

    async with async_session_factory() as session:
        async with session.begin():
            session.add(
                Message(
                    conversation_id=reply.conversation_id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    wam_id=_extract_sent_wam_id(result),
                    body=reply_text,
                )
            )
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is not None:
                conversation.last_bot_message_at = datetime.now(UTC)
    logger.info("worker_bot_reply_sent", conversation_id=str(reply.conversation_id))


# --------------------------------------------------------------------------
# Human echoes (Coexistence handover)
# --------------------------------------------------------------------------


async def _handle_human_echoes(value: WebhookValue) -> None:
    """Process `smb_message_echoes`: the human secretary replied from the app."""
    phone_number_id = value.metadata.phone_number_id if value.metadata else None
    # TODO(coexistence): confirm the exact smb_message_echoes payload shape
    #   against a real Coexistence webhook. The patient wa_id is taken from the
    #   echo `to` field, falling back to the contacts list.
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
            body=echo.text.body if echo.text else None,
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
                await HandoverManager(session).set_human_active(conversation)
        except IntegrityError:
            logger.info("worker_echo_duplicate_race", wam_id=wam_id)


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------


def _extract_sent_wam_id(send_response: dict) -> str | None:
    """Pull the wamid from a Cloud API send response, tolerating bad shapes."""
    try:
        return send_response["messages"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


async def _event_already_processed(session: AsyncSession, event_id: str) -> bool:
    """True when `event_id` is already in the `processed_events` ledger."""
    found = await session.scalar(
        select(ProcessedEvent.id).where(ProcessedEvent.event_id == event_id)
    )
    return found is not None


async def _resolve_tenant(session: AsyncSession, phone_number_id: str | None) -> Tenant | None:
    """Find the tenant for an inbound event.

    MVP single-tenant convenience: when no tenant row exists yet and the
    incoming phone_number_id matches the configured one, auto-provision it
    from environment settings so the system works end-to-end without a
    manual seed step.
    """
    if phone_number_id:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.phone_number_id == phone_number_id)
        )
        if tenant is not None:
            return tenant

    settings = get_settings()
    configured = settings.META_PHONE_NUMBER_ID
    if phone_number_id and configured and phone_number_id != configured:
        # Unknown number - never auto-provision a foreign tenant.
        return None

    target = phone_number_id or configured
    if not target:
        return None

    tenant = await session.scalar(select(Tenant).where(Tenant.phone_number_id == target))
    if tenant is not None:
        return tenant

    tenant = Tenant(
        clinic_name="MVP Clinic",
        phone_number_id=target,
        access_token=settings.META_ACCESS_TOKEN,
    )
    session.add(tenant)
    await session.flush()
    logger.info("worker_tenant_auto_provisioned", tenant_id=str(tenant.id))
    return tenant


async def _get_or_create_patient(
    session: AsyncSession,
    tenant: Tenant,
    wa_id: str,
    name: str | None,
) -> Patient:
    """Return the patient for (tenant, wa_id), creating it if needed."""
    patient = await session.scalar(
        select(Patient).where(
            Patient.tenant_id == tenant.id,
            Patient.wa_id == wa_id,
        )
    )
    if patient is None:
        patient = Patient(tenant_id=tenant.id, wa_id=wa_id, name=name)
        session.add(patient)
        await session.flush()
    elif name and not patient.name:
        patient.name = name
    return patient


async def _get_or_create_conversation(
    session: AsyncSession,
    tenant: Tenant,
    patient: Patient,
) -> Conversation:
    """Return the conversation for (tenant, patient), creating it if needed."""
    conversation = await session.scalar(
        select(Conversation).where(
            Conversation.tenant_id == tenant.id,
            Conversation.patient_id == patient.id,
        )
    )
    if conversation is None:
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
    return conversation
