"""arq job functions - all async webhook processing happens here.

This code runs OUTSIDE the HTTP request/response cycle, so it may safely do
database writes, handover logic and outbound Cloud API calls.
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Literal
from uuid import UUID
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from transcription_core import (
    MediaTooLarge,
    NotAudio,
    TranscriptionConfig,
    TranscriptionError,
    TranscriptionResult,
    transcribe_whatsapp_media,
)

from secretaria.ai.formatter import (
    BUTTON_ID_CANCEL,
    BUTTON_ID_CONFIRM,
    ButtonBubble,
    SlotsBubble,
    TextBubble,
    parse,
)
from secretaria.ai.graph import (
    CALENDAR_UNAVAILABLE_SENTINEL,
    MANAGE_APPOINTMENT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    SHOW_MAIN_MENU_SENTINEL,
    run_agent,
)
from secretaria.ai.tools import manage_existing_appointment
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    ConsentEvent,
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PixDepositStatus,
    ProcessedEvent,
    Professional,
    Tenant,
)
from secretaria.plugins.base import InboundContext
from secretaria.plugins.post_booking import enqueue_post_booking_hooks
from secretaria.plugins.registry import agent_tools_for, run_on_inbound
from secretaria.schemas.webhook import (
    WebhookPayload,
    WebhookValue,
    extract_action_button,
    extract_echo_body,
    extract_greeting_button,
    extract_inbound_body,
    history_item_is_final,
)
from secretaria.services.calendar import CalendarService
from secretaria.services.email import send_calendar_alert, send_transactional_email_message
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.flow_router import (
    LABEL_BOOK,
    LABEL_CANCEL_APPT,
    LABEL_OTHER,
    LABEL_RESCHEDULE,
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    FlowRouterResult,
    MenuBubble,
    _enter_professional_services,
    classify_yes_no,
    enter_manage_action,
    flows_enabled,
    manage_label,
    menu_buttons_for,
    menu_label,
    reactivation_choice_buttons,
    reactivation_continue_prompt,
    reactivation_enabled,
    reactivation_gap_minutes,
    resume_bubbles,
    route,
)
from secretaria.services.handover import HandoverManager
from secretaria.services.patient_context import (
    PatientOpeningContext,
    PatientOpeningState,
    load_upcoming_appointments,
    resolve_patient_opening_state,
)
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.payments.money import format_brl
from secretaria.services.tenant_config import (
    RuntimeAppointmentType,
    active_appointment_types,
    get_waba_token,
    list_active_professionals,
    load_tenant_config,
    professional_appointment_types,
    resolve_professional_calendar,
    set_waba_token,
)
from secretaria.services.whatsapp import WhatsAppClient

logger = get_logger(__name__)

# Patient-facing fallbacks for non-conversational outcomes. Hardcoded for the
# MVP; candidates for per-tenant configuration later.
SERVICE_UNAVAILABLE_MESSAGE = (
    "Nosso sistema de agendamento está em configuração. Em breve estará disponível. 🙏"
)
CALENDAR_UNAVAILABLE_MESSAGE = (
    "Estou com uma dificuldade técnica para acessar a agenda agora. "
    "Nossa equipe foi avisada e entrará em contato em breve. 🙏"
)
# Sent when a voice note can't be turned into a usable transcript (rejected
# media, or a low-confidence/empty STT result) - see transcribe_audio_message.
AUDIO_UNINTELLIGIBLE_MESSAGE = "Não consegui entender o áudio, pode repetir?"

# Context-aware opening. Appended by _adapt_greeting_to_state to the tenant's
# verbatim greeting; RETURNING_NO_APPOINTMENT / NEW states keep the greeting
# untouched (the returning_greeting_message already covers that tone).
# HAS_UPCOMING(_SOON) (PROMPT 2 final copy): the nearest appointment's detail
# (when/doctor/service/price/description/pre-consult orientations), a brief
# line per OTHER future appointment, and a closing action hint - see
# _adapt_greeting_has_upcoming / _compose_upcoming_greeting_body below.
GREETING_REQUIREMENTS_HEADER = "Orientações de pré-consulta:"
GREETING_BRIEF_HEADER = "Suas próximas consultas:"
GREETING_ACTION_HINT = (
    "Se quiser, use os botões abaixo para remarcar ou cancelar — ou toque em "
    '"Outro" para qualquer outra coisa.'
)
# WhatsApp's interactive body caps at 1024 chars (schemas.config.
# MAX_GREETING_WITH_BUTTONS_CHARS); this leaves margin below that once the
# tenant's own greeting is prepended to the blocks composed here.
GREETING_DETAIL_MAX_CHARS = 1000
# "The first few" lines/bullets kept when trimming for size - see
# _compose_upcoming_greeting_body.
GREETING_BRIEF_KEEP = 3
GREETING_REQUIREMENTS_KEEP = 3
# Neutral on purpose: a past appointment inside the window may still sit in
# SCHEDULED/CONFIRMED (nobody marked the outcome), so this must NOT assert
# the consult happened. Still MVP copy - PROMPT 2 only finalized HAS_UPCOMING.
JUST_HAD_CONSULT_NEUTRAL_LINE = (
    "Vi que você teve uma consulta recentemente, posso ajudar em algo?"
)
# Presupposes attendance — used ONLY when the doctor explicitly set ATTENDED.
JUST_HAD_CONSULT_ATTENDED_LINE = "Como foi sua consulta? Posso ajudar em algo?"

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
    {
        "e",
        "de",
        "da",
        "do",
        "das",
        "dos",
        "que",
        "mas",
        "então",
        "entao",
        "por",
        "favor",
        "pra",
        "para",
        "com",
        "sou",
        "tudo",
        "bem",
    }
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
class _ReactivationDirective:
    """How to handle a returning patient's answer to the 'continuar?' prompt.

    kind="resume" re-renders where they left off (or, for an LLM-mode origin,
    falls through to the agent with history intact); kind="reset" drops the
    saved flow and shows the menu. `origin` is the FlowState value captured when
    the prompt was offered.
    """

    kind: Literal["resume", "reset"]
    origin: str


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
    # Set when this inbound is a returning patient's answer to the "quer
    # continuar?" prompt: drives resume-vs-reset in `_send_bot_reply`.
    reactivation: "_ReactivationDirective | None" = None
    # Set when this inbound is a tap on a reminder's deposit-aware action
    # button (schemas/webhook.py::extract_action_button): (action,
    # appointment_id). Captured in `_persist_inbound_message` BEFORE the
    # handover/flow/LLM gates (see that function) and handled by
    # `_handle_action_button`, called first thing in `_send_bot_reply`.
    action_button: tuple[str, str] | None = None
    # Set when this inbound is a tap on one of the greeting's fixed action
    # buttons (schemas/webhook.py::extract_greeting_button) AND this tenant
    # has no deterministic flow to dispatch it to (flows disabled - see
    # flow_router.flows_enabled). Holds the raw suffix ("agendar"/"remarcar"/
    # "cancelar"/a legacy digit/anything else unrecognized). Handled by
    # `_handle_greeting_button_unavailable`, called early in `_send_bot_reply`
    # - see `_persist_inbound_message`'s greeting-button short-circuit for why
    # a flows-ENABLED tenant's tap never sets this (it flows through the
    # normal text-routed path instead, which route() already handles).
    greeting_button_unavailable: str | None = None


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
            elif field == "history":
                # Coexistence: chat-history sync (progress-only, no content).
                await _handle_history(value)
            elif field == "smb_app_state_sync":
                # Coexistence: business contact list sync (counts only).
                await _handle_smb_app_state_sync(value)
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
            logger.info("worker_rate_limited", wa_id=msg.from_)
            continue

        contact = contacts.get(msg.from_)
        patient_name = contact.profile.name if contact and contact.profile else None
        body = extract_inbound_body(msg)
        action_button = extract_action_button(msg)
        greeting_button = extract_greeting_button(msg)

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
    flows disabled, short-circuits to `greeting_button_unavailable` on the
    returned context instead of falling through to the normal dispatch below
    - see that field's docstring on `_ReplyContext`.
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

                # Timestamp of the last activity BEFORE this inbound, used to
                # measure the silence gap for the returning-patient offer.
                last_activity_at = await session.scalar(
                    select(func.max(Message.created_at)).where(
                        Message.conversation_id == conversation.id
                    )
                )

                session.add(
                    Message(
                        conversation_id=conversation.id,
                        direction=MessageDirection.INBOUND,
                        sender=MessageSender.PATIENT,
                        wam_id=wam_id,
                        body=body,
                    )
                )

                # Reminder action-button tap: short-circuit BEFORE the
                # handover/flow/LLM gates below (see this function's
                # docstring) - `_handle_action_button` does its own
                # tenant-scoped appointment lookup and reply.
                if action_button is not None:
                    return _ReplyContext(
                        conversation_id=conversation.id,
                        patient_wa_id=wa_id,
                        inbound_body=body or "",
                        action_button=action_button,
                    )

                handover = HandoverManager(session)
                if not handover.is_bot_active(conversation):
                    # Human secretary is handling it - record only, stay quiet.
                    logger.info(
                        "worker_bot_paused_human_active",
                        conversation_id=str(conversation.id),
                    )
                    return None

                # Greeting-button tap this tenant can't fulfil deterministically:
                # flows are disabled for them, so route() would otherwise
                # delegate straight to the LLM (see flow_router.route()'s
                # top-of-function flows_enabled gate). A flows-ENABLED tenant's
                # tap is NOT special-cased here - it falls through to the
                # normal dispatch below, which route() already handles
                # deterministically (LABEL_BOOK/LABEL_RESCHEDULE/
                # LABEL_CANCEL_APPT, or a graceful "here's the menu again" for
                # a stale/legacy label route() doesn't recognize).
                if greeting_button is not None and not flows_enabled(tenant):
                    return _ReplyContext(
                        conversation_id=conversation.id,
                        patient_wa_id=wa_id,
                        inbound_body=body or "",
                        greeting_button_unavailable=greeting_button,
                    )

                # A pending "quer continuar?" answer takes precedence over any
                # greeting/offer: resume where they were, or reset to the menu.
                if conversation.reactivation_origin is not None:
                    origin = conversation.reactivation_origin
                    conversation.reactivation_origin = None  # consume the gate
                    answer = classify_yes_no(body, tenant)
                    if answer == "no":
                        conversation.flow_state = FlowState.IDLE
                        conversation.flow_step = None
                        conversation.flow_selected_type = None
                        conversation.flow_selected_day = None
                        conversation.flow_selected_slot = None
                        conversation.flow_selected_professional_id = None
                        conversation.flow_selected_insurance = None
                        conversation.flow_managing_appointment_id = None
                        return _ReplyContext(
                            conversation_id=conversation.id,
                            patient_wa_id=wa_id,
                            inbound_body=body or "",
                            reactivation=_ReactivationDirective(kind="reset", origin=origin),
                        )
                    if answer == "yes":
                        return _ReplyContext(
                            conversation_id=conversation.id,
                            patient_wa_id=wa_id,
                            inbound_body=body or "",
                            reactivation=_ReactivationDirective(kind="resume", origin=origin),
                        )
                    # "other": gate consumed; fall through to normal dispatch so
                    # their message is routed against the preserved flow state.

                # On first contact, reply with a verbatim greeting (one message,
                # no LLM): the returning greeting (with {{name}}) for a known
                # patient, else the first-contact greeting. Tenants without a
                # greeting fall through to the improvised LLM opener.
                greeting_override = _select_greeting(
                    tenant, patient, is_first_contact, is_returning_patient
                )

                # Context-aware opening (indexed reads, worker-side only): adapt
                # the verbatim greeting to the patient's real appointment state.
                # Same gate as the greeting itself — the conversation-opening
                # message only. patient_id is always resolved here today (the
                # Patient row is created/flushed above); the resolver's None
                # guard is the safe degrade for any future call site.
                # `opening_context` also feeds `_greeting_buttons_for` below
                # (HAS_UPCOMING(_SOON) swaps the manage-action trio in for the
                # menu) - it stays None when there is no greeting to adapt.
                opening_context = None
                if greeting_override is not None:
                    opening_context = await resolve_patient_opening_state(
                        session, tenant.id, patient.id
                    )
                    # HAS_UPCOMING(_SOON) needs a bit more than the resolver's
                    # own indexed reads: the referenced professionals' display
                    # names and the nearest appointment's catalog entry (for
                    # price/description/orientações). Loaded here, in the same
                    # open session, so `_adapt_greeting_to_state` itself stays
                    # a pure composition function with no DB access of its own.
                    upcoming_data = None
                    if opening_context is not None and opening_context.state in (
                        PatientOpeningState.HAS_UPCOMING_SOON,
                        PatientOpeningState.HAS_UPCOMING,
                    ):
                        upcoming_data = await _load_upcoming_greeting_data(
                            session, tenant, opening_context.future_appointments
                        )
                    greeting_override = _adapt_greeting_to_state(
                        greeting_override, opening_context, tenant, upcoming_data
                    )

                # Returning after a silence gap (and not already greeting on first
                # contact): offer to resume the prior workflow, or re-greet.
                if (
                    greeting_override is None
                    and is_returning_patient
                    and reactivation_enabled(tenant)
                ):
                    offer = _reactivation_offer(
                        conversation,
                        tenant,
                        patient,
                        wa_id,
                        body,
                        last_activity_at,
                    )
                    if offer is not None:
                        return offer

                greeting_buttons = _greeting_buttons_for(tenant, greeting_override, opening_context)

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


def _greeting_buttons_for(
    tenant: Tenant,
    greeting_override: str | None,
    opening_context: PatientOpeningContext | None = None,
) -> list[str]:
    """Buttons to attach to a greeting: a FIXED, product-defined set.

    NEVER the clinic's own free text: `tenant.greeting_buttons` is no longer
    read here (nor anywhere else) - see docs/CHECKPOINT_fixed_greeting_buttons.md.
    Every button this function can return has a deterministic route() entry
    (flow_router.py's LABEL_BOOK/LABEL_RESCHEDULE/LABEL_CANCEL_APPT/
    LABEL_OTHER matches) and never falls through to the LLM on its own tap.

    WhatsApp caps interactive messages at 3 buttons. When the patient has a
    live upcoming appointment (HAS_UPCOMING/_SOON, from `opening_context`)
    AND flows are enabled, the two most useful actions - Remarcar/Cancelar -
    win the two deterministic slots, with "Outro" filling the third for
    anything else (unchanged from before this round). Every other case -
    including a flows-DISABLED tenant, which used to get its own free-text
    `greeting_buttons` here - gets the same fixed [Agendar, Remarcar,
    Cancelar] trio: for a flows-enabled tenant route() dispatches each label
    deterministically (see above); for a flows-disabled one, a tap is caught
    by `_persist_inbound_message`'s greeting-button short-circuit BEFORE it
    would ever reach route()'s unconditional LLM delegation, and degrades to
    a fixed "contact us" reply instead (`_handle_greeting_button_unavailable`)
    - so no button offered here ever reaches the LLM either way. Empty
    without a greeting.
    """
    if greeting_override is None:
        return []
    if (
        flows_enabled(tenant)
        and opening_context is not None
        and opening_context.state
        in (PatientOpeningState.HAS_UPCOMING_SOON, PatientOpeningState.HAS_UPCOMING)
        and opening_context.future_appointments
    ):
        return [LABEL_RESCHEDULE, LABEL_CANCEL_APPT, LABEL_OTHER]
    return [LABEL_BOOK, LABEL_RESCHEDULE, LABEL_CANCEL_APPT]


def _format_appointment_when(start_at: datetime, tz_name: str | None) -> str:
    """Render an appointment start for greeting copy, in the tenant's timezone."""
    tz = ZoneInfo(tz_name or "America/Sao_Paulo")
    return _as_utc(start_at).astimezone(tz).strftime("%d/%m às %H:%M")


