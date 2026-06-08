"""arq job functions - all async webhook processing happens here.

This code runs OUTSIDE the HTTP request/response cycle, so it may safely do
database writes, handover logic and outbound Cloud API calls.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.ai.formatter import (
    BUTTON_ID_CANCEL,
    BUTTON_ID_CONFIRM,
    ButtonBubble,
    SlotsBubble,
    TextBubble,
    parse,
)
from secretaria.ai.graph import run_agent
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    ProcessedEvent,
    Tenant,
)
from secretaria.services.tenant_config import load_tenant_config
from secretaria.services.whatsapp import WhatsAppClient
from secretaria.schemas.webhook import (
    WebhookPayload,
    WebhookValue,
    extract_inbound_body,
)
from secretaria.services.handover import HandoverManager
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

# Slash commands the patient can type to reset the conversation. Matched
# case-insensitively against the trimmed message body.
_MENU_COMMANDS = frozenset({"/menu", "/reset", "/recomecar", "/recomeçar", "/inicio", "/início"})

# Static menu shown after a /menu command. Three reply buttons fit the WhatsApp
# cap (max 3) and the labels double as the body the LLM will see on the next
# inbound — clicking "Marcar consulta" sends the literal string "Marcar
# consulta" to the agent, which knows how to handle it.
_MENU_BODY = (
    "Bem-vindo(a) à Eye Company 👁️\n"
    "Cuidado oftalmológico personalizado com o Dr. Mateus Chrysóstomo.\n\n"
    "Como posso te ajudar?"
)
_MENU_BUTTONS = [
    ("menu|book", "Marcar consulta"),
    ("menu|cancel", "Cancelar consulta"),
    ("menu|human", "Falar com humano"),
]


def is_menu_command(body: str | None) -> bool:
    """True when the patient typed a `/menu`-style reset command."""
    if not body:
        return False
    return body.strip().lower() in _MENU_COMMANDS


@dataclass(frozen=True)
class _ReplyContext:
    """Minimal data needed to send a bot reply once the inbound DB txn commits."""

    conversation_id: UUID
    patient_wa_id: str
    inbound_body: str
    # When set, this is the tenant's verbatim first-contact greeting: it is sent
    # as a single message and the LLM is NOT invoked for this turn.
    greeting_override: str | None = None
    # Optional quick-reply labels rendered as buttons on the greeting. The label
    # the patient taps comes back as their next message body.
    greeting_buttons: list[str] = field(default_factory=list)


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
        body = extract_inbound_body(msg)

        if is_menu_command(body):
            await _handle_menu_command(
                phone_number_id=phone_number_id,
                wa_id=msg.from_,
                patient_name=patient_name,
                wam_id=msg.id,
            )
            continue

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

                # First contact = no prior message on this conversation. Counted
                # BEFORE the inbound below is added so a brand-new conversation
                # reads as 0. Drives the verbatim greeting (see below).
                prior_messages = await session.scalar(
                    select(func.count())
                    .select_from(Message)
                    .where(Message.conversation_id == conversation.id)
                )
                is_first_contact = (prior_messages or 0) == 0

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

                # On first contact, reply with the tenant's configured greeting
                # verbatim (one message, no LLM, no bubble-splitting), optionally
                # with quick-reply buttons. Tenants without a greeting fall
                # through to the improvised LLM opener.
                greeting = (tenant.greeting_message or "").strip()
                if is_first_contact and greeting:
                    greeting_override = greeting
                    greeting_buttons = [str(b) for b in (tenant.greeting_buttons or [])]
                else:
                    greeting_override = None
                    greeting_buttons = []

                return _ReplyContext(
                    conversation_id=conversation.id,
                    patient_wa_id=wa_id,
                    inbound_body=body or "",
                    greeting_override=greeting_override,
                    greeting_buttons=greeting_buttons,
                )
        except IntegrityError:
            # A concurrent worker already claimed this event id.
            logger.info("worker_message_duplicate_race", wam_id=wam_id)
            return None


async def _handle_menu_command(
    *,
    phone_number_id: str | None,
    wa_id: str,
    patient_name: str | None,
    wam_id: str,
) -> None:
    """Reset the conversation and send a fresh button menu.

    Wipes every prior message for the conversation, flips handover back to
    BOT_ACTIVE and pushes a static welcome card. The `/menu` event itself is
    NOT persisted - it is a control command, not real conversation content.
    """
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if await _event_already_processed(session, wam_id):
                    logger.info("worker_menu_duplicate", wam_id=wam_id)
                    return
                session.add(ProcessedEvent(event_id=wam_id))

                tenant = await _resolve_tenant(session, phone_number_id)
                if tenant is None:
                    logger.error(
                        "worker_menu_tenant_unresolved",
                        phone_number_id=phone_number_id,
                    )
                    return

                patient = await _get_or_create_patient(
                    session, tenant, wa_id, patient_name
                )
                conversation = await _get_or_create_conversation(
                    session, tenant, patient
                )

                await session.execute(
                    delete(Message).where(Message.conversation_id == conversation.id)
                )
                conversation.handover_state = HandoverState.BOT_ACTIVE
                conversation.last_human_message_at = None
                conversation.last_bot_message_at = None
                conversation_id = conversation.id
        except IntegrityError:
            logger.info("worker_menu_duplicate_race", wam_id=wam_id)
            return

    try:
        result = await WhatsAppClient().send_buttons(
            to=wa_id,
            body=_MENU_BODY,
            buttons=_MENU_BUTTONS,
        )
    except Exception as exc:
        logger.error(
            "worker_menu_send_failed",
            error=str(exc),
            conversation_id=str(conversation_id),
        )
        return

    async with async_session_factory() as session:
        async with session.begin():
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    wam_id=_extract_sent_wam_id(result),
                    body=_MENU_BODY,
                )
            )
            conversation = await session.get(Conversation, conversation_id)
            if conversation is not None:
                conversation.last_bot_message_at = datetime.now(UTC)
    logger.info("worker_menu_sent", conversation_id=str(conversation_id))


async def _send_bot_reply(reply: _ReplyContext) -> None:
    """Generate a reply, split it into bubbles, send each, and record them."""
    # First-contact greeting: deterministic, verbatim, single message. Skips the
    # LLM and the bubble-splitter so the configured pitch arrives exactly as the
    # clinic wrote it, in one WhatsApp message.
    if reply.greeting_override is not None:
        await _send_greeting(reply)
        return

    # Load per-tenant config (decrypted credentials + prompt data) for this call.
    tenant_config = None
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is not None:
                tenant = await session.get(Tenant, conversation.tenant_id)
                if tenant is not None:
                    tenant_config = await load_tenant_config(session, tenant)
    except Exception as exc:
        logger.warning(
            "worker_tenant_config_load_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )

    reply_text = await run_agent(
        reply.inbound_body,
        context={"conversation_id": str(reply.conversation_id)},
        tenant_config=tenant_config,
    )
    bubbles = parse(reply_text)
    if not bubbles:
        logger.warning(
            "worker_bot_reply_empty_after_parse",
            conversation_id=str(reply.conversation_id),
        )
        return

    client = WhatsAppClient()
    sent_count = 0
    for index, bubble in enumerate(bubbles):
        try:
            result = await _send_bubble(client, reply.patient_wa_id, bubble)
        except Exception as exc:
            # MVP: do not retry. A transient send failure means this one auto-reply
            # is lost; the human secretary still sees the patient's message.
            # TODO(reliability): add an outbox + retry for guaranteed delivery.
            logger.error(
                "worker_bot_reply_failed",
                error=str(exc),
                conversation_id=str(reply.conversation_id),
                bubble_index=index,
                bubble_kind=bubble.kind,
            )
            # A failed mid-turn send still records the bubbles already sent so
            # the LLM history stays consistent on the next inbound.
            break

        sent_count += 1
        async with async_session_factory() as session:
            async with session.begin():
                session.add(
                    Message(
                        conversation_id=reply.conversation_id,
                        direction=MessageDirection.OUTBOUND,
                        sender=MessageSender.BOT,
                        wam_id=_extract_sent_wam_id(result),
                        body=_bubble_history_body(bubble),
                    )
                )
                conversation = await session.get(Conversation, reply.conversation_id)
                if conversation is not None:
                    conversation.last_bot_message_at = datetime.now(UTC)

    if sent_count:
        logger.info(
            "worker_bot_reply_sent",
            conversation_id=str(reply.conversation_id),
            bubbles=sent_count,
        )


async def _send_greeting(reply: _ReplyContext) -> None:
    """Send the tenant's first-contact greeting as a single verbatim message.

    With configured labels it goes out as an interactive reply-button message
    (the tapped label becomes the patient's next inbound); otherwise as plain
    text. The whole greeting is one WhatsApp message either way.
    """
    body = reply.greeting_override or ""
    client = WhatsAppClient()
    try:
        if reply.greeting_buttons:
            # WhatsApp needs a unique id per button; the label drives the LLM,
            # so a positional id is enough (see extract_inbound_body).
            buttons = [
                (f"greeting|{index}", label)
                for index, label in enumerate(reply.greeting_buttons)
            ]
            result = await client.send_buttons(
                to=reply.patient_wa_id, body=body, buttons=buttons
            )
        else:
            result = await client.send_text_message(to=reply.patient_wa_id, body=body)
    except Exception as exc:
        # MVP: no retry (mirrors _send_bot_reply). The patient's message still
        # reached the human secretary; the auto-greeting is simply lost.
        logger.error(
            "worker_greeting_send_failed",
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
                    body=body,
                )
            )
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is not None:
                conversation.last_bot_message_at = datetime.now(UTC)
    logger.info("worker_greeting_sent", conversation_id=str(reply.conversation_id))


async def _send_bubble(
    client: WhatsAppClient,
    to: str,
    bubble: TextBubble | ButtonBubble | SlotsBubble,
) -> dict:
    """Dispatch a single bubble to the right WhatsAppClient method."""
    if isinstance(bubble, ButtonBubble):
        return await client.send_buttons(
            to=to,
            body=bubble.body,
            buttons=[
                (BUTTON_ID_CONFIRM, bubble.confirm_label),
                (BUTTON_ID_CANCEL, bubble.cancel_label),
            ],
        )
    if isinstance(bubble, SlotsBubble):
        return await client.send_list(
            to=to,
            body=bubble.body,
            button_label=bubble.button_label,
            rows=[(rid, label, None) for rid, label in bubble.rows],
            section_title=bubble.section_title,
        )
    return await client.send_text_message(to=to, body=bubble.body)


def _bubble_history_body(bubble: TextBubble | ButtonBubble | SlotsBubble) -> str:
    """Render an outbound bubble as the text the LLM should see in history.

    Interactive cards collapse to a clean string (no markup tags) so the
    next agent turn rebuilt from the DB does not see leftover `[CONFIRM]`
    syntax and try to repeat it.
    """
    if isinstance(bubble, ButtonBubble):
        return bubble.body
    if isinstance(bubble, SlotsBubble):
        labels = ", ".join(label for _, label in bubble.rows)
        return f"{bubble.body}\n(opções: {labels})" if labels else bubble.body
    return bubble.body


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
            body=extract_inbound_body(echo),
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


# --------------------------------------------------------------------------
# Platform-initiated patient notification
# --------------------------------------------------------------------------


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

    try:
        await WhatsAppClient.from_tenant(tenant).send_text_message(to=phone, body=message)
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
