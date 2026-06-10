"""arq job functions - all async webhook processing happens here.

This code runs OUTSIDE the HTTP request/response cycle, so it may safely do
database writes, handover logic and outbound Cloud API calls.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
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
from secretaria.ai.graph import CALENDAR_UNAVAILABLE_SENTINEL, run_agent
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    ProcessedEvent,
    Tenant,
)
from secretaria.schemas.webhook import (
    WebhookPayload,
    WebhookValue,
    extract_inbound_body,
)
from secretaria.services.calendar import CalendarService
from secretaria.services.flow_router import (
    MenuBubble,
    flows_enabled,
    menu_buttons,
    route,
)
from secretaria.services.handover import HandoverManager
from secretaria.services.tenant_config import load_tenant_config
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

# Patient-facing fallbacks for non-conversational outcomes. Hardcoded for the
# MVP; candidates for per-tenant configuration later.
SERVICE_UNAVAILABLE_MESSAGE = (
    "Nosso sistema de agendamento está em configuração. "
    "Em breve estará disponível. 🙏"
)
CALENDAR_UNAVAILABLE_MESSAGE = (
    "Estou com uma dificuldade técnica para acessar a agenda agora. "
    "Nossa equipe foi avisada e entrará em contato em breve. 🙏"
)

# Slash commands the patient can type to reset the conversation. Matched
# case-insensitively against the trimmed message body.
_MENU_COMMANDS = frozenset({"/menu", "/reset", "/recomecar", "/recomeçar", "/inicio", "/início"})

def is_menu_command(body: str | None) -> bool:
    """True when the patient typed a `/menu`-style reset command."""
    if not body:
        return False
    return body.strip().lower() in _MENU_COMMANDS


# Strong, low-false-positive openers a patient uses to state their name. We do
# NOT try to infer a name from arbitrary capitalised words - only from these
# explicit self-introductions.
_NAME_PATTERNS = (
    re.compile(r"\bmeu\s+nome\s+(?:é|eh|e)\s+(.+)", re.IGNORECASE),
    re.compile(r"\bme\s+chamo\s+(.+)", re.IGNORECASE),
    re.compile(r"\bpode\s+me\s+chamar\s+de\s+(.+)", re.IGNORECASE),
)
# Words that terminate a captured name (connectors / fillers that follow it).
_NAME_STOPWORDS = frozenset(
    {"e", "de", "da", "do", "das", "dos", "que", "mas", "então", "entao",
     "por", "favor", "pra", "para", "com", "sou", "tudo", "bem"}
)


def extract_patient_name(body: str | None) -> str | None:
    """Best-effort patient name from an explicit self-introduction.

    Returns a Title-Cased name (max 3 words) or None. Conservative by design:
    only fires on "meu nome é ...", "me chamo ...", "pode me chamar de ...".
    """
    if not body:
        return None
    for pattern in _NAME_PATTERNS:
        match = pattern.search(body)
        if not match:
            continue
        words: list[str] = []
        for raw in re.split(r"\s+", match.group(1).strip()):
            token = raw.strip(".,!?;:()").strip()
            if not token or not token.replace("-", "").isalpha():
                break
            if token.lower() in _NAME_STOPWORDS:
                break
            words.append(token)
            if len(words) == 3:
                break
        if words:
            name = " ".join(w.capitalize() for w in words)
            if 2 <= len(name) <= 80:
                return name
    return None


def _render_greeting_template(template: str, name: str | None) -> str:
    """Substitute the `{{name}}` placeholder and tidy the spacing.

    When the name is unknown the placeholder collapses to nothing and stray
    spaces before punctuation / doubled spaces are cleaned up so the greeting
    still reads naturally (e.g. "Olá {{name}}!" -> "Olá!").
    """
    rendered = template.replace("{{name}}", (name or "").strip())
    rendered = re.sub(r"\s+([,.!?;:])", r"\1", rendered)
    rendered = re.sub(r"[ \t]{2,}", " ", rendered)
    return rendered.strip()


async def _is_rate_limited(redis, phone_number_id: str | None, wa_id: str) -> bool:
    """Sliding-window inbound rate limit per wa_id, backed by the arq Redis pool.

    Fail-open: when Redis is unavailable (or not passed, e.g. in tests) no
    message is dropped. Once a sender exceeds the window cap, a silence key
    suppresses them for RATE_LIMIT_SILENCE_SECONDS.
    """
    if redis is None:
        return False
    settings = get_settings()
    scope = phone_number_id or "default"
    silence_key = f"ratelimit:silence:{scope}:{wa_id}"
    count_key = f"ratelimit:count:{scope}:{wa_id}"
    try:
        if await redis.exists(silence_key):
            return True
        count = await redis.incr(count_key)
        if count == 1:
            await redis.expire(count_key, settings.RATE_LIMIT_WINDOW_SECONDS)
        if count > settings.RATE_LIMIT_MAX_MESSAGES:
            await redis.setex(silence_key, settings.RATE_LIMIT_SILENCE_SECONDS, "1")
            return True
    except Exception as exc:
        logger.warning("worker_rate_limit_check_failed", error=str(exc), wa_id=wa_id)
        return False
    return False


@dataclass(frozen=True)
class _ReplyContext:
    """Minimal data needed to send a bot reply once the inbound DB txn commits."""

    conversation_id: UUID | None
    patient_wa_id: str
    inbound_body: str
    # When set, this is the tenant's verbatim first-contact (or returning)
    # greeting: it is sent as a single message and the LLM is NOT invoked.
    greeting_override: str | None = None
    # Optional quick-reply labels rendered as buttons on the greeting. The label
    # the patient taps comes back as their next message body.
    greeting_buttons: list[str] = field(default_factory=list)
    # When True, the tenant's bot is not activated: send a single polite
    # fallback and do nothing else (no conversation, no LLM).
    service_unavailable: bool = False


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
            elif field == "smb_message_echoes":
                # Coexistence: the human secretary replied from the app.
                await _handle_human_echoes(value)
            else:
                logger.info("worker_field_ignored", field=field)


# --------------------------------------------------------------------------
# Inbound patient messages
# --------------------------------------------------------------------------


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

        # Flood protection: silently drop once a sender exceeds the window cap.
        # Silence is intentional - replying would reward the spammer and still
        # cost an outbound send.
        if await _is_rate_limited(redis, phone_number_id, msg.from_):
            logger.info("worker_rate_limited", wa_id=msg.from_)
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

                # Bot not activated for this tenant (Calendar/types/hours not
                # set up): send a single polite fallback and do NOT create a
                # conversation, persist the message, or invoke the LLM.
                if not tenant.is_active:
                    logger.info("worker_bot_not_active", tenant_id=str(tenant.id))
                    return _ReplyContext(
                        conversation_id=None,
                        patient_wa_id=wa_id,
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
                elif patient_name and not patient.name:
                    patient.name = patient_name

                # Capture a self-introduced name ("meu nome é ...") when we don't
                # have one yet, so future returning greetings can use {{name}}.
                if not patient.name:
                    extracted = extract_patient_name(body)
                    if extracted:
                        patient.name = extracted
                        logger.info("worker_patient_name_captured", patient_id=str(patient.id))

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

                # On first contact, reply with a verbatim greeting (one message,
                # no LLM): the returning greeting (with {{name}}) for a known
                # patient, else the first-contact greeting. Tenants without a
                # greeting fall through to the improvised LLM opener.
                greeting_override = _select_greeting(
                    tenant, patient, is_first_contact, is_returning_patient
                )
                greeting_buttons = _greeting_buttons_for(tenant, greeting_override)

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


def _select_greeting(
    tenant: Tenant,
    patient: Patient,
    is_first_contact: bool,
    is_returning_patient: bool,
) -> str | None:
    """Pick the verbatim greeting to send on first contact, or None.

    Returning patients (seen before) get `returning_greeting_message` with the
    `{{name}}` placeholder filled; otherwise the first-contact greeting is used.
    """
    if not is_first_contact:
        return None
    returning = (tenant.returning_greeting_message or "").strip()
    if is_returning_patient and returning:
        return _render_greeting_template(returning, patient.name)
    first = (tenant.greeting_message or "").strip()
    return first or None


def _greeting_buttons_for(tenant: Tenant, greeting_override: str | None) -> list[str]:
    """Buttons to attach to a greeting.

    When deterministic flows are enabled the greeting doubles as the menu, so
    it carries the menu buttons (which the IDLE router then matches). Otherwise
    the tenant's configured quick-reply labels are used. Empty without a greeting.
    """
    if greeting_override is None:
        return []
    if flows_enabled(tenant):
        return menu_buttons(tenant)
    return [str(b) for b in (tenant.greeting_buttons or [])]


async def _handle_menu_command(
    *,
    phone_number_id: str | None,
    wa_id: str,
    patient_name: str | None,
    wam_id: str,
) -> None:
    """Dev reset: delete the patient who sent /menu, then greet them as new.

    DELETES the patient row for this number and everything tied to it - their
    conversation, every message, and their appointment records - so the number
    is treated as a brand-new first contact. A fresh, empty patient +
    conversation is then created and the tenant's *first-contact* greeting
    (`greeting_message`) is sent, NOT the returning greeting.

    This is a development-only convenience for retesting the new-patient flow
    from a clean slate; it is not part of the shipped patient experience. It
    removes the local appointment rows but does NOT delete the underlying Google
    Calendar events. The `/menu` event itself is not persisted as conversation
    content - it is a control command.
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

                # Wipe the existing patient and all data hanging off them. Done
                # as explicit ordered deletes (not relying on DB cascade) so it
                # behaves identically on Postgres and the SQLite test engine.
                # We select only the id, so no stale ORM object lingers in the
                # identity map after the row is deleted.
                existing_id = await session.scalar(
                    select(Patient.id).where(
                        Patient.tenant_id == tenant.id,
                        Patient.wa_id == wa_id,
                    )
                )
                if existing_id is not None:
                    conv_ids = (
                        await session.scalars(
                            select(Conversation.id).where(
                                Conversation.patient_id == existing_id
                            )
                        )
                    ).all()
                    # Appointments first (FK -> patients/conversations). The
                    # schema would SET NULL and keep them; for a dev reset we
                    # remove the rows so the number leaves zero local trace.
                    await session.execute(
                        delete(Appointment).where(
                            Appointment.patient_id == existing_id
                        )
                    )
                    if conv_ids:
                        await session.execute(
                            delete(Message).where(
                                Message.conversation_id.in_(conv_ids)
                            )
                        )
                        await session.execute(
                            delete(Conversation).where(
                                Conversation.id.in_(conv_ids)
                            )
                        )
                    await session.execute(
                        delete(Patient).where(Patient.id == existing_id)
                    )
                    await session.flush()
                    logger.info("worker_menu_patient_deleted", wa_id=wa_id)

                # Recreate a clean patient + conversation. A fresh conversation
                # already defaults to BOT_ACTIVE + flow IDLE, so there is no
                # prior state left to reset.
                patient = await _get_or_create_patient(
                    session, tenant, wa_id, patient_name
                )
                conversation = await _get_or_create_conversation(
                    session, tenant, patient
                )
                conversation_id = conversation.id
                # Send the first-contact greeting (the "initial" one), NOT the
                # returning greeting - the whole point of deleting the patient.
                greeting = (tenant.greeting_message or "").strip()
                greeting_buttons = _greeting_buttons_for(tenant, greeting or None)
        except IntegrityError:
            logger.info("worker_menu_duplicate_race", wam_id=wam_id)
            return

    if not greeting:
        # No first-contact greeting configured: the slate is clean and the next
        # patient message will get the LLM's improvised opener. Nothing to send.
        logger.info("worker_menu_reset_no_greeting", conversation_id=str(conversation_id))
        return

    # Send the first-contact greeting verbatim (one message, with the configured
    # quick-reply buttons), exactly as a brand-new patient would receive it.
    await _send_greeting(
        _ReplyContext(
            conversation_id=conversation_id,
            patient_wa_id=wa_id,
            inbound_body="",
            greeting_override=greeting,
            greeting_buttons=greeting_buttons,
        )
    )
    logger.info("worker_menu_reset", conversation_id=str(conversation_id))