@dataclass(frozen=True)
class _UpcomingGreetingData:
    """Plain data `_adapt_greeting_to_state` needs for HAS_UPCOMING(_SOON).

    Loaded by `_load_upcoming_greeting_data` inside the open ingest session
    (`_persist_inbound_message`) and passed in so the composition functions
    below stay DB-free and directly unit-testable. Every other opening state
    ignores this argument entirely.
    """

    # professional_id (str) -> stored display name (Professional.name,
    # verbatim - no honorific logic), for every distinct professional
    # referenced across `future_appointments`. Includes INACTIVE
    # professionals: an appointment with a deactivated doctor still shows
    # their name.
    professional_names: dict[str, str] = field(default_factory=dict)
    # Resolved catalog dict (name/price/description/long_description/
    # requirements - see services/tenant_config.py) for the NEAREST
    # appointment's service, matched by name (casefold, stripped) against the
    # owning professional's own catalog, or the tenant's when the appointment
    # has no owner. None when no match was found (stale/renamed service) -
    # the detail block then degrades to date/doctor/service-name only.
    nearest_service: dict | None = None


def _appointment_doctor_name(
    appointment: dict, professional_names: dict[str, str]
) -> str | None:
    """The stored professional name for one `future_appointments` dict, or None.

    Verbatim from `Professional.name` - no "Dr(a)." honorific games, the
    clinic's own stored name is used as-is. None when the appointment has no
    `professional_id`, or (should not happen - see `_load_upcoming_greeting_data`)
    the id wasn't resolved.
    """
    professional_id = appointment.get("professional_id")
    return professional_names.get(professional_id) if professional_id else None


def _compose_upcoming_greeting_body(
    greeting: str,
    intro: str,
    description: str | None,
    requirements: list[str],
    brief_lines: list[str],
    *,
    max_chars: int = GREETING_DETAIL_MAX_CHARS,
) -> str:
    """Pure compose + size-trim for the HAS_UPCOMING(_SOON) greeting body.

    Assembles, in order: `greeting`, `intro` (the nearest appointment's
    always-shown when/doctor/service/price lines, already rendered by the
    caller), the optional `description` paragraph, the "Orientações de
    pré-consulta" bullets (one per `requirements` item), the "Suas próximas
    consultas" bullets (one per `brief_lines` entry - each already rendered
    as "when — service[ — doctor]": date+service+doctor ONLY, no
    price/description), and the closing action hint.

    Pure string/list manipulation - no DB, no ORM types - so it is unit-tested
    directly with plain args. Under-budget input is returned unmodified. Over
    `max_chars`, drops content in order until it fits: (1) `description`,
    (2) excess `brief_lines` (kept to the first `GREETING_BRIEF_KEEP`, with an
    "… e mais N consultas" tail), (3) excess `requirements` bullets (kept to
    the first `GREETING_REQUIREMENTS_KEEP`, with a "…" tail). If still over
    budget after all three, the maximally-trimmed body is returned as-is
    (best effort - the tenant's own greeting text is never touched).
    """

    def _assemble(
        *, include_description: bool, brief_cap: int | None, requirements_cap: int | None
    ) -> str:
        parts = [greeting, intro]
        if include_description and description:
            parts.append(description)
        if requirements:
            shown = requirements if requirements_cap is None else requirements[:requirements_cap]
            bullets = "\n".join(f"• {item}" for item in shown)
            if requirements_cap is not None and len(requirements) > requirements_cap:
                bullets += "\n…"
            parts.append(f"{GREETING_REQUIREMENTS_HEADER}\n{bullets}")
        if brief_lines:
            shown = brief_lines if brief_cap is None else brief_lines[:brief_cap]
            bullets = "\n".join(f"• {line}" for line in shown)
            if brief_cap is not None and len(brief_lines) > brief_cap:
                extra = len(brief_lines) - brief_cap
                tail = "consulta" if extra == 1 else "consultas"
                bullets += f"\n… e mais {extra} {tail}"
            parts.append(f"{GREETING_BRIEF_HEADER}\n{bullets}")
        parts.append(GREETING_ACTION_HINT)
        return "\n\n".join(parts)

    body = _assemble(include_description=True, brief_cap=None, requirements_cap=None)
    if len(body) <= max_chars:
        return body

    body = _assemble(include_description=False, brief_cap=None, requirements_cap=None)
    if len(body) <= max_chars:
        return body

    body = _assemble(
        include_description=False, brief_cap=GREETING_BRIEF_KEEP, requirements_cap=None
    )
    if len(body) <= max_chars:
        return body

    return _assemble(
        include_description=False,
        brief_cap=GREETING_BRIEF_KEEP,
        requirements_cap=GREETING_REQUIREMENTS_KEEP,
    )


def _adapt_greeting_has_upcoming(
    greeting: str,
    future_appointments: list[dict],
    tenant: Tenant,
    upcoming_data: _UpcomingGreetingData,
) -> str:
    """Render the HAS_UPCOMING(_SOON) greeting body (still a pure function).

    `future_appointments` is `PatientOpeningContext.future_appointments`
    (nearest first, guaranteed non-empty by the resolver for this state).
    `upcoming_data` carries the plain data the call site resolved inside the
    open DB session - see `_load_upcoming_greeting_data`.
    """
    nearest = future_appointments[0]
    when = _format_appointment_when(nearest["start_at"], tenant.timezone)
    doctor = _appointment_doctor_name(nearest, upcoming_data.professional_names)
    service_name = nearest.get("appointment_type") or "Consulta"

    header_line = f"Vi aqui que você já tem uma consulta marcada para {when}"
    if doctor:
        header_line += f", com {doctor}"
    header_line += "."

    service = upcoming_data.nearest_service
    price = service.get("price") if service else None
    service_line = f"{service_name} — {price}" if price else service_name
    description = (
        (service.get("long_description") or service.get("description")) if service else None
    )
    requirements = list(service.get("requirements") or []) if service else []

    brief_lines = []
    for appt in future_appointments[1:]:
        appt_when = _format_appointment_when(appt["start_at"], tenant.timezone)
        line = f"{appt_when} — {appt.get('appointment_type') or 'Consulta'}"
        appt_doctor = _appointment_doctor_name(appt, upcoming_data.professional_names)
        if appt_doctor:
            line += f" — {appt_doctor}"
        brief_lines.append(line)

    intro = f"{header_line}\n{service_line}"
    return _compose_upcoming_greeting_body(greeting, intro, description, requirements, brief_lines)


def _adapt_greeting_to_state(
    greeting: str,
    context: PatientOpeningContext | None,
    tenant: Tenant,
    upcoming_data: _UpcomingGreetingData | None = None,
) -> str:
    """Append the state-aware opening content to the verbatim greeting.

    A None context (patient not resolved) and the RETURNING_NO_APPOINTMENT/NEW
    states return the greeting unchanged — the safe degrade (the returning
    greeting message already covers the "good to see you again" tone).
    JUST_HAD_CONSULT only presupposes attendance when the doctor explicitly
    marked ATTENDED; a past row still in SCHEDULED/CONFIRMED has an unknown
    outcome and gets the neutral line. HAS_UPCOMING(_SOON) gets the full
    detail/brief blocks (`_adapt_greeting_has_upcoming`); `upcoming_data`
    defaults to empty so callers that don't resolve it (or don't need to,
    for other states) still degrade gracefully rather than crash.
    """
    if context is None:
        return greeting
    if context.state in (
        PatientOpeningState.HAS_UPCOMING_SOON,
        PatientOpeningState.HAS_UPCOMING,
    ):
        return _adapt_greeting_has_upcoming(
            greeting,
            context.future_appointments,
            tenant,
            upcoming_data or _UpcomingGreetingData(),
        )
    if context.state is PatientOpeningState.JUST_HAD_CONSULT:
        line = (
            JUST_HAD_CONSULT_ATTENDED_LINE
            if context.recent_past_appointments[0]["status"] == AppointmentStatus.ATTENDED
            else JUST_HAD_CONSULT_NEUTRAL_LINE
        )
        return f"{greeting}\n\n{line}"
    return greeting


async def _load_upcoming_greeting_data(
    session: AsyncSession, tenant: Tenant, future_appointments: list[dict]
) -> _UpcomingGreetingData:
    """DB-side loads for the HAS_UPCOMING(_SOON) greeting detail (impure).

    Called from `_persist_inbound_message`'s open ingest session, right after
    `resolve_patient_opening_state`. Resolves (a) the display name for every
    professional referenced across `future_appointments` (active or not - a
    deactivated doctor's name must still show on an existing booking), and
    (b) the catalog service dict for the NEAREST appointment (for
    price/description/requirements enrichment). Returns plain data only (see
    `_UpcomingGreetingData`) so `_adapt_greeting_to_state` and its helpers
    stay DB-free. No appointment content is logged here - state/count
    logging is already done by `resolve_patient_opening_state`.
    """
    professional_ids = {
        pid for appt in future_appointments if (pid := appt.get("professional_id"))
    }
    professionals_by_id: dict[str, Professional] = {}
    if professional_ids:
        rows = await session.scalars(
            select(Professional).where(
                Professional.id.in_([UUID(pid) for pid in professional_ids])
            )
        )
        professionals_by_id = {str(row.id): row for row in rows}

    nearest = future_appointments[0]
    owner_id = nearest.get("professional_id")
    owner = professionals_by_id.get(owner_id) if owner_id else None
    catalog = (
        professional_appointment_types(owner, tenant)
        if owner is not None
        else active_appointment_types(tenant)
    )
    nearest_service = None
    raw_type = nearest.get("appointment_type")
    if raw_type:
        target_name = raw_type.strip().casefold()
        nearest_service = next(
            (svc for svc in catalog if str(svc.get("name", "")).strip().casefold() == target_name),
            None,
        )

    return _UpcomingGreetingData(
        professional_names={pid: row.name for pid, row in professionals_by_id.items()},
        nearest_service=nearest_service,
    )