async def _send_bot_reply(reply: _ReplyContext) -> None:
    """Generate a reply, split it into bubbles, send each, and record them."""
    # Tenant bot not activated: one polite fallback, nothing else.
    if reply.service_unavailable:
        await _send_simple_text(reply.patient_wa_id, SERVICE_UNAVAILABLE_MESSAGE)
        return

    # First-contact greeting: deterministic, verbatim, single message. Skips the
    # LLM and the bubble-splitter so the configured pitch arrives exactly as the
    # clinic wrote it, in one WhatsApp message.
    if reply.greeting_override is not None:
        await _send_greeting(reply)
        return

    # Load per-tenant config + flow context (one short read). We snapshot the
    # flow-relevant fields into plain objects so the router never touches a
    # detached ORM instance after the session closes.
    tenant_config = None
    flow_snapshot: tuple[SimpleNamespace, SimpleNamespace] | None = None
    patient_name = None
    patient_wa = reply.patient_wa_id
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is not None:
                tenant = await session.get(Tenant, conversation.tenant_id)
                if tenant is not None:
                    tenant_config = await load_tenant_config(session, tenant)
                if conversation.patient_id is not None:
                    patient = await session.get(Patient, conversation.patient_id)
                    if patient is not None:
                        patient_name = patient.name
                        patient_wa = patient.wa_id or patient_wa
                if tenant is not None and flows_enabled(tenant):
                    flow_snapshot = (
                        SimpleNamespace(
                            flow_state=conversation.flow_state,
                            flow_step=conversation.flow_step,
                            flow_selected_type=conversation.flow_selected_type,
                            flow_selected_day=conversation.flow_selected_day,
                            flow_selected_slot=conversation.flow_selected_slot,
                            patient_id=conversation.patient_id,
                        ),
                        SimpleNamespace(
                            initial_flows=tenant.initial_flows,
                            appointment_types=tenant.appointment_types,
                            appointment_duration_min=tenant.appointment_duration_min,
                            business_hours=tenant.business_hours,
                        ),
                    )
    except Exception as exc:
        logger.warning(
            "worker_tenant_config_load_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )

    # Deterministic flow engine: when enabled, try to handle the turn with zero
    # LLM calls. Returns True when fully handled; False falls through to the LLM.
    if flow_snapshot is not None:
        conv_snapshot, tenant_snapshot = flow_snapshot
        if await _run_flow(
            reply, conv_snapshot, tenant_snapshot, tenant_config, patient_name, patient_wa
        ):
            return

    reply_text = await run_agent(
        reply.inbound_body,
        context={"conversation_id": str(reply.conversation_id)},
        tenant_config=tenant_config,
    )

    # A tool failed because the calendar is unreachable: tell the patient and
    # hand the conversation to a human secretary instead of faking success.
    if reply_text == CALENDAR_UNAVAILABLE_SENTINEL:
        await _handle_calendar_unavailable(reply)
        return

    bubbles = parse(reply_text)
    if not bubbles:
        logger.warning(
            "worker_bot_reply_empty_after_parse",
            conversation_id=str(reply.conversation_id),
        )
        return

    await _dispatch_bubbles(reply, bubbles)


async def _run_flow(
    reply: _ReplyContext,
    conv_snapshot: SimpleNamespace,
    tenant_snapshot: SimpleNamespace,
    tenant_config,
    patient_name: str | None,
    patient_wa: str | None,
) -> bool:
    """Run the deterministic flow router for this turn.

    `conv_snapshot`/`tenant_snapshot` are detached plain copies of the flow-
    relevant fields (the router does no DB I/O). Returns True when the turn was
    fully handled (bubbles sent, or handed off on a calendar outage); False to
    fall through to the LLM agent.
    """
    calendar = (
        CalendarService.from_tenant_config(tenant_config) if tenant_config else None
    )
    try:
        result = await route(
            conv_snapshot, tenant_snapshot, calendar, reply.inbound_body, patient_name
        )
    except Exception as exc:
        logger.warning(
            "worker_flow_router_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )
        return False

    # Persist the new flow state (+ any booked appointment) in one short txn.
    persisted = True
    try:
        async with async_session_factory() as session:
            async with session.begin():
                conv = await session.get(Conversation, reply.conversation_id)
                if conv is not None:
                    conv.flow_state = result.flow_state
                    conv.flow_step = result.flow_step
                    conv.flow_selected_type = result.flow_selected_type
                    conv.flow_selected_day = result.flow_selected_day
                    conv.flow_selected_slot = result.flow_selected_slot
                    if result.appointment:
                        session.add(
                            Appointment(
                                tenant_id=conv.tenant_id,
                                patient_id=conv.patient_id,
                                conversation_id=conv.id,
                                phone=patient_wa,
                                status=AppointmentStatus.SCHEDULED,
                                **result.appointment,
                            )
                        )
    except Exception as exc:
        persisted = False
        logger.error(
            "worker_flow_persist_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )

    if result.action == "calendar_unavailable":
        await _handle_calendar_unavailable(reply)
        return True
    # A booking was made on Google Calendar but recording it failed: do NOT
    # tell the patient it is confirmed. Hand off to a human to reconcile the
    # (now orphaned) event instead of claiming success or risking a re-book.
    if result.appointment is not None and not persisted:
        await _handle_calendar_unavailable(reply)
        return True
    if result.action == "reply":
        if result.bubbles:
            await _dispatch_bubbles(reply, result.bubbles)
        return True
    return False  # delegate_llm


async def _dispatch_bubbles(reply: _ReplyContext, bubbles: list) -> int:
    """Send each bubble in order, recording outbound messages. Returns count sent.

    MVP: no retry. A transient send failure stops the turn; bubbles already sent
    are recorded so the LLM history stays consistent on the next inbound.
    """
    client = WhatsAppClient()
    sent_count = 0
    for index, bubble in enumerate(bubbles):
        try:
            result = await _send_bubble(client, reply.patient_wa_id, bubble)
        except Exception as exc:
            logger.error(
                "worker_bot_reply_failed",
                error=str(exc),
                conversation_id=str(reply.conversation_id),
                bubble_index=index,
                bubble_kind=getattr(bubble, "kind", "?"),
            )
            break

        sent_count += 1
        # The message is already delivered; a failure to record it must not
        # crash the turn (which would propagate, retry, and short-circuit on the
        # committed ProcessedEvent, losing the bubble entirely). Log and go on.
        try:
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
        except Exception as exc:
            logger.error(
                "worker_bot_reply_record_failed",
                error=str(exc),
                conversation_id=str(reply.conversation_id),
                bubble_index=index,
            )

    if sent_count:
        logger.info(
            "worker_bot_reply_sent",
            conversation_id=str(reply.conversation_id),
            bubbles=sent_count,
        )
    return sent_count


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


async def _send_simple_text(to: str, body: str) -> None:
    """Send a single plain-text message, swallowing send errors (MVP: no retry)."""
    try:
        await WhatsAppClient().send_text_message(to=to, body=body)
    except Exception as exc:
        logger.error("worker_simple_text_send_failed", error=str(exc), to=to)


async def _handle_calendar_unavailable(reply: _ReplyContext) -> None:
    """Degrade gracefully on a calendar outage: notify + hand off to a human."""
    logger.error(
        "worker_calendar_unavailable", conversation_id=str(reply.conversation_id)
    )
    if reply.conversation_id is not None:
        try:
            async with async_session_factory() as session:
                async with session.begin():
                    conversation = await session.get(Conversation, reply.conversation_id)
                    if conversation is not None:
                        await HandoverManager(session).set_human_active(conversation)
        except Exception as exc:
            logger.error(
                "worker_calendar_unavailable_handover_failed",
                error=str(exc),
                conversation_id=str(reply.conversation_id),
            )
    await _send_simple_text(reply.patient_wa_id, CALENDAR_UNAVAILABLE_MESSAGE)


async def _send_bubble(
    client: WhatsAppClient,
    to: str,
    bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble,
) -> dict:
    """Dispatch a single bubble to the right WhatsAppClient method."""
    if isinstance(bubble, MenuBubble):
        # Generic N-button reply card; the tapped label becomes the next body.
        return await client.send_buttons(
            to=to,
            body=bubble.body,
            buttons=[(f"menu|{i}", label) for i, label in enumerate(bubble.labels)],
        )
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


def _bubble_history_body(bubble: TextBubble | ButtonBubble | SlotsBubble | MenuBubble) -> str:
    """Render an outbound bubble as the text the LLM should see in history.

    Interactive cards collapse to a clean string (no markup tags) so the
    next agent turn rebuilt from the DB does not see leftover `[CONFIRM]`
    syntax and try to repeat it.
    """
    if isinstance(bubble, ButtonBubble):
        return bubble.body
    if isinstance(bubble, MenuBubble):
        labels = ", ".join(bubble.labels)
        return f"{bubble.body}\n(opções: {labels})" if labels else bubble.body
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
        # The single-tenant MVP scaffold is live by definition; without this the
        # new is_active gate would silently block the validated dev flow.
        is_active=True,
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


# --------------------------------------------------------------------------
# Cron jobs
# --------------------------------------------------------------------------


async def check_handover_timeouts(ctx: dict) -> None:
    """arq cron: hand stale HUMAN_ACTIVE conversations back to the bot.

    A conversation a human secretary has not touched within
    HANDOVER_TIMEOUT_MINUTES is flipped back to BOT_ACTIVE so the bot answers
    the patient's next message. Registered in arq_worker.WorkerSettings.
    """
    cutoff = datetime.now(UTC) - timedelta(
        minutes=get_settings().HANDOVER_TIMEOUT_MINUTES
    )
    flipped = 0
    async with async_session_factory() as session:
        async with session.begin():
            stale = await session.scalars(
                select(Conversation).where(
                    Conversation.handover_state == HandoverState.HUMAN_ACTIVE,
                    Conversation.last_human_message_at.is_not(None),
                    Conversation.last_human_message_at < cutoff,
                )
            )
            for conversation in stale:
                conversation.handover_state = HandoverState.BOT_ACTIVE
                flipped += 1
                logger.info(
                    "worker_handover_timeout_reset",
                    conversation_id=str(conversation.id),
                )
    if flipped:
        logger.info("worker_handover_timeouts_swept", flipped=flipped)