def _as_utc(dt: datetime) -> datetime:
    """Treat a naive timestamp (e.g. from SQLite) as UTC; pass tz-aware through."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _reactivation_offer(
    conversation: Conversation,
    tenant: Tenant,
    patient: Patient,
    wa_id: str,
    body: str | None,
    last_activity_at: datetime | None,
) -> _ReplyContext | None:
    """Maybe offer a returning patient to resume, after a silence gap.

    When the conversation has a resumable state, arms the gate
    (`conversation.reactivation_origin`) and returns an offer that reuses the
    greeting send path: the returning greeting + the configured "quer continuar?"
    question, with Sim/Não buttons. For an IDLE conversation there is nothing to
    resume, so it sends just the returning greeting + menu. Returns None to fall
    through to normal dispatch (gap not reached yet, or nothing to say).
    """
    if last_activity_at is None:
        return None
    gap = datetime.now(UTC) - _as_utc(last_activity_at)
    if gap < timedelta(minutes=reactivation_gap_minutes(tenant)):
        return None

    returning = (tenant.returning_greeting_message or "").strip() or (
        tenant.greeting_message or ""
    ).strip()
    greeting = _render_greeting_template(returning, patient.name) if returning else ""

    if conversation.flow_state in (
        FlowState.MENU,
        FlowState.SERVICE_CATALOG,
        FlowState.LLM,
    ):
        # Resumable: arm the gate and ask whether to continue.
        conversation.reactivation_origin = conversation.flow_state.value
        prompt = reactivation_continue_prompt(tenant)
        body_text = f"{greeting}\n\n{prompt}".strip() if greeting else prompt
        return _ReplyContext(
            conversation_id=conversation.id,
            patient_wa_id=wa_id,
            inbound_body=body or "",
            greeting_override=body_text,
            greeting_buttons=reactivation_choice_buttons(tenant),
        )

    # IDLE / nothing to resume: a plain returning greeting + menu, if configured.
    if not greeting:
        return None
    return _ReplyContext(
        conversation_id=conversation.id,
        patient_wa_id=wa_id,
        inbound_body=body or "",
        greeting_override=greeting,
        greeting_buttons=_greeting_buttons_for(tenant, greeting),
    )


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
                            select(Conversation.id).where(Conversation.patient_id == existing_id)
                        )
                    ).all()
                    # Appointments first (FK -> patients/conversations). The
                    # schema would SET NULL and keep them; for a dev reset we
                    # remove the rows so the number leaves zero local trace.
                    await session.execute(
                        delete(Appointment).where(Appointment.patient_id == existing_id)
                    )
                    if conv_ids:
                        await session.execute(
                            delete(Message).where(Message.conversation_id.in_(conv_ids))
                        )
                        await session.execute(
                            delete(Conversation).where(Conversation.id.in_(conv_ids))
                        )
                    await session.execute(delete(Patient).where(Patient.id == existing_id))
                    await session.flush()
                    logger.info("worker_menu_patient_deleted", wa_id=wa_id)

                # Recreate a clean patient + conversation. A fresh conversation
                # already defaults to BOT_ACTIVE + flow IDLE, so there is no
                # prior state left to reset.
                patient = await _get_or_create_patient(session, tenant, wa_id, patient_name)
                conversation = await _get_or_create_conversation(session, tenant, patient)
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


async def _send_bot_reply(reply: _ReplyContext, redis=None) -> None:
    """Generate a reply, split it into bubbles, send each, and record them."""
    # Reminder action-button tap: a fully self-contained turn (its own
    # tenant/appointment lookup, its own reply) - never falls through to the
    # entitlement gate, deterministic flow, or LLM below. See
    # `_persist_inbound_message`'s docstring for why this runs first.
    if reply.action_button is not None:
        action, appointment_id = reply.action_button
        await _handle_action_button(reply, action, appointment_id, redis=redis)
        return

    # Greeting-button tap this (flows-disabled) tenant can't fulfil
    # deterministically: a fully self-contained degrade, same shape as
    # action_button above - never falls through to the LLM.
    if reply.greeting_button_unavailable is not None:
        await _handle_greeting_button_unavailable(reply, reply.greeting_button_unavailable)
        return

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
    tenant: Tenant | None = None
    tenant_config = None
    waba_token: str | None = None
    summary = None
    flow_snapshot: tuple[SimpleNamespace, SimpleNamespace] | None = None
    upcoming_appointments: list[dict] | None = None
    # Multi-doctor context: the active-professionals snapshot (the router's
    # roster AND the run_agent prompt-context source), the selected
    # professional's plain snapshot, and their resolved calendar.
    professional_rows: list[Professional] | None = None
    flow_professionals: list[SimpleNamespace] | None = None
    selected_professional: SimpleNamespace | None = None
    flow_calendar: CalendarService | None = None
    # The manage (cancel/reschedule) sub-flow's owning calendar for THIS turn
    # (see `_manage_owner_calendar_target`) - a professional's own, or the
    # tenant's, resolved independently of any stale booking-flow selection.
    manage_calendar: CalendarService | None = None
    patient_name = None
    patient_wa = reply.patient_wa_id
    # Post-consult-knowledge injection gate (see
    # _should_inject_post_consult_knowledge below): the patient's derived
    # opening state and the conversation's flow_state, captured as plain
    # values inside the session below - both stay None when the
    # conversation/tenant fails to resolve, or the check isn't needed this turn.
    opening_state: PatientOpeningState | None = None
    flow_state: FlowState | None = None
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is not None:
                flow_state = conversation.flow_state
                tenant = await session.get(Tenant, conversation.tenant_id)
                if tenant is not None:
                    tenant_config = await load_tenant_config(session, tenant)
                    # Decrypt inside the session (single seam); the plaintext
                    # value only travels in memory from here on.
                    waba_token = await get_waba_token(session, tenant.id)
                    # The ONE entitlement read for this inbound message
                    # (Redis-cached — see services/entitlements_client.py).
                    summary = await get_entitlements(tenant.id, redis)
                    # Post-consult knowledge only needs the patient's derived
                    # opening state when the turn doesn't already qualify via
                    # flow_state == LLM (_should_inject_post_consult_knowledge) -
                    # skips the extra appointment query on every other turn.
                    if (
                        (tenant.post_consult_knowledge or "").strip()
                        and conversation.patient_id is not None
                        and flow_state != FlowState.LLM
                    ):
                        opening_context = await resolve_patient_opening_state(
                            session, tenant.id, conversation.patient_id
                        )
                        opening_state = (
                            opening_context.state if opening_context is not None else None
                        )
                if conversation.patient_id is not None:
                    patient = await session.get(Patient, conversation.patient_id)
                    if patient is not None:
                        patient_name = patient.name
                        patient_wa = patient.wa_id or patient_wa
                if tenant is not None:
                    # Active-professionals snapshot (plain objects — the flow
                    # router does no DB I/O). Loaded whenever a tenant
                    # resolves, not only when flows are on: the agent's
                    # professional context + sentinel hand-backs need it too.
                    professional_rows = await list_active_professionals(session, tenant.id)
                    flow_professionals = [
                        SimpleNamespace(
                            id=p.id,
                            name=p.name,
                            specialty=p.specialty,
                            about=p.about,
                            context_doctor_message=p.context_doctor_message,
                            appointment_types=p.appointment_types,
                        )
                        for p in professional_rows
                    ]
                    selected_id = conversation.flow_selected_professional_id
                    if selected_id is not None:
                        selected_row = next(
                            (p for p in professional_rows if p.id == selected_id), None
                        )
                        if selected_row is not None:
                            selected_professional = next(
                                p for p in flow_professionals if p.id == selected_id
                            )
                            # The selected doctor's own CalendarService, so the
                            # flow's day/slot/booking hit THEIR agenda.
                            try:
                                flow_calendar = await resolve_professional_calendar(
                                    session,
                                    tenant,
                                    selected_row,
                                    tenant_config=tenant_config,
                                )
                            except Exception as exc:
                                # The flow degrades to the LLM on a missing
                                # professional calendar — never blocks the reply.
                                logger.warning(
                                    "worker_professional_calendar_failed",
                                    error=str(exc),
                                    professional_id=str(selected_id),
                                )
                if tenant is not None and flows_enabled(tenant):
                    flow_snapshot = (
                        SimpleNamespace(
                            flow_state=conversation.flow_state,
                            flow_step=conversation.flow_step,
                            flow_selected_type=conversation.flow_selected_type,
                            flow_selected_day=conversation.flow_selected_day,
                            flow_selected_slot=conversation.flow_selected_slot,
                            flow_selected_professional_id=(
                                conversation.flow_selected_professional_id
                            ),
                            flow_selected_insurance=conversation.flow_selected_insurance,
                            flow_managing_appointment_id=(
                                conversation.flow_managing_appointment_id
                            ),
                            patient_id=conversation.patient_id,
                        ),
                        SimpleNamespace(
                            initial_flows=tenant.initial_flows,
                            appointment_types=tenant.appointment_types,
                            appointment_duration_min=tenant.appointment_duration_min,
                            business_hours=tenant.business_hours,
                            collect_insurance=tenant.collect_insurance,
                            insurances=tenant.insurances,
                        ),
                    )
                    # Load the patient's future appointments whenever THIS turn
                    # might need them - no longer only the manage (cancel/
                    # reschedule) flow: the manage flow being active or being
                    # opened (classic manage_label, or a direct "Remarcar"/
                    # "Cancelar" tap - see enter_manage_action), a conversation
                    # already in (or entering) full LLM mode via "Outro" (see
                    # _should_inject_appointment_context /
                    # _appointment_context_text below) - so the router, the
                    # manage_existing_appointment tool hand-back, and the LLM
                    # prompt can list/resolve them without any of them doing
                    # their own DB I/O.
                    wants_upcoming_appointments = (
                        conversation.flow_state in (FlowState.MANAGE_BOOKING, FlowState.LLM)
                        or _label_match_body(reply.inbound_body, manage_label(tenant))
                        or _label_match_body(reply.inbound_body, LABEL_RESCHEDULE)
                        or _label_match_body(reply.inbound_body, LABEL_CANCEL_APPT)
                        or _label_match_body(reply.inbound_body, LABEL_OTHER)
                    )
                    if wants_upcoming_appointments and conversation.patient_id is not None:
                        upcoming_appointments = await load_upcoming_appointments(
                            session, tenant.id, conversation.patient_id
                        )
                    # Owning-professional calendar for THIS manage turn: cancel/
                    # reschedule must act on the agenda that actually owns the
                    # appointment, never a stale booking-flow selection - see
                    # `_manage_owner_calendar_target`.
                    manage_target = _manage_owner_calendar_target(
                        conversation.flow_state,
                        conversation.flow_managing_appointment_id,
                        upcoming_appointments,
                        professional_rows,
                    )
                    if isinstance(manage_target, Professional):
                        try:
                            manage_calendar = await resolve_professional_calendar(
                                session, tenant, manage_target, tenant_config=tenant_config
                            )
                        except Exception as exc:
                            # Degrades to the LLM (manage_calendar stays None) -
                            # never blocks the reply. Count-only logging.
                            logger.warning(
                                "worker_manage_calendar_failed",
                                error=str(exc),
                                professional_id=str(manage_target.id),
                            )
                    elif manage_target == "tenant":
                        manage_calendar = (
                            CalendarService.from_tenant_config(tenant_config)
                            if tenant_config
                            else None
                        )
    except Exception as exc:
        logger.warning(
            "worker_tenant_config_load_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )

    # Entitlement gate: a tenant whose subscription/status doesn't clear brain-api
    # gets NO reply at all (the inbound message stays persisted; handover state is
    # untouched — a human can still see and answer it). Fails closed: a missing
    # summary (fetch failure with no stale fallback, or no tenant to check) is
    # treated the same as an explicit "not entitled".
    if summary is None or not (summary.active and summary.secretaria_enabled):
        logger.warning(
            "bot_reply_suppressed_unentitled",
            tenant_id=str(tenant.id) if tenant is not None else None,
            status=summary.status if summary is not None else None,
        )
        return

    # Optional-addon inbound interception (e.g. human_backup_24_7's
    # outside-business-hours handover). Runs once entitlement is confirmed,
    # BEFORE any flow logic decides what the bot would say. A hook that
    # returns True owns this turn completely - no further reply is sent.
    if tenant is not None and reply.conversation_id is not None:
        handled = await run_on_inbound(
            summary,
            InboundContext(
                tenant=tenant,
                conversation_id=reply.conversation_id,
                patient_wa_id=patient_wa,
                inbound_body=reply.inbound_body,
                waba_token=waba_token,
                redis=redis,
            ),
        )
        if handled:
            return

    # Returning-patient resume/reset: act on the "quer continuar?" answer. The
    # offer itself went out via the greeting path; this handles the tap.
    if reply.reactivation is not None and flow_snapshot is not None:
        conv_snapshot, tenant_snapshot = flow_snapshot
        if reply.reactivation.kind == "reset":
            # "Não": drop the saved flow and show a fresh (effective) menu.
            result = FlowRouterResult(
                action="reply",
                bubbles=[
                    MenuBubble(
                        body=menu_label(tenant_snapshot),
                        labels=menu_buttons_for(
                            tenant_snapshot, len(flow_professionals or []) > 1
                        ),
                    )
                ],
                flow_state=FlowState.MENU,
            )
            await _apply_flow_result(
                reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
            )
            return
        # "Sim": re-render the deterministic step they were on. LLM-mode origins
        # (and any resume that has to delegate) fall through to the agent below,
        # which already has the full history.
        if reply.reactivation.origin != FlowState.LLM.value:
            calendar = _flow_turn_calendar(conv_snapshot, tenant_config, flow_calendar)
            result = await resume_bubbles(
                conv_snapshot, tenant_snapshot, calendar, professionals=flow_professionals
            )
            if await _apply_flow_result(
                reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
            ):
                return
        # Skip the deterministic block so "Sim" isn't re-interpreted as input.
        flow_snapshot = None

    # Deterministic flow engine: when enabled, try to handle the turn with zero
    # LLM calls. Returns True when fully handled; False falls through to the LLM.
    if flow_snapshot is not None:
        conv_snapshot, tenant_snapshot = flow_snapshot
        if await _run_flow(
            reply,
            conv_snapshot,
            tenant_snapshot,
            tenant_config,
            patient_name,
            patient_wa,
            upcoming_appointments=upcoming_appointments,
            redis=redis,
            tenant=tenant,
            waba_token=waba_token,
            professionals=flow_professionals,
            flow_calendar=flow_calendar,
            manage_calendar=manage_calendar,
        ):
            return

    # Reaching here with flow_snapshot set means _run_flow ran and returned
    # False - the router explicitly delegated THIS turn to the LLM (e.g. the
    # "Outro" tap) - one of the post-consult-knowledge qualifying conditions.
    delegated_to_llm = flow_snapshot is not None

    # Appointment-context injection (Outro -> LLM handoff): a rendered
    # "consultas marcadas" block for THIS turn's prompt, only when the gate
    # qualifies - see _should_inject_appointment_context / _appointment_context_text.
    appointment_context_text = None
    if _should_inject_appointment_context(upcoming_appointments, flow_state, delegated_to_llm):
        professional_names = {str(p.id): p.name for p in (flow_professionals or [])}
        appointment_context_text = _appointment_context_text(
            upcoming_appointments or [],
            tenant_config.timezone if tenant_config is not None else None,
            professional_names,
            tenant_config.appointment_types if tenant_config is not None else [],
        )

    reply_text = await run_agent(
        reply.inbound_body,
        context={"conversation_id": str(reply.conversation_id)},
        tenant_config=tenant_config,
        # manage_existing_appointment is exposed only for flow-enabled tenants
        # (its sentinel hand-back re-enters the deterministic manage flow,
        # which does not exist otherwise) - see _handle_manage_appointment.
        extra_tools=(
            [*agent_tools_for(summary), manage_existing_appointment]
            if (tenant is not None and flows_enabled(tenant))
            else agent_tools_for(summary)
        ),
        redis=redis,
        selected_professional=selected_professional,
        include_post_consult_knowledge=_should_inject_post_consult_knowledge(
            tenant_config.post_consult_knowledge if tenant_config is not None else None,
            opening_state,
            flow_state,
            delegated_to_llm,
        ),
        appointment_context=appointment_context_text,
    )

    # A tool failed because the calendar is unreachable: tell the patient and
    # hand the conversation to a human secretary instead of faking success.
    if reply_text == CALENDAR_UNAVAILABLE_SENTINEL:
        await _handle_calendar_unavailable(reply, redis=redis, tenant=tenant, waba_token=waba_token)
        return

    # The agent asked to hand the patient back to the deterministic flow:
    # non-destructive menu return, or re-entry at a confirmed doctor's
    # greeting + services. Mirrors the calendar sentinel short-circuit above.
    if reply_text == SHOW_MAIN_MENU_SENTINEL:
        await _handle_show_main_menu(
            reply, tenant, flow_professionals, patient_wa, redis=redis, waba_token=waba_token
        )
        return
    if reply_text.startswith(SELECT_PROFESSIONAL_SENTINEL_PREFIX):
        await _handle_select_professional(
            reply,
            reply_text,
            tenant,
            flow_snapshot,
            flow_professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    if reply_text.startswith(MANAGE_APPOINTMENT_SENTINEL_PREFIX):
        action = reply_text[len(MANAGE_APPOINTMENT_SENTINEL_PREFIX) :]
        await _handle_manage_appointment(
            reply,
            action,
            tenant,
            flow_professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return

    bubbles = parse(reply_text)
    if not bubbles:
        logger.warning(
            "worker_bot_reply_empty_after_parse",
            conversation_id=str(reply.conversation_id),
        )
        return

    await _dispatch_bubbles(reply, bubbles, tenant=tenant, waba_token=waba_token)


def _should_inject_post_consult_knowledge(
    knowledge: str | None,
    opening_state: PatientOpeningState | None,
    flow_state: FlowState | None,
    delegated_to_llm: bool,
) -> bool:
    """Whether THIS turn's system prompt should carry post_consult_knowledge.

    Pure gate over already-resolved turn facts (see the orchestration in
    `_send_bot_reply`) - does no I/O itself. `knowledge` blank/None always
    wins (nothing to inject regardless of state). Otherwise the turn
    qualifies when the patient just had a consult, the conversation is
    already in full-LLM ("Outro") mode, or the deterministic router just
    delegated this very turn to the LLM.
    """
    if not (knowledge or "").strip():
        return False
    return (
        opening_state is PatientOpeningState.JUST_HAD_CONSULT
        or flow_state is FlowState.LLM
        or delegated_to_llm
    )


def _should_inject_appointment_context(
    future_appointments: list[dict] | None,
    flow_state: FlowState | None,
    delegated_to_llm: bool,
) -> bool:
    """Whether THIS turn's system prompt should carry the appointment context block.

    Pure gate over already-resolved turn facts (see the orchestration in
    `_send_bot_reply`) - does no I/O itself. Mirrors
    `_should_inject_post_consult_knowledge`'s shape: an empty/None
    `future_appointments` always loses (nothing to render regardless of state
    - see `_appointment_context_text`). Otherwise the turn qualifies when the
    conversation is already in full-LLM ("Outro") mode, or the deterministic
    router just delegated THIS turn to the LLM.
    """
    if not future_appointments:
        return False
    return flow_state is FlowState.LLM or delegated_to_llm


def _appointment_context_text(
    future_appointments: list[dict],
    tz_name: str | None,
    professional_names: dict[str, str],
    appointment_types: list[RuntimeAppointmentType],
) -> str | None:
    """Render the per-turn "consultas marcadas" block for the LLM prompt.

    Pure formatting over already-resolved data (see
    `_should_inject_appointment_context` for the gate and `_send_bot_reply`
    for the orchestration) - no DB access, and no appointment content is ever
    logged from here (only counts are logged elsewhere, e.g.
    `resolve_patient_opening_state`). Returns None (never "") when
    `future_appointments` is empty, so the caller can tell "nothing to
    inject" apart from "injected an empty string" the same way
    `ai/prompts.py::_format_post_consult_knowledge` etc. do with a falsy check.

    `future_appointments` must be nearest-first (see
    `patient_context.load_upcoming_appointments`). The NEAREST one (index 0)
    gets the fuller "Próxima consulta" line (service, doctor when resolvable,
    price when its service matches a catalog entry) plus an "Orientações"
    line when that matched entry has `requirements`. Every OTHER appointment
    gets one brief "when — service[ — doctor]" line, mirroring the greeting's
    own brief-list rendering (`_adapt_greeting_has_upcoming`).

    `professional_names` is id -> display name, expected to be built from the
    ACTIVE-professionals roster (`flow_professionals` in `_send_bot_reply`):
    an appointment whose owner is no longer active simply renders with no
    doctor name (`_appointment_doctor_name` degrades to None on a miss).
    `appointment_types` is `TenantRuntimeConfig.appointment_types`, matched
    against the nearest appointment's stored service name by casefold/strip.
    """
    if not future_appointments:
        return None

    nearest = future_appointments[0]
    when = _format_appointment_when(nearest["start_at"], tz_name)
    service_name = nearest.get("appointment_type") or "Consulta"
    doctor = _appointment_doctor_name(nearest, professional_names)
    matched = next(
        (
            t
            for t in appointment_types
            if t.name.strip().casefold() == service_name.strip().casefold()
        ),
        None,
    )

    nearest_line = f"Próxima consulta: {when} — {service_name}"
    if doctor:
        nearest_line += f" — {doctor}"
    if matched and matched.price:
        nearest_line += f" — {matched.price}"

    lines = [nearest_line]
    if matched and matched.requirements:
        lines.append("Orientações: " + "; ".join(matched.requirements))

    for appt in future_appointments[1:]:
        appt_when = _format_appointment_when(appt["start_at"], tz_name)
        line = f"{appt_when} — {appt.get('appointment_type') or 'Consulta'}"
        appt_doctor = _appointment_doctor_name(appt, professional_names)
        if appt_doctor:
            line += f" — {appt_doctor}"
        lines.append(line)

    return "\n".join(lines)


def _label_match_body(body: str | None, label: str) -> bool:
    """True when `body` equals `label` or its 20-char button truncation."""
    target = (body or "").strip().casefold()
    return bool(target) and (
        target == label.strip().casefold() or target == label[:20].strip().casefold()
    )


def _flow_turn_calendar(
    conv_snapshot: SimpleNamespace,
    tenant_config,
    flow_calendar: CalendarService | None,
) -> CalendarService | None:
    """The calendar the flow router should use for THIS turn.

    With a professional selected, ONLY that professional's resolved calendar
    counts: when its resolution failed (None), the router degrades to the LLM
    (its calendar-needing steps delegate on None) instead of silently listing
    or booking on the tenant-level agenda. Without a selection, the tenant
    calendar is used exactly as before.
    """
    if getattr(conv_snapshot, "flow_selected_professional_id", None) is not None:
        return flow_calendar
    return CalendarService.from_tenant_config(tenant_config) if tenant_config else None


def _manage_owner_calendar_target(
    flow_state: FlowState | None,
    flow_managing_appointment_id: UUID | None,
    upcoming_appointments: list[dict] | None,
    professional_rows: list[Professional] | None,
) -> Professional | Literal["tenant"] | None:
    """Whose calendar owns the appointment being managed this turn (pure, no I/O).

    Resolves the conversation's manage-flow target (`flow_managing_appointment_id`)
    against the `upcoming_appointments` dicts already loaded for this turn (see
    `wants_upcoming_appointments` above - it guarantees they are loaded whenever
    this matters) and the tenant's active-professional rows. Returns:

      - the owning `Professional` ORM row, when the managed appointment's
        `professional_id` matches one of `professional_rows`;
      - the literal "tenant", when the managed appointment has no
        `professional_id` (booked at the tenant level, not with a specific
        doctor);
      - None when this isn't a manage-with-target turn (not MANAGE_BOOKING, no
        id set yet, or the id can't be resolved in `upcoming_appointments`) -
        the caller falls back to its existing calendar resolution
        (`_flow_turn_calendar`).

    Deliberately ignores `flow_selected_professional_id` (the SERVICE_CATALOG
    booking selection) - a stale doctor pick from an earlier booking flow must
    never decide which agenda a cancel/reschedule acts on.
    """
    if flow_state != FlowState.MANAGE_BOOKING or flow_managing_appointment_id is None:
        return None
    target = str(flow_managing_appointment_id)
    appt = next(
        (a for a in (upcoming_appointments or []) if str(a.get("id")) == target), None
    )
    if appt is None:
        return None
    professional_id = appt.get("professional_id")
    if not professional_id:
        return "tenant"
    return next(
        (p for p in (professional_rows or []) if str(p.id) == str(professional_id)), None
    )


# --------------------------------------------------------------------------
# Pix deposit money hooks (PROMPT S3) — reminder action buttons + the
# deterministic flow's own manage (cancel/reschedule) sub-flow.
# --------------------------------------------------------------------------

_APPOINTMENT_NOT_FOUND_TEXT = "Não encontrei essa consulta."


def _pix_retention_warning_line(tenant: Tenant, deposit) -> str:
    """The pt-BR retention-policy line for a cancellation landing inside the
    refund window. Shared VERBATIM by the deterministic flow's cancel-confirm
    question (`_apply_deposit_awareness`) and the reminder-button `apptcancel`
    warn (`_handle_action_button`) - same wording, different surrounding
    call-to-action text.
    """
    if tenant.pix_retention_policy == "partial":
        partial_amount = round(deposit.amount_cents * tenant.pix_partial_refund_percent / 100)
        return (
            f"Cancelamentos com menos de {tenant.pix_refund_window_hours}h têm reembolso "
            f"parcial de {tenant.pix_partial_refund_percent}% ({format_brl(partial_amount)})."
        )
    return (
        f"Cancelamentos com menos de {tenant.pix_refund_window_hours}h de antecedência não "
        "são reembolsáveis."
    )


def _hours_until_start(appointment: Appointment, now: datetime) -> float | None:
    """Hours between `now` and `appointment.start_at`, or None when unknown.

    Mirrors services/payments/deposit_lifecycle.py::on_appointment_cancelled's
    own window computation exactly (None reads as "outside the window" on
    both sides, same as that function) - duplicated locally rather than
    imported, matching this codebase's existing convention of small
    per-module `_as_utc`-adjacent helpers (see plugins/reminders.py).
    """
    if appointment.start_at is None:
        return None
    return (_as_utc(appointment.start_at) - now).total_seconds() / 3600


async def _calendar_for_appointment(
    session: AsyncSession,
    tenant: Tenant,
    tenant_config,
    appointment: Appointment,
) -> CalendarService | None:
    """The calendar that owns `appointment`: the booking professional's own
    resolved calendar, or the tenant-level one. Mirrors
    `_manage_owner_calendar_target` + `resolve_professional_calendar`'s
    resolution, but reads straight off the appointment ROW (no
    upcoming-appointments dict lookup needed — the caller already holds it).

    A booked-with professional that no longer resolves (deleted/deactivated)
    returns None rather than silently falling back to the TENANT calendar —
    mirroring `_manage_owner_calendar_target`'s own "don't guess, degrade"
    rule. Guessing wrong here is worse than skipping: `cancel_event` treats a
    404 as "already gone" (success), so calling it against the WRONG
    calendar could silently no-op while the event still lives on the
    professional's own agenda.
    """
    if appointment.professional_id is not None:
        professional = await session.get(Professional, appointment.professional_id)
        if professional is None:
            return None
        try:
            return await resolve_professional_calendar(
                session, tenant, professional, tenant_config=tenant_config
            )
        except Exception as exc:
            logger.warning(
                "action_button_professional_calendar_failed",
                error=str(exc),
                professional_id=str(appointment.professional_id),
            )
            return None
    return CalendarService.from_tenant_config(tenant_config) if tenant_config else None


async def _execute_appointment_cancel(
    session: AsyncSession,
    tenant: Tenant,
    tenant_config,
    appointment: Appointment,
    waba_token: str | None,
) -> str:
    """Cancel `appointment` (Calendar delete + status + deposit outcome).

    Mirrors flow_router._manage_cancel's happy path, for the button-driven
    carrier (no flow_state involved). Returns the patient-facing reply text,
    including the deposit `cancellation_notice` when there is one. The
    Calendar delete is best-effort: the platform row is the source of truth
    for a patient's cancel request even when Calendar is unreachable (same
    philosophy as deposit_lifecycle's own best-effort Calendar deletes).
    """
    if appointment.google_event_id:
        calendar = await _calendar_for_appointment(session, tenant, tenant_config, appointment)
        if calendar is not None:
            try:
                await calendar.cancel_event(appointment.google_event_id)
            except Exception as exc:
                logger.warning(
                    "action_button_calendar_cancel_failed",
                    error=str(exc),
                    appointment_id=str(appointment.id),
                )

    appointment.status = AppointmentStatus.CANCELLED
    text = "Consulta cancelada."
    try:
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment, waba_token=waba_token
        )
        if outcome is not None:
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
            if deposit is not None:
                notice = deposit_lifecycle.cancellation_notice(outcome, tenant, deposit)
                if notice:
                    text = f"{text} {notice}"
    except Exception as exc:
        logger.warning(
            "action_button_deposit_cancel_hook_failed",
            error=str(exc),
            appointment_id=str(appointment.id),
        )
    return text


async def _send_reschedule_limit_buttons(
    client: WhatsAppClient, to: str | None, appointment_id, count: int, limit: int
) -> None:
    """The keep-or-cancel message sent once an appointment's deposit has hit
    `pix_reschedule_limit`. Shared by the reminder-button `apptresched`
    handler and the deterministic flow's own reschedule-entry pre-check
    (`_apply_deposit_awareness`) so both surfaces say exactly the same thing
    and use the same apptconfirm|/apptcancel| button ids.
    """
    body = (
        f"Você já remarcou essa consulta {count}x — o limite com sinal é {limit}. "
        "Prefere manter o horário ou cancelar?"
    )
    await client.send_buttons(
        to,
        body,
        [
            (f"apptconfirm|{appointment_id}", "Manter horário"),
            (f"apptcancel|{appointment_id}", "Cancelar"),
        ],
    )


async def _apply_deposit_awareness(
    result: FlowRouterResult,
    tenant: Tenant | None,
    patient_wa: str | None,
    waba_token: str | None,
) -> FlowRouterResult:
    """Fold Pix-deposit awareness into a manage-flow `FlowRouterResult`
    BEFORE it is persisted/dispatched by `_apply_flow_result` (money hooks,
    PROMPT S3 section 4). Two moments, both reached from every entry path
    (direct menu tap, LLM sentinel hand-back, or a reminder-button
    preselection) since they all funnel through `_apply_flow_result`:

      - STEP_MANAGE_CANCEL_CONFIRM: prepend the retention-policy warning
        (`_pix_retention_warning_line`, same wording as the reminder-button
        `apptcancel` warn) to the Sim/Não question when the target has a PAID
        deposit inside the refund window. The Sim/Não buttons themselves are
        untouched — tapping "Sim" still runs `_manage_cancel`, which this
        module's cancel-site hook (below, in `_apply_flow_result`) makes
        deposit-aware too.
      - a freshly-targeted STEP_MANAGE_DAY (a reschedule was just begun):
        when the target is AT/OVER `pix_reschedule_limit`, replace the
        day-ask with the SAME keep-or-cancel button message
        (`_send_reschedule_limit_buttons`) the reminder's own blocked-
        reschedule reply uses — sent directly here (no Bubble type carries
        custom button ids) — and the flow is reset to MENU so those buttons
        (routed by id, independent of flow_state — see
        schemas/webhook.py::extract_action_button) are the only way forward.
        Non-incrementing: this is a PRE-check, exactly like the button
        carrier's own (`_handle_action_button`) — `register_reschedule` only
        ever runs at actual completion (this module's reschedule-site hook).

    Implemented HERE, not threaded into flow_router.py's pure functions:
    that module's own docstring commits it to doing no DB I/O of its own —
    this keeps that invariant at the cost of one extra short-lived
    session/query for these two specific turns. Every other turn (including
    STEP_MANAGE_DAY retries for an UNBLOCKED target — cheap, idempotent,
    re-checked each time rather than tracked) returns `result` unchanged.
    """
    if (
        result.action != "reply"
        or tenant is None
        or result.flow_managing_appointment_id is None
        or result.flow_step not in (STEP_MANAGE_CANCEL_CONFIRM, STEP_MANAGE_DAY)
    ):
        return result

    appointment_id = result.flow_managing_appointment_id
    warning: str | None = None
    limit_count: int | None = None
    limit_value: int | None = None

    async with async_session_factory() as session:
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment_id)

        if result.flow_step == STEP_MANAGE_CANCEL_CONFIRM:
            if deposit is None or deposit.status != PixDepositStatus.PAID:
                return result
            appointment = await session.get(Appointment, appointment_id)
            if appointment is None:
                return result
            hours_until = _hours_until_start(appointment, datetime.now(UTC))
            if hours_until is None or hours_until > tenant.pix_refund_window_hours:
                return result
            warning = _pix_retention_warning_line(tenant, deposit)
        else:  # STEP_MANAGE_DAY
            if deposit is None or deposit.reschedule_count < tenant.pix_reschedule_limit:
                return result
            limit_count = deposit.reschedule_count
            limit_value = tenant.pix_reschedule_limit

    if warning is not None:
        if result.bubbles:
            result.bubbles[0].body = f"{warning}\n\n{result.bubbles[0].body}"
        return result

    client = (
        WhatsAppClient.for_tenant(tenant, waba_token) if tenant is not None else WhatsAppClient()
    )
    await _send_reschedule_limit_buttons(
        client, patient_wa, appointment_id, limit_count, limit_value
    )
    result.bubbles = []
    result.flow_state = FlowState.MENU
    result.flow_step = None
    result.flow_managing_appointment_id = None
    return result


async def _handle_action_button(
    reply: _ReplyContext, action: str, appointment_id: str, redis=None
) -> None:
    """Handle a tap on a reminder's deposit-aware action button.

    Runs before handover/flow/LLM routing (see `_persist_inbound_message`):
    these are structured, unambiguous commands tied to one appointment id,
    not a free-form conversational turn, so they fire even while a human has
    taken the conversation over — and they leave `Conversation.flow_state`
    untouched, except `apptresched`'s reschedule-entry branch, which sets its
    own (mirroring a direct "Remarcar" tap).

    Every lookup is scoped by the CALLER'S tenant_id (resolved from
    `reply.conversation_id`, never from the payload); an appointment id that
    resolves to nothing — or to another tenant's row — gets the same polite
    miss as a stale/expired one. The payload's appointment id is NEVER
    trusted on its own (see schemas/webhook.py::extract_action_button).
    """
    if reply.conversation_id is None:
        return
    try:
        appt_uuid = UUID(appointment_id)
    except ValueError:
        return  # already validated by extract_action_button; defensive only

    # Set only on the "enter the reschedule sub-flow" path (apptresched,
    # under the limit) - handled AFTER this session closes, mirroring
    # _handle_manage_appointment's own short-read-session-then-handoff shape.
    reschedule_handoff: tuple[Tenant, list[dict], list, str | None] | None = None

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        waba_token = await get_waba_token(session, tenant.id)
        client = WhatsAppClient.for_tenant(tenant, waba_token)

        appointment = await session.scalar(
            select(Appointment).where(
                Appointment.id == appt_uuid, Appointment.tenant_id == tenant.id
            )
        )
        if appointment is None:
            await client.send_text_message(to=reply.patient_wa_id, body=_APPOINTMENT_NOT_FOUND_TEXT)
            return

        if action == "apptconfirm":
            now = datetime.now(UTC)
            is_future = appointment.start_at is not None and _as_utc(appointment.start_at) > now
            if (
                appointment.status in (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)
                and is_future
            ):
                appointment.status = AppointmentStatus.CONFIRMED
                when = _format_appointment_when(appointment.start_at, tenant.timezone)
                text = f"Presença confirmada! Até {when}."
            else:
                text = "Essa consulta não está mais ativa."
            await session.commit()
            await client.send_text_message(to=reply.patient_wa_id, body=text)
            return

        if action == "apptcancel":
            tenant_config = await load_tenant_config(session, tenant)
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
            hours_until = _hours_until_start(appointment, datetime.now(UTC))
            inside_window = (
                deposit is not None
                and deposit.status == PixDepositStatus.PAID
                and hours_until is not None
                and hours_until <= tenant.pix_refund_window_hours
            )
            if inside_window:
                warning = _pix_retention_warning_line(tenant, deposit)
                body = f"{warning} Cancelar mesmo assim ou prefere reagendar?"
                await client.send_buttons(
                    reply.patient_wa_id,
                    body,
                    [
                        (f"apptcancelyes|{appointment.id}", "Cancelar mesmo assim"),
                        (f"apptresched|{appointment.id}", "Reagendar"),
                    ],
                )
                return
            text = await _execute_appointment_cancel(
                session, tenant, tenant_config, appointment, waba_token
            )
            await session.commit()
            await client.send_text_message(to=reply.patient_wa_id, body=text)
            return

        if action == "apptcancelyes":
            tenant_config = await load_tenant_config(session, tenant)
            text = await _execute_appointment_cancel(
                session, tenant, tenant_config, appointment, waba_token
            )
            await session.commit()
            await client.send_text_message(to=reply.patient_wa_id, body=text)
            return

        if action == "apptresched":
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
            if deposit is not None and deposit.reschedule_count >= tenant.pix_reschedule_limit:
                await _send_reschedule_limit_buttons(
                    client,
                    reply.patient_wa_id,
                    appointment.id,
                    deposit.reschedule_count,
                    tenant.pix_reschedule_limit,
                )
                return
            if not flows_enabled(tenant):
                # Patient self-service reschedule (deterministic OR
                # LLM-mediated - see ai/tools.py's ManageAppointmentRequested)
                # is a flow-only capability throughout this codebase; there is
                # no non-flow equivalent to hand off to.
                await client.send_text_message(
                    to=reply.patient_wa_id,
                    body="Para remarcar essa consulta, entre em contato com a nossa equipe.",
                )
                return
            if appointment.patient_id is None:
                await client.send_text_message(
                    to=reply.patient_wa_id, body=_APPOINTMENT_NOT_FOUND_TEXT
                )
                return
            appointments = await load_upcoming_appointments(
                session, tenant.id, appointment.patient_id
            )
            professional_rows = await list_active_professionals(session, tenant.id)
            professionals = [
                SimpleNamespace(
                    id=p.id,
                    name=p.name,
                    specialty=p.specialty,
                    about=p.about,
                    context_doctor_message=p.context_doctor_message,
                    appointment_types=p.appointment_types,
                )
                for p in professional_rows
            ]
            reschedule_handoff = (tenant, appointments, professionals, waba_token)

    if reschedule_handoff is not None:
        handoff_tenant, appointments, professionals, handoff_waba_token = reschedule_handoff
        result = enter_manage_action(
            "reschedule", handoff_tenant, appointments, professionals, preselected_id=appt_uuid
        )
        await _apply_flow_result(
            reply,
            result,
            reply.patient_wa_id,
            redis=redis,
            tenant=handoff_tenant,
            waba_token=handoff_waba_token,
        )


# Fixed pt-BR degrade per known greeting-button action, for a tenant with
# flows disabled (flow_router.flows_enabled) - see
# _handle_greeting_button_unavailable. There is no non-flow equivalent to
# hand these off to (same acknowledged limitation as _handle_action_button's
# apptresched-while-flows-disabled branch above), so the reply is a fixed
# "contact us" message, never the LLM. Any suffix not in this dict (a legacy
# pre-deploy numeric id, or anything else `extract_greeting_button` didn't
# recognize) gets the generic default below.
_GREETING_ACTION_UNAVAILABLE_TEXT: dict[str, str] = {
    "agendar": "Para agendar uma consulta, entre em contato com a nossa equipe.",
    "remarcar": "Para remarcar sua consulta, entre em contato com a nossa equipe.",
    "cancelar": "Para cancelar sua consulta, entre em contato com a nossa equipe.",
}
_GREETING_ACTION_UNAVAILABLE_DEFAULT = (
    "Não consigo processar esse pedido automaticamente. "
    "Entre em contato com a nossa equipe, por favor."
)


async def _handle_greeting_button_unavailable(reply: _ReplyContext, suffix: str) -> None:
    """Deterministic degrade for a greeting-button tap a flows-disabled
    tenant can't fulfil (see `_persist_inbound_message`'s greeting-button
    short-circuit, which is the only place that sets
    `reply.greeting_button_unavailable`). A fully self-contained turn (its
    own tenant lookup, its own reply), mirroring `_handle_action_button`'s
    shape: never falls through to the entitlement gate, deterministic flow,
    or LLM.
    """
    if reply.conversation_id is None:
        return
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        waba_token = await get_waba_token(session, tenant.id)
        client = WhatsAppClient.for_tenant(tenant, waba_token)
    text = _GREETING_ACTION_UNAVAILABLE_TEXT.get(suffix, _GREETING_ACTION_UNAVAILABLE_DEFAULT)
    await _send_simple_text(reply.patient_wa_id, text, client=client)


async def _run_flow(
    reply: _ReplyContext,
    conv_snapshot: SimpleNamespace,
    tenant_snapshot: SimpleNamespace,
    tenant_config,
    patient_name: str | None,
    patient_wa: str | None,
    upcoming_appointments: list[dict] | None = None,
    redis=None,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
    professionals: list | None = None,
    flow_calendar: CalendarService | None = None,
    manage_calendar: CalendarService | None = None,
) -> bool:
    """Run the deterministic flow router for this turn.

    `conv_snapshot`/`tenant_snapshot` are detached plain copies of the flow-
    relevant fields (the router does no DB I/O). Returns True when the turn was
    fully handled (bubbles sent, or handed off on a calendar outage); False to
    fall through to the LLM agent. `tenant`/`waba_token` (already loaded by the
    caller) are threaded through to whichever send path fires.
    `professionals`/`flow_calendar` are the multi-doctor context loaded by
    `_send_bot_reply` (active snapshot + the selected professional's calendar).
    `manage_calendar` is the manage (cancel/reschedule) sub-flow's owning
    calendar (`_manage_owner_calendar_target`) - used INSTEAD of
    `_flow_turn_calendar` whenever this turn is already inside MANAGE_BOOKING
    with a target set, deliberately ignoring `flow_selected_professional_id`
    (a stale booking-flow selection must never decide the manage agenda).
    """
    if (
        getattr(conv_snapshot, "flow_state", None) == FlowState.MANAGE_BOOKING
        and getattr(conv_snapshot, "flow_managing_appointment_id", None) is not None
    ):
        calendar = manage_calendar
    else:
        calendar = _flow_turn_calendar(conv_snapshot, tenant_config, flow_calendar)
    try:
        result = await route(
            conv_snapshot,
            tenant_snapshot,
            calendar,
            reply.inbound_body,
            patient_name,
            upcoming_appointments=upcoming_appointments,
            professionals=professionals,
        )
    except Exception as exc:
        logger.warning(
            "worker_flow_router_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )
        return False

    return await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )


async def _apply_flow_result(
    reply: _ReplyContext,
    result: FlowRouterResult,
    patient_wa: str | None,
    redis=None,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
) -> bool:
    """Persist a FlowRouterResult (+ any appointment) and dispatch its bubbles.

    Shared by the live flow router (`_run_flow`), the returning-patient
    resume/reset path, and every LLM-hand-back re-entry
    (`_handle_show_main_menu`/`_handle_select_professional`/
    `_handle_manage_appointment`) — i.e. the ONE seam every manage-flow
    result passes through, which is why the Pix-deposit hooks below
    (`_apply_deposit_awareness` up front, the cancel/reschedule money hooks
    inside the persist txn) live HERE rather than in each individual caller.
    Returns True when the turn was fully handled (bubbles sent, or handed off
    on a calendar outage); False for `delegate_llm`.
    """
    result = await _apply_deposit_awareness(result, tenant, patient_wa, waba_token)

    # Persist the new flow state (+ any booked appointment) in one short txn.
    persisted = True
    booked_appointment: Appointment | None = None
    cancellation_note: str | None = None
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
                    conv.flow_selected_professional_id = result.flow_selected_professional_id
                    conv.flow_selected_insurance = result.flow_selected_insurance
                    conv.flow_managing_appointment_id = result.flow_managing_appointment_id
                    if result.appointment:
                        booked_appointment = Appointment(
                            tenant_id=conv.tenant_id,
                            patient_id=conv.patient_id,
                            conversation_id=conv.id,
                            phone=patient_wa,
                            status=AppointmentStatus.SCHEDULED,
                            **result.appointment,
                        )
                        session.add(booked_appointment)
                    # Cancel/reschedule mirror the calendar action onto the
                    # platform row, scoped by tenant_id (google_event_id is
                    # indexed but not globally unique). Best-effort: the calendar
                    # is the source of truth, so a stale row never blocks the reply.
                    if result.appointment_cancel_id:
                        await session.execute(
                            update(Appointment)
                            .where(
                                Appointment.google_event_id == result.appointment_cancel_id,
                                Appointment.tenant_id == conv.tenant_id,
                            )
                            .values(status=AppointmentStatus.CANCELLED)
                        )
                        # Money hook: resolve the deposit's outcome for this
                        # cancellation and carry the honest notice through to
                        # the reply dispatched below (PROMPT S3 section 4).
                        if tenant is not None:
                            cancelled_appt = await session.scalar(
                                select(Appointment).where(
                                    Appointment.google_event_id
                                    == result.appointment_cancel_id,
                                    Appointment.tenant_id == conv.tenant_id,
                                )
                            )
                            if cancelled_appt is not None:
                                outcome = await deposit_lifecycle.on_appointment_cancelled(
                                    session,
                                    tenant=tenant,
                                    appointment=cancelled_appt,
                                    waba_token=waba_token,
                                )
                                if outcome is not None:
                                    cancelled_deposit = (
                                        await deposit_lifecycle.get_deposit_for_appointment(
                                            session, cancelled_appt.id
                                        )
                                    )
                                    if cancelled_deposit is not None:
                                        cancellation_note = deposit_lifecycle.cancellation_notice(
                                            outcome, tenant, cancelled_deposit
                                        )
                    if result.appointment_reschedule:
                        resched = result.appointment_reschedule
                        await session.execute(
                            update(Appointment)
                            .where(
                                Appointment.google_event_id == resched["google_event_id"],
                                Appointment.tenant_id == conv.tenant_id,
                            )
                            .values(
                                start_at=resched["start_at"],
                                end_at=resched["end_at"],
                                status=AppointmentStatus.RESCHEDULED,
                            )
                        )
                        # Money hook: count this reschedule against the
                        # deposit's limit. Non-crashing on a race (entry was
                        # already pre-checked by _apply_deposit_awareness /
                        # the button carrier's own check) — never unwind an
                        # already-persisted reschedule over a counter race.
                        if tenant is not None:
                            resched_appt = await session.scalar(
                                select(Appointment).where(
                                    Appointment.google_event_id
                                    == resched["google_event_id"],
                                    Appointment.tenant_id == conv.tenant_id,
                                )
                            )
                            if resched_appt is not None:
                                allowed, _count = await deposit_lifecycle.register_reschedule(
                                    session, tenant=tenant, appointment=resched_appt
                                )
                                if not allowed:
                                    logger.warning(
                                        "pix_reschedule_limit_race",
                                        tenant_id=str(tenant.id),
                                        appointment_id=str(resched_appt.id),
                                    )
    except Exception as exc:
        persisted = False
        logger.error(
            "worker_flow_persist_failed",
            error=str(exc),
            conversation_id=str(reply.conversation_id),
        )

    # Fire-and-forget: enqueue post_booking plugin hooks off the hot path,
    # mirroring ai/tools.py:_persist_appointment's agent-path enqueue — see
    # plugins/post_booking.py. Only when the appointment row actually made it
    # to the DB (never on a persist failure) and `tenant` is loaded (always
    # true here — the flow engine only runs once `tenant` is resolved).
    if booked_appointment is not None and persisted and tenant is not None:
        await enqueue_post_booking_hooks(
            redis, tenant.id, booked_appointment.id, source="flow"
        )

    if result.action == "calendar_unavailable":
        await _handle_calendar_unavailable(reply, redis=redis, tenant=tenant, waba_token=waba_token)
        return True
    # A booking was made on Google Calendar but recording it failed: do NOT
    # tell the patient it is confirmed. Hand off to a human to reconcile the
    # (now orphaned) event instead of claiming success or risking a re-book.
    if result.appointment is not None and not persisted:
        await _handle_calendar_unavailable(reply, redis=redis, tenant=tenant, waba_token=waba_token)
        return True
    if result.action == "reply":
        if result.bubbles:
            if cancellation_note:
                result.bubbles[-1].body = f"{result.bubbles[-1].body}\n\n{cancellation_note}"
            await _dispatch_bubbles(reply, result.bubbles, tenant=tenant, waba_token=waba_token)
        return True
    return False  # delegate_llm


async def _dispatch_bubbles(
    reply: _ReplyContext,
    bubbles: list,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
) -> int:
    """Send each bubble in order, recording outbound messages. Returns count sent.

    MVP: no retry. A transient send failure stops the turn; bubbles already sent
    are recorded so the LLM history stays consistent on the next inbound.
    `tenant`/`waba_token` select the per-tenant WhatsApp client (see
    WhatsAppClient.for_tenant); `tenant=None` falls back to the single-tenant
    env scaffold, same as before this parameter existed.
    """
    client = (
        WhatsAppClient.for_tenant(tenant, waba_token) if tenant is not None else WhatsAppClient()
    )
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


# Semantic payload-id suffix for each of the greeting's FIXED, product-defined
# action buttons (see _greeting_buttons_for) - used by _send_greeting below and
# read back by schemas/webhook.py::extract_greeting_button. Deliberately only
# covers those four code-constant labels; anything else (reactivation's
# Sim/Não, tenant-configurable) is NOT in this map on purpose - see
# _send_greeting's comment.
_GREETING_ACTION_IDS: dict[str, str] = {
    LABEL_BOOK: "agendar",
    LABEL_RESCHEDULE: "remarcar",
    LABEL_CANCEL_APPT: "cancelar",
    LABEL_OTHER: "outro",
}


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
            # The label still drives route()'s dispatch (as with every other
            # deterministic tap - see extract_inbound_body), so the id itself
            # doesn't have to. It carries semantic meaning anyway: a KNOWN
            # fixed action label (_GREETING_ACTION_IDS - the current
            # _greeting_buttons_for trio) gets a matching "greeting|<action>"
            # id, which `extract_greeting_button` uses to give a flows-
            # disabled tenant's tap a deterministic degrade instead of
            # route()'s unconditional LLM delegation. Any OTHER label - e.g.
            # the reactivation Sim/Não prompt, which is tenant-configurable
            # free text - gets a positional "reactivation|<index>" id
            # instead, deliberately never "greeting|<number>", so it can
            # never be confused with a legacy pre-deploy greeting-button tap
            # (which used exactly that numeric shape) by
            # `extract_greeting_button`.
            buttons = [
                (
                    f"greeting|{_GREETING_ACTION_IDS[label]}"
                    if label in _GREETING_ACTION_IDS
                    else f"reactivation|{index}",
                    label,
                )
                for index, label in enumerate(reply.greeting_buttons)
            ]
            result = await client.send_buttons(to=reply.patient_wa_id, body=body, buttons=buttons)
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


async def _send_simple_text(to: str, body: str, client: WhatsAppClient | None = None) -> None:
    """Send a single plain-text message, swallowing send errors (MVP: no retry).

    `client=None` falls back to the single-tenant env scaffold (unchanged
    default); pass a `WhatsAppClient.for_tenant(...)` instance to send on a
    specific tenant's own WhatsApp number/token.
    """
    try:
        await (client or WhatsAppClient()).send_text_message(to=to, body=body)
    except Exception as exc:
        logger.error("worker_simple_text_send_failed", error=str(exc), to=to)


async def _handle_calendar_unavailable(
    reply: _ReplyContext,
    redis=None,
    tenant: Tenant | None = None,
    waba_token: str | None = None,
) -> None:
    """Degrade gracefully on a calendar outage: notify patient + hand off + alert tenant.

    `tenant`/`waba_token` (already loaded by the caller, if any) select the
    per-tenant WhatsApp client for the patient-facing message below. The
    clinic-owner alert further down always re-fetches its own tenant row
    (inside the same transaction as the handover-state update) so it sees the
    freshest `contact_email`, independent of what the caller passed in.
    """
    logger.error("worker_calendar_unavailable", conversation_id=str(reply.conversation_id))
    alert_tenant: Tenant | None = None
    if reply.conversation_id is not None:
        try:
            async with async_session_factory() as session:
                async with session.begin():
                    conversation = await session.get(Conversation, reply.conversation_id)
                    if conversation is not None:
                        await HandoverManager(session).set_human_active(conversation)
                        alert_tenant = await session.get(Tenant, conversation.tenant_id)
        except Exception as exc:
            logger.error(
                "worker_calendar_unavailable_handover_failed",
                error=str(exc),
                conversation_id=str(reply.conversation_id),
            )

    client = WhatsAppClient.for_tenant(tenant, waba_token) if tenant is not None else None
    await _send_simple_text(reply.patient_wa_id, CALENDAR_UNAVAILABLE_MESSAGE, client=client)

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


async def _handle_show_main_menu(
    reply: _ReplyContext,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """Non-destructive menu return (the agent's show_main_menu tool).

    Unlike the dev-only /menu command, NOTHING is deleted: `_apply_flow_result`
    resets the flow fields to MENU (its unconditional writes also clear
    flow_selected_professional_id / flow_selected_insurance) and the effective
    menu bubbles go out. History and the patient row stay untouched.
    """
    if tenant is None:
        logger.warning(
            "worker_show_main_menu_without_tenant",
            conversation_id=str(reply.conversation_id),
        )
        return
    result = FlowRouterResult(
        action="reply",
        bubbles=[
            MenuBubble(
                body=menu_label(tenant),
                labels=menu_buttons_for(tenant, len(professionals or []) > 1),
            )
        ],
        flow_state=FlowState.MENU,
    )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )


async def _handle_select_professional(
    reply: _ReplyContext,
    reply_text: str,
    tenant: Tenant | None,
    flow_snapshot: tuple[SimpleNamespace, SimpleNamespace] | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: re-enter the deterministic flow at the chosen doctor.

    `select_professional_and_continue` already resolved the name against the
    ACTIVE roster, so the id in the sentinel should re-resolve here; if it
    doesn't (deactivated in the same instant, or a malformed sentinel), fall
    back to the plain menu instead of dropping the turn.
    """
    raw_id = reply_text[len(SELECT_PROFESSIONAL_SENTINEL_PREFIX) :]
    professional = None
    try:
        professional_id = UUID(raw_id)
    except ValueError:
        logger.error("worker_select_professional_bad_sentinel", raw=raw_id[:64])
    else:
        professional = next(
            (p for p in professionals or [] if p.id == professional_id), None
        )
    tenant_snapshot = flow_snapshot[1] if flow_snapshot is not None else tenant
    if professional is None or tenant_snapshot is None:
        logger.warning(
            "worker_select_professional_unresolved",
            conversation_id=str(reply.conversation_id),
        )
        await _handle_show_main_menu(
            reply, tenant, professionals, patient_wa, redis=redis, waba_token=waba_token
        )
        return
    result = _enter_professional_services(professional, tenant_snapshot)
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )


async def _handle_manage_appointment(
    reply: _ReplyContext,
    action: str,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: re-enter the deterministic manage (cancel/reschedule) flow.

    `manage_existing_appointment` (ai/tools.py) already normalizes/validates
    `action` to "reschedule"/"cancel" before raising ManageAppointmentRequested,
    so an unrecognized suffix here means a malformed sentinel; fall back to the
    plain menu instead of dropping the turn, mirroring
    `_handle_select_professional`'s guard on a bad professional id. The tool
    itself is only ever exposed to flow-enabled tenants (see the `extra_tools`
    wiring in `_send_bot_reply`), so `tenant` being None or not
    `flows_enabled` here should not normally happen - guarded defensively with
    a count-only warning, same style as `_handle_show_main_menu`.

    Re-loads the patient's upcoming appointments FRESH in its own session
    (authoritative regardless of what this turn preloaded - the LLM may have
    taken several tool-call turns since) before handing off to
    `enter_manage_action`, the exact same deterministic entry a direct
    "Remarcar"/"Cancelar" button tap uses.
    """
    if action not in ("reschedule", "cancel"):
        logger.warning("worker_manage_appointment_bad_action", action=action[:32])
        await _handle_show_main_menu(
            reply, tenant, professionals, patient_wa, redis=redis, waba_token=waba_token
        )
        return
    if tenant is None or not flows_enabled(tenant):
        logger.warning(
            "worker_manage_appointment_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        return

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        patient_id = conversation.patient_id if conversation is not None else None
        if patient_id is None:
            logger.warning(
                "worker_manage_appointment_no_patient",
                conversation_id=str(reply.conversation_id),
            )
            return
        appointments = await load_upcoming_appointments(session, tenant.id, patient_id)

    result = enter_manage_action(action, tenant, appointments, professionals)
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )


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
        # Rows are (id, title) or (id, title, description) — see SlotsBubble.
        return await client.send_list(
            to=to,
            body=bubble.body,
            button_label=bubble.button_label,
            rows=[(row[0], row[1], row[2] if len(row) > 2 else None) for row in bubble.rows],
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
        labels = ", ".join(row[1] for row in bubble.rows)
        return f"{bubble.body}\n(opções: {labels})" if labels else bubble.body
    return bubble.body


# --------------------------------------------------------------------------
# Inbound audio transcription
# --------------------------------------------------------------------------


def _transcription_config() -> TranscriptionConfig:
    """Build the transcription-core config from env settings.

    Deliberately reads OPENAI_TRANSCRIPT_MODEL, never OPENAI_SECRETARIA_MODEL —
    the chat/agent model and the STT model must not cross-contaminate.
    """
    settings = get_settings()
    return TranscriptionConfig(
        openai_api_key=settings.OPENAI_API_KEY,
        openai_model=settings.OPENAI_TRANSCRIPT_MODEL,
        groq_api_key=settings.GROQ_API_KEY or None,
        primary=settings.AUDIO_TRANSCRIPTION_PRIMARY,
        domain_prompt=settings.AUDIO_DOMAIN_PROMPT or None,
        language="pt",
    )


async def _mark_audio_event_processed(message_id: str) -> None:
    """Claim `message_id` in the ProcessedEvent ledger without a full inbound persist.

    Used when a voice note fails permanently (rejected media) or transcribes
    with low confidence: the patient gets a clarification instead of a
    transcript, but the event must still be marked processed so a Meta
    redelivery never pays for STT again on the same wamid.
    """
    async with async_session_factory() as session:
        try:
            async with session.begin():
                if not await _event_already_processed(session, message_id):
                    session.add(ProcessedEvent(event_id=message_id))
        except IntegrityError:
            # A concurrent worker already claimed this event id.
            logger.info("worker_audio_duplicate_race", wam_id=message_id)


async def transcribe_audio_message(
    ctx: dict,
    *,
    media_id: str,
    phone_number_id: str | None,
    wa_id: str,
    message_id: str,
    patient_name: str | None = None,
) -> None:
    """arq job: transcribe one inbound WhatsApp voice note, then reply like text.

    Enqueued by the webhook POST handler (api/webhook.py) right alongside
    `process_webhook_event`, with a deliberately minimal payload —
    media_id/phone_number_id/wa_id/message_id/patient_name only, never the
    full webhook body. All download + STT + reply work happens here, off the
    request/response cycle (see `iter_audio_messages`).

    Error policy:
    - Permanent failures (`MediaTooLarge`, `NotAudio`) get the ProcessedEvent
      id claimed explicitly (`_mark_audio_event_processed`) and the patient
      receives `AUDIO_UNINTELLIGIBLE_MESSAGE`. Retrying a file that will
      never transcribe usefully would just burn another Graph API call.
    - A low-confidence (or empty) transcript is treated the same way: marked
      processed + the same clarification, WITHOUT ever reaching the LLM.
    - Any other `TranscriptionError` (e.g. `MediaFetchError`,
      `AllProvidersFailed`) is logged and re-raised so arq retries the job.
      The ProcessedEvent id is deliberately NOT claimed on this path, so a
      retry is safe. The media URL is re-fetched fresh from Meta on every
      attempt (short-lived Graph API URLs), so nothing is stale.
    - On success, the ProcessedEvent id is claimed by `_persist_inbound_message`
      itself — the exact same seam the text path uses — so it is claimed
      exactly once and a retried job never pays for STT twice.
    """
    # Single rate-limit increment for audio: _handle_patient_messages skips
    # audio messages entirely (see the guard added there), so this is the
    # only place an audio message's inbound rate limit is counted/checked.
    if await _is_rate_limited(ctx.get("redis"), phone_number_id, wa_id):
        logger.info("worker_audio_rate_limited", wa_id=wa_id)
        return

    async with async_session_factory() as session:
        async with session.begin():
            if await _event_already_processed(session, message_id):
                logger.info("worker_audio_duplicate", wam_id=message_id)
                return

            tenant = await _resolve_tenant(session, phone_number_id)
            if tenant is None:
                logger.error("worker_audio_tenant_unresolved", phone_number_id=phone_number_id)
                return

            # Snapshot plain values so they survive past this session's close
            # (expire_on_commit=False makes this safe for already-loaded
            # attributes too, but explicit snapshots keep intent obvious).
            tenant_is_active = tenant.is_active
            tenant_phone_number_id = tenant.phone_number_id
            # Decrypt inside the session (single seam); only the in-memory
            # value travels from here on - never logged, never returned.
            waba_token = await get_waba_token(session, tenant.id)

    # Tenant not activated yet: replicate today's zero-STT-spend behavior
    # exactly (single polite fallback, no conversation/message/LLM touched).
    if not tenant_is_active:
        reply = await _persist_inbound_message(
            phone_number_id=phone_number_id,
            wa_id=wa_id,
            patient_name=patient_name,
            wam_id=message_id,
            body=None,
        )
        if reply is not None:
            await _send_bot_reply(reply, redis=ctx.get("redis"))
        return

    token = waba_token or get_settings().META_ACCESS_TOKEN
    if not token:
        logger.error("worker_audio_no_token", tenant_id=str(tenant.id), wam_id=message_id)
        return

    # A ValueError here means TranscriptionConfig rejected the configured STT
    # model/provider combo - a misconfiguration, not a per-message failure.
    # Deliberately left to propagate loudly into the arq error logs.
    config = _transcription_config()

    # Built once, reused by whichever clarification path (if any) fires below.
    reply_client = WhatsAppClient(
        phone_number_id=phone_number_id or tenant_phone_number_id,
        access_token=token,
    )

    client = ctx.get("http_client")
    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient()
    try:
        try:
            result: TranscriptionResult = await transcribe_whatsapp_media(
                media_id,
                token,
                api_version=get_settings().META_GRAPH_API_VERSION,
                config=config,
                http_client=client,
            )
        except (MediaTooLarge, NotAudio) as exc:
            await _mark_audio_event_processed(message_id)
            await _send_simple_text(wa_id, AUDIO_UNINTELLIGIBLE_MESSAGE, client=reply_client)
            logger.info("worker_audio_rejected", wam_id=message_id, reason=type(exc).__name__)
            return
        except TranscriptionError:
            # MediaFetchError / AllProvidersFailed are usually transient (a
            # blip fetching from Meta, or every STT provider briefly down);
            # let arq retry. The media URL is re-fetched fresh next attempt.
            logger.warning("worker_audio_transcription_failed", wam_id=message_id)
            raise
    finally:
        if owns_client:
            await client.aclose()

    if result.is_low_confidence:
        await _mark_audio_event_processed(message_id)
        await _send_simple_text(wa_id, AUDIO_UNINTELLIGIBLE_MESSAGE, client=reply_client)
        logger.info(
            "worker_audio_low_confidence",
            wam_id=message_id,
            provider=result.provider_used,
            chars=result.char_count,
        )
        return

    # NEVER log result.text (the transcript itself) - char_count/provider only.
    logger.info(
        "worker_audio_transcribed",
        wam_id=message_id,
        provider=result.provider_used,
        chars=result.char_count,
    )
    reply = await _persist_inbound_message(
        phone_number_id=phone_number_id,
        wa_id=wa_id,
        patient_name=patient_name,
        wam_id=message_id,
        body=result.text,
    )
    if reply is not None:
        await _send_bot_reply(reply, redis=ctx.get("redis"))


# --------------------------------------------------------------------------
# Human echoes (Coexistence handover)
# --------------------------------------------------------------------------


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
                # _handle_history / _handle_smb_app_state_sync below.
                _mark_mode_resolved(tenant)

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
# Coexistence mode-resolution signals: history sync + business state sync
# --------------------------------------------------------------------------
#
# Neither handler ingests message/contact content (LGPD - contract v1 §10):
# only counts are ever logged, and only sync-progress / tenant timestamps are
# ever written to the DB. Both signal that Coexistence mode resolved for the
# tenant, exactly like `smb_message_echoes` above.


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


def _mark_connected(tenant: Tenant) -> Tenant:
    """Best-effort `connected_at` backstop: any webhook resolving to a known
    tenant implies its WhatsApp number is receiving live traffic.

    The primary setter is the internal whatsapp-connection endpoint
    (contract v1 §4 endpoint 2, api/internal_provisioning.py) - this only
    fires when that path was somehow skipped (e.g. a dev-seeded tenant, or a
    number reconnected by hand), so `connected_at` is never left NULL once
    real traffic exists. A no-op once already set.
    """
    if tenant.connected_at is None:
        tenant.connected_at = datetime.now(UTC)
    return tenant


async def _resolve_tenant(session: AsyncSession, phone_number_id: str | None) -> Tenant | None:
    """Find the tenant for an inbound event.

    Primary path (always on): exact match on `phone_number_id`. This is
    null-safe by construction against the now-NULLABLE `tenants.phone_number_id`
    (onboarding creates a tenant row before its WhatsApp number is connected,
    so several tenants may have a NULL phone_number_id at once) - the lookup
    only runs when `phone_number_id` is truthy, and `Tenant.phone_number_id ==
    <a non-empty string>` never matches a NULL column value in SQL, so a
    NULL-phone tenant can never be adopted here.

    MVP single-tenant scaffold (`settings.ALLOW_WEBHOOK_AUTOPROVISION`,
    default False): when no tenant matches and the flag is on, fall back to
    the configured META_PHONE_NUMBER_ID / auto-provision a tenant from env,
    exactly as the single-tenant dev flow always has. Production/multi-tenant
    deployments must leave this OFF - an unrecognized phone_number_id is then
    simply dropped (returns None), never adopted or fabricated.

    Every resolved tenant is passed through `_mark_connected` before it is
    returned (see that function's docstring).
    """
    if phone_number_id:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.phone_number_id == phone_number_id)
        )
        if tenant is not None:
            return _mark_connected(tenant)

    settings = get_settings()
    if not settings.ALLOW_WEBHOOK_AUTOPROVISION:
        return None

    configured = settings.META_PHONE_NUMBER_ID
    if phone_number_id and configured and phone_number_id != configured:
        # Unknown number - never auto-provision a foreign tenant.
        return None

    target = phone_number_id or configured
    if not target:
        return None

    tenant = await session.scalar(select(Tenant).where(Tenant.phone_number_id == target))
    if tenant is not None:
        return _mark_connected(tenant)

    tenant = Tenant(
        clinic_name="MVP Clinic",
        phone_number_id=target,
        # The single-tenant MVP scaffold is live by definition; without this the
        # new is_active gate would silently block the validated dev flow.
        is_active=True,
    )
    session.add(tenant)
    await session.flush()
    if settings.META_ACCESS_TOKEN:
        # Encrypted at rest from the very first write — never a plaintext column.
        await set_waba_token(session, tenant.id, settings.META_ACCESS_TOKEN)
    logger.info("worker_tenant_auto_provisioned", tenant_id=str(tenant.id))
    return _mark_connected(tenant)


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


# --------------------------------------------------------------------------
# Transactional email (onboarding lifecycle - contract v1 §4 endpoint 6)
# --------------------------------------------------------------------------


async def send_transactional_email(ctx: dict, template: str, to: str, variables: dict) -> None:
    """arq job: render + send one onboarding transactional email.

    Enqueued by `POST /internal/notifications/email`
    (api/internal_provisioning.py). Never raises: `send_transactional_email_message`
    already swallows every failure (disabled, unknown template, SMTP error)
    and returns a bool - this wrapper just adapts arq's `(ctx, ...)` calling
    convention and logs the outcome. Positional order (template BEFORE to)
    matches the contract's function signature exactly.
    """
    sent = await send_transactional_email_message(to=to, template=template, variables=variables)
    logger.info("worker_transactional_email_processed", template=template, sent=sent)


# --------------------------------------------------------------------------
# Cron jobs
# --------------------------------------------------------------------------


async def check_handover_timeouts(ctx: dict) -> None:
    """arq cron: hand stale HUMAN_ACTIVE conversations back to the bot.

    A conversation a human secretary has not touched within
    HANDOVER_TIMEOUT_MINUTES is flipped back to BOT_ACTIVE so the bot answers
    the patient's next message. Registered in arq_worker.WorkerSettings.
    """
    cutoff = datetime.now(UTC) - timedelta(minutes=get_settings().HANDOVER_TIMEOUT_MINUTES)
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
