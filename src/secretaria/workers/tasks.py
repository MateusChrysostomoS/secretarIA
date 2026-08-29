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
from arq import Retry
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
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
    run_agent,
)
from secretaria.ai.tools import manage_existing_appointment, start_guided_booking
from secretaria.config import get_settings
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger, wa_suffix
from secretaria.core.whatsapp_limits import truncate_button_label
from secretaria.models import (
    AnalyticsEvent,
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
    PixDeposit,
    PixDepositStatus,
    ProcessedEvent,
    Professional,
    RebookingDecline,
    Tenant,
    is_live_status,
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
from secretaria.services import cancellation_notice
from secretaria.services.appointment_status import (
    SOURCE_BUTTON,
    SOURCE_FLOW,
    log_status_transition,
)
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    booking_topology,
    sole_active_professional,
)
from secretaria.services.calendar import CalendarService
from secretaria.services.email import (
    send_calendar_alert,
    send_cancellation_escalation_alert,
    send_professional_config_incomplete_alert,
    send_transactional_email_message,
)
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.flow_router import (
    LABEL_BOOK,
    LABEL_CANCEL_APPT,
    LABEL_MANAGE_APPOINTMENT,
    LABEL_OTHER,
    LABEL_RESCHEDULE,
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_ESCAPE,
    STEP_MANAGE_DAY_RETRY,
    FlowRouterResult,
    MenuBubble,
    _enter_professional_services,
    classify_yes_no,
    enter_decline_reasons,
    enter_guided_booking,
    enter_manage_action,
    enter_rebooking,
    flows_enabled,
    llm_state_ttl_minutes,
    manage_label,
    menu_buttons_for,
    menu_label,
    reactivation_choice_buttons,
    reactivation_continue_prompt,
    reactivation_enabled,
    reactivation_gap_minutes,
    reactivation_prompt_enabled,
    rebooking_candidates,
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
from secretaria.services.service_catalog import (
    load_service_catalog,
    normalize as normalize_service_name,
    resolve_entries,
)
from secretaria.services.tenant_config import (
    RuntimeAppointmentType,
    active_appointment_types,
    get_waba_token,
    list_active_professionals,
    load_tenant_config,
    professional_appointment_types,
    professional_business_hours,
    resolve_professional_calendar,
    set_waba_token,
)
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import TenantWhatsAppCredentialMissing, WhatsAppClient

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

# Slash commands the patient can type to go back to the main menu. Matched
# case-insensitively against the trimmed message body.
#
# These are NON-DESTRUCTIVE (PROMPT_FIX_18). They used to route to a "dev
# reset" that deleted the patient row, their conversation, every message and
# their appointments — off a word a patient types by accident. That handler
# still exists, but only behind `REMOVE_CONTEXT_COMMAND` below; `/menu` and
# friends now do exactly what `ai/tools.py::show_main_menu` does: reset the
# transient flow fields and re-render the menu, touching nothing else.
_MENU_COMMANDS = frozenset({"/menu", "/reset", "/recomecar", "/recomeçar", "/inicio", "/início"})


def is_menu_command(body: str | None) -> bool:
    """True when the patient typed a `/menu`-style (non-destructive) command."""
    if not body:
        return False
    return body.strip().lower() in _MENU_COMMANDS


# The DESTRUCTIVE reset. Deliberately long, literal and self-describing: it is
# the one command nobody types by accident, so reaching it is unambiguously a
# deliberate operator gesture.
REMOVE_CONTEXT_COMMAND = "/dangerously-remove-context"


def is_remove_context_command(body: str | None) -> bool:
    """True ONLY for the exact `/dangerously-remove-context` string.

    EXACT match on purpose (PROMPT_FIX_18): no aliases, no case folding, no
    whitespace trimming, no prefix matching. The literal string IS the safety
    mechanism — accepting variations is what made `/menu` dangerous in the
    first place, and would hand the accident right back.
    """
    return body == REMOVE_CONTEXT_COMMAND


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
        logger.warning(
            "worker_rate_limit_check_failed", error=str(exc), wa_id_suffix=wa_suffix(wa_id)
        )
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
    # The tenant this turn resolved to. Carried explicitly because the
    # `service_unavailable` degrade below has NO conversation to look it up
    # from, and every outbound send must still go out on this tenant's own
    # WhatsApp number/token — never a global env fallback (PROMPT_FIX_21).
    tenant_id: UUID | None = None
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
    # Set when this inbound is a `/menu`-style command (see `is_menu_command`):
    # a NON-DESTRUCTIVE request to go back to the main menu. Handled by
    # `_handle_show_main_menu` from `_send_bot_reply`, i.e. only AFTER the
    # allowlist, tenant-active, handover, entitlement and plugin gates have
    # all been cleared like any other turn (PROMPT_FIX_18).
    menu_requested: bool = False


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
            logger.info("worker_rate_limited", wa_id_suffix=wa_suffix(msg.from_))
            continue

        contact = contacts.get(msg.from_)
        patient_name = contact.profile.name if contact and contact.profile else None
        body = extract_inbound_body(msg)
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
                # tenant-scoped appointment lookup and reply. The
                # allowlist guard above already covers this path too: it
                # returns before `action_button` is ever attached to a
                # `_ReplyContext`, so a non-allowlisted wa_id never reaches
                # `_handle_action_button` (dispatched from `_send_bot_reply`,
                # which only runs when `_persist_inbound_message` returns
                # non-None).
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

                # `/menu` (and /reset, /recomeçar, /inicio): a NON-DESTRUCTIVE
                # request to go back to the main menu (PROMPT_FIX_18). Nothing
                # is deleted: the patient row, their history, appointments,
                # consent events, Pix deposits and Google Calendar events are
                # all untouched. Only the transient flow fields move, and
                # `_apply_flow_result` (reached via `_handle_show_main_menu`)
                # does that write.
                #
                # Placed HERE deliberately:
                #   * AFTER the handover check, so an active human is NOT
                #     silently pre-empted - while a human owns the
                #     conversation, `/menu` is recorded like any other message
                #     and IGNORED by the bot (the return above already
                #     happened). It is never a way to take the bot back.
                #   * BEFORE the greeting / reactivation branches, so it is
                #     idempotent: two deliveries of `/menu` produce the same
                #     menu, never a greeting on one and a menu on the other.
                # The pending reactivation gate, if any, is consumed here too -
                # an explicit "take me to the menu" answers the "quer
                # continuar?" question by superseding it.
                if is_menu_command(body):
                    conversation.reactivation_origin = None
                    logger.info(
                        "conversation_menu_requested",
                        conversation_id=str(conversation.id),
                        tenant_id=str(tenant.id),
                        source="command",
                    )
                    return _ReplyContext(
                        conversation_id=conversation.id,
                        tenant_id=tenant.id,
                        patient_wa_id=wa_id,
                        inbound_body=body or "",
                        menu_requested=True,
                    )

                # Greeting-button tap this tenant can't fulfil deterministically:
                # flows are disabled for them, so route() would otherwise
                # delegate straight to the LLM (see flow_router.route()'s
                # top-of-function flows_enabled gate). A flows-ENABLED tenant's
                # tap is NOT special-cased here - it falls through to the
                # normal dispatch below, which route() already handles
                # deterministically (LABEL_BOOK/LABEL_MANAGE_APPOINTMENT/
                # LABEL_RESCHEDULE/LABEL_CANCEL_APPT, or a graceful "here's
                # the menu again" for a stale/legacy label route() doesn't
                # recognize). "Outro" is exempt for BOTH cohorts: it IS the
                # deliberate LLM hand-off, and for a flows-disabled tenant the
                # normal path below already goes straight to the LLM - exactly
                # what the button promises - so short-circuiting it to a
                # "contact us" degrade would break the one button that works
                # fine for them.
                if (
                    greeting_button is not None
                    and greeting_button != _GREETING_LLM_ESCAPE_SUFFIX
                    and not flows_enabled(tenant)
                ):
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

                # Universal floor on how long full LLM mode may last (see
                # `_expire_stale_llm_state`). Runs BEFORE the offer below, and
                # THAT ORDER IS LOAD-BEARING: the state must already be dropped
                # when "quer continuar?" goes out, so a patient who simply never
                # answers it still leaves LLM mode. Ask first and the prompt
                # becomes the only time-based exit again - the exact hole this
                # floor was added to close, just with a question instead of
                # silence. The pre-expiry state is captured first so the offer
                # can still arm the right resume origin, and "Sim" puts it back
                # (see the reactivation directive in `_send_bot_reply`). Still
                # after the pending-answer gate, so a resume in progress is
                # never wiped.
                resumable_origin = conversation.flow_state
                if _expire_stale_llm_state(conversation, tenant, last_activity_at):
                    logger.info(
                        "conversation_llm_state_expired",
                        conversation_id=str(conversation.id),
                        tenant_id=str(tenant.id),
                        ttl_minutes=llm_state_ttl_minutes(tenant),
                    )

                # Returning after a silence gap (and not already greeting on
                # first contact): offer to resume the prior workflow, or
                # re-greet. NO LONGER gated on `reactivation_enabled` - the
                # resume prompt has a product default text, so it works for
                # every tenant, while `reactivation_prompt_enabled` still
                # honours an explicit `initial_flows.reactivation.enabled`
                # of false.
                if (
                    greeting_override is None
                    and is_returning_patient
                    and reactivation_prompt_enabled(tenant)
                ):
                    offer = _reactivation_offer(
                        conversation,
                        tenant,
                        patient,
                        wa_id,
                        body,
                        last_activity_at,
                        resumable_origin,
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
    anything else (unchanged). Every other case - including a flows-DISABLED
    tenant - gets [Agendar, Outro].

    That pair used to be a trio, with "Gerenciar consulta" in the middle. It
    is gone from THIS branch on purpose: this branch is precisely the patient
    with nothing booked, for whom the button could only ever reach
    `_enter_manage`'s dead end ("Você não tem nenhuma consulta agendada no
    momento.") - a card offering an action the system already knows is
    impossible. The patient who DOES have something booked never saw it
    either: they get the Remarcar/Cancelar trio above. Nothing became
    unreachable - `route()` still dispatches the label from an older thread's
    button or from typed text, which is also why LABEL_MANAGE_APPOINTMENT
    stays in `_GREETING_ACTION_IDS`.

    For a flows-enabled tenant route() dispatches each label deterministically
    ("Outro" deliberately to the LLM); for a flows-disabled one, an Agendar
    tap is caught by `_persist_inbound_message`'s greeting-button
    short-circuit and degrades to a fixed "contact us" reply
    (`_handle_greeting_button_unavailable`), while "Outro" falls through to
    that tenant's normal all-LLM path - for that cohort the LLM IS the
    product, so "anything else" belongs there. Empty without a greeting.
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
    return [LABEL_BOOK, LABEL_OTHER]


def _flow_tenant_snapshot(
    tenant: Tenant, professionals: list[Professional], services: list | None = None
) -> SimpleNamespace:
    """Build the tenant-shaped config consumed by the deterministic router.

    A single active professional is the effective clinic config, matching
    ``load_tenant_config``. Multi-professional flows select a professional
    before reading that professional's catalog, so they keep tenant defaults
    on the initial snapshot.

    Resolution goes through ``professional_appointment_types`` /
    ``professional_business_hours``, so the sole professional's EMPTY own
    config stays empty here: a clinic that closed every day or removed every
    service has the deterministic router offer nothing, rather than quietly
    falling back to the tenant's legacy columns (see the NULL-versus-EMPTY note
    in services/tenant_config.py).

    The "exactly one" rule comes from `services/booking_scope.py`, the same
    one `resolve_booking_owner_id` applies to the ROSTER this snapshot travels
    with (``route(professionals=...)``) — so whoever's catalog is rendered
    here is provably whoever owns the booking that follows. `collect_insurance`
    / `insurances` stay tenant-level on purpose: they are clinic-wide settings
    and the convênio step asks them regardless of topology (see
    `flow_router._insurance_step_skip_reason`).

    `services` is the clinic's canonical catalog (services/service_catalog.py),
    loaded by the caller because the router itself does no DB I/O. Resolving
    HERE is what keeps `flow_router` a pure function while still showing the
    patient one spelling per service: the router receives entries that are
    already canonical and filters them exactly as before. An empty catalog (a
    tenant not backfilled yet) resolves to the raw stored entries.
    """
    appointment_types = resolve_entries(tenant.appointment_types, services)
    business_hours = tenant.business_hours
    professional = sole_active_professional(professionals)
    if professional is not None:
        appointment_types = professional_appointment_types(professional, tenant, services)
        business_hours = professional_business_hours(professional, tenant)

    return SimpleNamespace(
        initial_flows=tenant.initial_flows,
        appointment_types=appointment_types,
        appointment_duration_min=tenant.appointment_duration_min,
        business_hours=business_hours,
        collect_insurance=tenant.collect_insurance,
        insurances=tenant.insurances,
    )


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
    # Resolved through the clinic's canonical catalog so a historical
    # `appointment_type` whose spelling drifted still finds its service (and
    # therefore its price/description) — historical rows are never rewritten.
    services = await load_service_catalog(session, tenant.id)
    catalog = (
        professional_appointment_types(owner, tenant, services)
        if owner is not None
        else active_appointment_types(tenant, services)
    )
    nearest_service = None
    raw_type = nearest.get("appointment_type")
    if raw_type:
        target_name = normalize_service_name(raw_type)
        nearest_service = next(
            (svc for svc in catalog if normalize_service_name(svc.get("name")) == target_name),
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
    origin: FlowState,
) -> _ReplyContext | None:
    """Maybe offer a returning patient to resume, after a silence gap.

    When the conversation has a resumable state, arms the gate
    (`conversation.reactivation_origin`) and returns an offer that reuses the
    greeting send path: the returning greeting + the configured "quer continuar?"
    question, with Sim/Não buttons. For an IDLE conversation there is nothing to
    resume, so it sends just the returning greeting + menu. Returns None to fall
    through to normal dispatch (gap not reached yet, or nothing to say).

    `origin` is the flow state as it was BEFORE `_expire_stale_llm_state` ran
    earlier in the same turn - NOT `conversation.flow_state`, which by now may
    already have been dropped to IDLE. Reading the live column here would make a
    just-expired LLM conversation look like it had nothing to resume, and the
    patient would be silently rerouted instead of asked. The caller passes it
    explicitly for exactly that reason.
    """
    if last_activity_at is None:
        return None
    gap = datetime.now(UTC) - _as_utc(last_activity_at)
    if gap < timedelta(minutes=reactivation_gap_minutes(tenant)):
        return None

    returning = (tenant.returning_greeting_message or "").strip()
    if not returning and reactivation_enabled(tenant):
        # Pre-existing fallback, deliberately kept for the OPTED-IN cohort ONLY:
        # a tenant that switched reactivation on without writing a returning
        # greeting still reuses its welcome pitch. NOT extended to the universal
        # cohort - re-pitching the clinic at every 6h return is a lot of message
        # for a question that stands perfectly well on its own, and the pitch is
        # the one part of this that has no sensible product default.
        returning = (tenant.greeting_message or "").strip()
    greeting = _render_greeting_template(returning, patient.name) if returning else ""

    if origin in (
        FlowState.MENU,
        FlowState.SERVICE_CATALOG,
        FlowState.LLM,
    ):
        # Resumable: arm the gate and ask whether to continue.
        conversation.reactivation_origin = origin.value
        prompt = reactivation_continue_prompt(tenant)
        body_text = f"{greeting}\n\n{prompt}".strip() if greeting else prompt
        return _ReplyContext(
            conversation_id=conversation.id,
            patient_wa_id=wa_id,
            inbound_body=body or "",
            greeting_override=body_text,
            greeting_buttons=reactivation_choice_buttons(tenant),
        )

    # IDLE / nothing to resume: a plain returning greeting + menu. THIS half
    # stays opt-in, unlike the resume prompt above: it re-sends the clinic's OWN
    # greeting text - which is the welcome pitch (see CLAUDE.md) - so making it
    # universal would blast a marketing paragraph at every returning patient
    # every 6h. There is no sensible product default for someone else's pitch,
    # and nothing about a conversation sitting IDLE needs bounding.
    if not greeting or not reactivation_enabled(tenant):
        return None
    return _ReplyContext(
        conversation_id=conversation.id,
        patient_wa_id=wa_id,
        inbound_body=body or "",
        greeting_override=greeting,
        greeting_buttons=_greeting_buttons_for(tenant, greeting),
    )


def _expire_stale_llm_state(
    conversation: Conversation,
    tenant: Tenant,
    last_activity_at: datetime | None,
) -> bool:
    """Drop a long-idle full-LLM flow state so the next turn re-opens the menu.

    THE GAP THIS CLOSES. `route()` keeps a conversation in `FlowState.LLM`
    until something explicitly resets it, and every reset is initiated by the
    patient or by the agent itself: `/menu` (and its aliases), or one of the
    four hand-back tools (`show_main_menu`, `start_guided_booking`,
    `select_professional_and_continue`, `manage_existing_appointment`). Those
    work, and are untouched here — but none of them is GUARANTEED to happen.
    `_reactivation_offer` above is the only time-based exit, and it runs only
    when `reactivation_enabled(tenant)` is true, i.e. when the clinic filled in
    `returning_greeting_message` in the hub (or set the explicit
    `initial_flows.reactivation.enabled` flag). A default tenant has neither —
    `initial_flows` defaults to `{}` and the column to NULL — so for them
    NOTHING bounded the stay, and `Conversation` is one row per patient forever
    (`uq_conversations_tenant_patient`), so no "new conversation" ever starts
    clean either. One "Outro" tap left that patient answering into the free LLM
    on every future contact, weeks later included.

    THE FLOOR. After `llm_state_ttl_minutes` of silence the flow state is
    simply dropped, so the CURRENT inbound routes from `IDLE` and `route()`
    re-presents the menu. Deliberately silent: no extra message is sent, no
    history/appointment/consent row is touched — only the transient `flow_*`
    fields move, the same set the "Não" answer to the resume prompt clears.
    Runs AFTER the offer above, so an opted-in tenant's "quer continuar?" still
    wins and this only ever catches the cohort the offer skips.

    SCOPE. `FlowState.LLM` only. A stale `SERVICE_CATALOG`/`MANAGE_BOOKING`
    conversation is left alone on purpose: those steps re-prompt deterministically
    on unexpected input, so they self-correct — full LLM mode is the one state
    with no deterministic way back.

    Returns True when the state was expired (the caller logs it).
    """
    if conversation.flow_state != FlowState.LLM:
        return False
    if last_activity_at is None:
        return False
    gap = datetime.now(UTC) - _as_utc(last_activity_at)
    if gap < timedelta(minutes=llm_state_ttl_minutes(tenant)):
        return False
    conversation.flow_state = FlowState.IDLE
    conversation.flow_step = None
    conversation.flow_selected_type = None
    conversation.flow_selected_day = None
    conversation.flow_selected_slot = None
    conversation.flow_managing_appointment_id = None
    # `flow_selected_professional_id` / `flow_selected_insurance` are NOT
    # cleared here, unlike the "Não" answer which drops everything. They say WHO
    # the patient is dealing with, not where they were in a form, and the agent
    # reads the professional to overlay its config (`selected_professional` ->
    # `run_agent`). Since this expiry now runs BEFORE the "quer continuar?"
    # prompt, clearing them would make a "Sim" resume into a conversation that
    # had silently forgotten the patient's doctor. Nothing leaks from keeping
    # them: `_apply_flow_result` rewrites every flow field from the next result,
    # so the very next routed turn overwrites both.
    return True


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
    message, then recreates an empty patient + conversation and sends the
    tenant's *first-contact* greeting (`greeting_message`), so the number is
    treated as a brand-new first contact. This is the old `/menu` "dev reset",
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
                greeting = (tenant.greeting_message or "").strip()
                greeting_buttons = _greeting_buttons_for(tenant, greeting or None)
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

    # Send the first-contact greeting verbatim (one message, with the configured
    # quick-reply buttons), exactly as a brand-new patient would receive it.
    await _send_greeting(
        _ReplyContext(
            conversation_id=conversation_id,
            tenant_id=tenant.id,
            patient_wa_id=wa_id,
            inbound_body="",
            greeting_override=greeting,
            greeting_buttons=greeting_buttons,
        ),
        tenant=tenant,
        waba_token=waba_token,
    )


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

    # Tenant bot not activated: one polite fallback, nothing else - sent on
    # that tenant's OWN credentials and behind the same entitlement gate as
    # every other outbound (PROMPT_FIX_21).
    if reply.service_unavailable:
        await _handle_service_unavailable(reply, redis=redis)
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
    # The paired flag says an owner was IDENTIFIED, so a failed build stays a
    # None the router degrades on instead of falling back to the wrong agenda.
    manage_calendar: CalendarService | None = None
    manage_calendar_owned = False
    patient_name = None
    patient_wa = reply.patient_wa_id
    # Post-consult-knowledge injection gate (see
    # _should_inject_post_consult_knowledge below): the patient's derived
    # opening state and the conversation's flow_state, captured as plain
    # values inside the session below - both stay None when the
    # conversation/tenant fails to resolve, or the check isn't needed this turn.
    opening_state: PatientOpeningState | None = None
    flow_state: FlowState | None = None
    # Captured alongside flow_state purely for the llm_activated accounting
    # below: flow_state says WHICH state leaked to the model, flow_step says
    # where inside it - together they name the node that still needs a
    # deterministic answer.
    flow_step: str | None = None
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is not None:
                flow_state = conversation.flow_state
                flow_step = conversation.flow_step
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
                    # ONE read of the clinic's canonical catalog per turn. Each
                    # professional's entries are resolved through it HERE, so
                    # the pure router downstream sees the clinic's single
                    # spelling per service without ever touching the DB. An
                    # empty catalog (tenant not backfilled) leaves the stored
                    # entries exactly as they are today. `None` is preserved as
                    # `None`, not flattened to `[]`, because that is the ONLY
                    # thing that makes `professional_appointment_types` fall
                    # back to the tenant's legacy list — a professional whose
                    # own list is `[]` offers nothing, and flattening here would
                    # silently turn that into the clinic's old catalog.
                    service_catalog = await load_service_catalog(session, tenant.id)
                    flow_professionals = [
                        SimpleNamespace(
                            id=p.id,
                            name=p.name,
                            specialty=p.specialty,
                            about=p.about,
                            context_doctor_message=p.context_doctor_message,
                            appointment_types=(
                                resolve_entries(p.appointment_types, service_catalog)
                                if p.appointment_types
                                else p.appointment_types
                            ),
                            # Verbatim, NULL and all — no catalog to resolve
                            # against, and the NULL-versus-EMPTY distinction is
                            # exactly what `professional_business_hours` reads
                            # to tell "inherits the clinic's hours" from "has
                            # none at all", which is what the day picker's
                            # config-gap check now turns on.
                            business_hours=p.business_hours,
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
                            # The conversation's id rides along so the scoped
                            # help nodes (ai/scoped_help.py, called from
                            # route()) can re-read recent history the same
                            # stateless way run_agent does.
                            id=conversation.id,
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
                        _flow_tenant_snapshot(tenant, professional_rows, service_catalog),
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
                        or _label_match_body(reply.inbound_body, LABEL_MANAGE_APPOINTMENT)
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
                        reply.inbound_body,
                    )
                    manage_calendar_owned = manage_target is not None
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

    # First-contact/returning greeting: deterministic, verbatim and sent with
    # this tenant's own WhatsApp number/token. This intentionally runs only
    # after entitlement and tenant credentials have been resolved.
    if reply.greeting_override is not None:
        await _send_greeting(reply, tenant=tenant, waba_token=waba_token)
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

    # `/menu` & aliases: the non-destructive menu return (PROMPT_FIX_18).
    # Reached only here, so it has already cleared the exact same gates as any
    # other turn - allowlist and handover (in `_persist_inbound_message`),
    # entitlement, and the addon inbound hooks just above. It reuses
    # `_handle_show_main_menu`, the very seam the agent's `show_main_menu` tool
    # goes through, so both surfaces produce the identical effective menu and
    # the identical flow-state write. Placed BEFORE the reactivation/flow
    # blocks so the command is never re-interpreted as input to whatever step
    # the patient was on.
    if reply.menu_requested:
        await _handle_show_main_menu(
            reply,
            tenant,
            flow_professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="command",
        )
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
        if reply.reactivation.origin == FlowState.LLM.value:
            # `_expire_stale_llm_state` already dropped the state to IDLE before
            # the prompt was sent, so that an UNANSWERED prompt still exits LLM
            # mode. "Sim" is the answer that undoes that, so it has to write the
            # state back - through `_apply_flow_result`, the one persistence
            # seam, never by hand. `delegate_llm` writes the flow fields and
            # returns False, so the turn falls straight through to the agent.
            await _apply_flow_result(
                reply,
                FlowRouterResult(
                    action="delegate_llm",
                    flow_state=FlowState.LLM,
                    # Carried EXPLICITLY: `_apply_flow_result` writes every flow
                    # field from the result, so a resume that does not name
                    # these drops the patient's doctor on the way back in.
                    flow_selected_professional_id=conv_snapshot.flow_selected_professional_id,
                    flow_selected_insurance=conv_snapshot.flow_selected_insurance,
                ),
                patient_wa,
                redis=redis,
                tenant=tenant,
                waba_token=waba_token,
            )
        else:
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
            manage_calendar_owned=manage_calendar_owned,
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

    # Every LLM turn is either a deliberate escape hatch ("Outro") or a gap in
    # the deterministic flow. Emitted at INFO so the gaps are countable in
    # production without raising LOG_LEVEL - carries no message body, phone or
    # patient name.
    logger.info(
        "llm_activated",
        reason=_llm_activation_reason(flow_state, delegated_to_llm),
        flow_state=flow_state.value if flow_state is not None else None,
        flow_step=flow_step,
        delegated=delegated_to_llm,
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id) if tenant is not None else None,
    )

    # The tenant's REAL shape for THIS turn, resolved once: it decides both
    # the capability set (below, and ai/graph.py::base_tools_for) and which
    # hand-back tools the agent may be offered (_flow_handback_tools).
    turn_topology = booking_topology(professional_rows)

    reply_text = await run_agent(
        reply.inbound_body,
        context={"conversation_id": str(reply.conversation_id)},
        tenant_config=tenant_config,
        # The deterministic-flow hand-back tools ride along with the plugin
        # ones, gated on the flow existing at all (and, for
        # start_guided_booking, on the topology) - see _flow_handback_tools.
        extra_tools=_flow_handback_tools(tenant, turn_topology, agent_tools_for(summary)),
        redis=redis,
        selected_professional=selected_professional,
        # The tenant's REAL shape decides which tools the agent is given at
        # all (ai/graph.py::base_tools_for): a multi-professional clinic never
        # receives the tenant-level calendar tools, and a single-professional
        # one lets the base booking tool resolve its owner. `professional_rows`
        # is None only when the roster load itself failed (already logged) -
        # booking_topology maps that to "unknown", which keeps today's tool
        # set rather than silently disarming a working clinic.
        booking_topology=turn_topology,
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
    if reply_text.startswith(START_GUIDED_BOOKING_SENTINEL_PREFIX):
        await _handle_start_guided_booking(
            reply,
            reply_text,
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


def _llm_activation_reason(
    flow_state: FlowState | None,
    delegated_to_llm: bool,
) -> str:
    """Why THIS turn is about to spend a full LangGraph agent call.

    The deterministic router is the product; the model is the last resort. So
    every activation is worth naming, and the names are chosen to be acted on:

    - `sticky_llm_mode`: the conversation is already parked in FlowState.LLM
      (the patient tapped "Outro" earlier and nothing handed them back). A high
      count here means the hand-backs (`show_main_menu`, select-professional,
      manage-appointment) are not firing often enough.
    - `router_delegated`: the router saw this turn and gave up on it. This is
      the number to drive to zero - each one is a flow node that needs a
      deterministic answer. Pair with `flow_state`/`flow_step` to find it.
    - `no_deterministic_flow`: the router never ran at all for this turn (no
      flow snapshot could be built - e.g. tenant/config resolution came back
      empty). Not a flow gap; a data/config problem.

    Pure function over already-resolved turn facts, like the gate below.
    """
    if flow_state is FlowState.LLM:
        return "sticky_llm_mode"
    if delegated_to_llm:
        return "router_delegated"
    return "no_deterministic_flow"


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
        target == label.strip().casefold()
        or target == truncate_button_label(label).strip().casefold()
    )


def _flow_handback_tools(
    tenant: Tenant | None, topology: str, plugin_tools: list
) -> list:
    """This turn's `extra_tools`: the plugin set + the flow hand-back tools.

    Both hand-backs re-enter the deterministic flow through a sentinel, so
    neither means anything to a tenant that has no such flow — hence the
    `flows_enabled` gate they have always shared (unconditional since the flows
    became the product, kept because it is the file's pattern and the switch
    could come back).

    `start_guided_booking` carries one gate the other does not: it is withheld
    from a MULTI-professional tenant. The day picker it opens would read
    availability off the clinic-level agenda, not the chosen doctor's, so on
    those tenants the way back into the flow is `select_professional_and_continue`
    (which re-enters at a doctor whose calendar IS resolved) or the menu. Same
    two-lock shape the calendar tools use: withheld from the tool set here,
    and refused again inside the tool by `_blocked_tenant_level` if it ever
    arrives anyway.
    """
    if tenant is None or not flows_enabled(tenant):
        return list(plugin_tools)
    handbacks = [manage_existing_appointment]
    if topology != BOOKING_TOPOLOGY_MULTI:
        handbacks.append(start_guided_booking)
    return [*plugin_tools, *handbacks]


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


def _appointment_calendar_target(
    appt: dict | None, professional_rows: list[Professional] | None
) -> Professional | Literal["tenant"] | None:
    """Whose calendar owns ONE appointment dict (pure, no I/O).

    The owning `Professional` row when the appointment names one on the active
    roster; the literal "tenant" when it names none (booked at the tenant
    level); None when there is no appointment, or its `professional_id` no
    longer resolves - the caller must then NOT guess an agenda.
    """
    if appt is None:
        return None
    professional_id = appt.get("professional_id")
    if not professional_id:
        return "tenant"
    return next(
        (p for p in (professional_rows or []) if str(p.id) == str(professional_id)), None
    )


async def _appointment_calendar(
    session, tenant: Tenant, target: "Professional | Literal['tenant'] | None"
) -> CalendarService | None:
    """Build the CalendarService for an owner `_appointment_calendar_target` found.

    Used by the two entries that open the reschedule day picker WITHOUT a
    conversation snapshot to hang it off (a reminder button tap, and the LLM's
    manage-appointment hand-back). None in -> None out, and a build failure is
    logged count-only and answered with None too: the day picker then replies
    `calendar_unavailable`, which is the honest answer, rather than listing
    days off whichever agenda happened to be at hand.
    """
    if target is None:
        return None
    try:
        tenant_config = await load_tenant_config(session, tenant)
        if isinstance(target, Professional):
            return await resolve_professional_calendar(
                session, tenant, target, tenant_config=tenant_config
            )
        return CalendarService.from_tenant_config(tenant_config) if tenant_config else None
    except Exception as exc:
        logger.warning("worker_appointment_calendar_failed", error=str(exc))
        return None


def _manage_owner_calendar_target(
    flow_state: FlowState | None,
    flow_managing_appointment_id: UUID | None,
    upcoming_appointments: list[dict] | None,
    professional_rows: list[Professional] | None,
    inbound_body: str | None = None,
) -> Professional | Literal["tenant"] | None:
    """Whose calendar this turn's manage action acts on (pure, no I/O).

    Two turns need it, and both are resolved BEFORE `route()` runs because the
    router does no DB I/O of its own:

      - a turn already INSIDE the manage flow with a target set: resolve
        `flow_managing_appointment_id` against the `upcoming_appointments`
        dicts already loaded for this turn (see `wants_upcoming_appointments`
        above - it guarantees they are loaded whenever this matters);
      - the ENTRY turn of a direct "Remarcar" tap, which is still IDLE/MENU
        here yet opens the day picker inside this very turn
        (`enter_manage_action`'s single-appointment shortcut). Exactly one
        upcoming appointment is unambiguous; with 2+ the router shows a pick
        list and needs no calendar at all, so None is correct there.

    Returns None for every other turn - the caller falls back to its existing
    calendar resolution (`_flow_turn_calendar`).

    Deliberately ignores `flow_selected_professional_id` (the SERVICE_CATALOG
    booking selection) - a stale doctor pick from an earlier booking flow must
    never decide which agenda a cancel/reschedule acts on.
    """
    if flow_state == FlowState.MANAGE_BOOKING and flow_managing_appointment_id is not None:
        target = str(flow_managing_appointment_id)
        appt = next(
            (a for a in (upcoming_appointments or []) if str(a.get("id")) == target), None
        )
        return _appointment_calendar_target(appt, professional_rows)
    if _label_match_body(inbound_body, LABEL_RESCHEDULE) and len(upcoming_appointments or []) == 1:
        return _appointment_calendar_target(
            (upcoming_appointments or [])[0], professional_rows
        )
    return None


# --------------------------------------------------------------------------
# Pix deposit money hooks (PROMPT S3) — reminder action buttons + the
# deterministic flow's own manage (cancel/reschedule) sub-flow.
# --------------------------------------------------------------------------

_APPOINTMENT_NOT_FOUND_TEXT = "Não encontrei essa consulta."

# Steps `_apply_deposit_awareness` inspects. The day-picker retry/escape
# renders are the SAME step of the flow as STEP_MANAGE_DAY, just re-drawn
# after an unreadable free-text date, so the reschedule-limit pre-check has to
# recognise them too — otherwise a blocked target could slip past the gate by
# mistyping a date once.
_RESCHEDULE_PRECHECK_STEPS = (
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_RETRY,
    STEP_MANAGE_DAY_ESCAPE,
)


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

    previous_status = appointment.status
    appointment.status = AppointmentStatus.CANCELLED
    log_status_transition(
        appointment_id=appointment.id,
        tenant_id=tenant.id,
        old_status=previous_status,
        new_status=AppointmentStatus.CANCELLED,
        source=SOURCE_BUTTON,
        idempotency_key=f"apptcancel:{appointment.id}",
    )
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
        or result.flow_step not in _RESCHEDULE_PRECHECK_STEPS
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
        else:  # any STEP_MANAGE_DAY* render
            if deposit is None or deposit.reschedule_count < tenant.pix_reschedule_limit:
                return result
            limit_count = deposit.reschedule_count
            limit_value = tenant.pix_reschedule_limit

    if warning is not None:
        if result.bubbles:
            result.bubbles[0].body = f"{warning}\n\n{result.bubbles[0].body}"
        return result

    client = _tenant_client(tenant, waba_token)
    if client is None:
        # Fail closed (PROMPT_FIX_21): without this tenant's own credentials
        # the keep-or-cancel card cannot be sent, so leave the flow result
        # untouched rather than resetting to MENU with nothing delivered.
        logger.error("worker_reschedule_limit_no_credential", appointment_id=str(appointment_id))
        return result
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
    # FEAT_34 §4: a tap on the cancellation notice's rebooking buttons. Same
    # shape as the reschedule handoff — resolved inside the session, acted on
    # after it closes.
    rebooking_handoff: tuple | None = None
    decline_handoff: tuple | None = None

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        waba_token = await get_waba_token(session, tenant.id)
        # Fail closed (PROMPT_FIX_21): the tap is already deduped by its
        # ProcessedEvent, so raising here would just retry a configuration
        # problem forever. The patient simply gets no answer to the tap.
        client = _tenant_client(tenant, waba_token)
        if client is None:
            logger.error("worker_action_button_no_credential", tenant_id=str(tenant.id))
            return

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
            # LIVE, not a hand-written pair (PROMPT_FIX_16): a booking the
            # patient already rescheduled is still theirs to confirm - it used
            # to answer "essa consulta não está mais ativa" instead.
            if is_live_status(appointment.status) and is_future:
                previous_status = appointment.status
                appointment.status = AppointmentStatus.CONFIRMED
                log_status_transition(
                    appointment_id=appointment.id,
                    tenant_id=tenant.id,
                    old_status=previous_status,
                    new_status=AppointmentStatus.CONFIRMED,
                    source=SOURCE_BUTTON,
                    idempotency_key=f"apptconfirm:{appointment.id}",
                )
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

        if action == "rebookno":
            # The patient does not want to rebook. Ask WHY — deterministically,
            # with a fixed option list — because "vou procurar outra clínica"
            # and "não preciso mais" are the same tap and mean opposite things
            # commercially (FEAT_34 §8). Never handed to the LLM.
            #
            # Handed off like the branches below rather than answered here:
            # `_apply_flow_result` opens its OWN session, and calling it while
            # this one is still open nests two sessions on the same engine.
            decline_handoff = (tenant, waba_token, appointment.id)

        if action in ("rebooksame", "rebookother"):
            # Rebooking after the DOCTOR cancelled. The cancelled appointment
            # stays CANCELLED and is NOT reopened (FEAT_34 §4.3) — confirming
            # produces a new booking through the normal tail.
            if not flows_enabled(tenant):
                await client.send_text_message(
                    to=reply.patient_wa_id,
                    body="Para remarcar, entre em contato com a nossa equipe.",
                )
                return
            professional_rows = await list_active_professionals(session, tenant.id)
            professionals = [
                SimpleNamespace(
                    id=p.id,
                    name=p.name,
                    specialty=p.specialty,
                    about=p.about,
                    context_doctor_message=p.context_doctor_message,
                    appointment_types=p.appointment_types,
                    # Verbatim, NULL and all: the router reads it through
                    # `professional_business_hours`, whose whole contract is
                    # that NULL inherits the clinic's hours and `{}` does not.
                    # Flattening it here would erase that distinction and make
                    # an inheriting doctor look unbookable.
                    business_hours=p.business_hours,
                )
                for p in professional_rows
            ]
            same = action == "rebooksame"
            service_name = appointment.appointment_type
            candidates: list = []
            prefix = None
            if same:
                target_id = appointment.professional_id
            else:
                services = await load_service_catalog(session, tenant.id)
                candidates, prefix = rebooking_candidates(
                    tenant,
                    professionals,
                    cancelled_professional_id=appointment.professional_id,
                    service_name=service_name,
                    services=services,
                )
                # Only a single candidate fixes the agenda in advance; with a
                # list the patient still picks, and their own calendar is
                # resolved when they do.
                target_id = candidates[0].id if len(candidates) == 1 else None
            rebook_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target({"professional_id": target_id}, professional_rows),
            )
            rebooking_handoff = (
                tenant,
                conversation,
                professionals,
                waba_token,
                rebook_calendar,
                same,
                appointment.professional_id,
                service_name,
                candidates,
                prefix,
            )

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
                    # Verbatim, NULL and all: the router reads it through
                    # `professional_business_hours`, whose whole contract is
                    # that NULL inherits the clinic's hours and `{}` does not.
                    # Flattening it here would erase that distinction and make
                    # an inheriting doctor look unbookable.
                    business_hours=p.business_hours,
                )
                for p in professional_rows
            ]
            # The reschedule opens the day picker in this very turn, so it
            # needs THIS appointment's own agenda resolved here, inside the
            # session. A failure leaves it None and the picker answers
            # `calendar_unavailable` - never a day list off the wrong calendar.
            reschedule_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target(
                    {"professional_id": appointment.professional_id}, professional_rows
                ),
            )
            reschedule_handoff = (
                tenant,
                appointments,
                professionals,
                waba_token,
                reschedule_calendar,
            )

    if decline_handoff is not None:
        dh_tenant, dh_waba_token, dh_appointment_id = decline_handoff
        await _apply_flow_result(
            reply,
            enter_decline_reasons(dh_appointment_id),
            reply.patient_wa_id,
            redis=redis,
            tenant=dh_tenant,
            waba_token=dh_waba_token,
        )
        return

    if rebooking_handoff is not None:
        (
            rb_tenant,
            rb_conversation,
            rb_professionals,
            rb_waba_token,
            rb_calendar,
            rb_same,
            rb_cancelled_professional_id,
            rb_service_name,
            rb_candidates,
            rb_prefix,
        ) = rebooking_handoff
        result = await enter_rebooking(
            rb_conversation,
            rb_tenant,
            rb_calendar,
            professionals=rb_professionals,
            cancelled_professional_id=rb_cancelled_professional_id,
            service_name=rb_service_name,
            same_professional=rb_same,
            candidates=rb_candidates,
            prefix=rb_prefix,
        )
        await _apply_flow_result(
            reply,
            result,
            reply.patient_wa_id,
            redis=redis,
            tenant=rb_tenant,
            waba_token=rb_waba_token,
        )
        return

    if reschedule_handoff is not None:
        (
            handoff_tenant,
            appointments,
            professionals,
            handoff_waba_token,
            reschedule_calendar,
        ) = reschedule_handoff
        result = await enter_manage_action(
            "reschedule",
            handoff_tenant,
            appointments,
            professionals,
            preselected_id=appt_uuid,
            calendar=reschedule_calendar,
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
    "gerenciar": "Para remarcar ou cancelar sua consulta, entre em contato com a nossa equipe.",
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
        client = _tenant_client(tenant, waba_token)
    if client is None:
        return  # fail closed - never on the global scaffold (PROMPT_FIX_21)
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
    manage_calendar_owned: bool = False,
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
    calendar and `manage_calendar_owned` says an owner was identified for this
    turn at all (`_manage_owner_calendar_target`) - together they replace
    `_flow_turn_calendar`, deliberately ignoring `flow_selected_professional_id`
    (a stale booking-flow selection must never decide the manage agenda).
    """
    # A turn that acts on an EXISTING appointment must use that appointment's
    # own agenda and no other. `manage_calendar_owned` says the caller
    # identified such an owner (`_manage_owner_calendar_target`); the calendar
    # itself may still be None because building it failed, and that must stay
    # a None the router degrades on - never a silent fallback to the tenant
    # agenda, which would cancel or reschedule on the wrong calendar.
    calendar = (
        manage_calendar
        if manage_calendar_owned
        else _flow_turn_calendar(conv_snapshot, tenant_config, flow_calendar)
    )
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


def _log_booking_scope(appointment: Appointment, tenant_id: UUID, *, source: str) -> None:
    """Emit the two booking-scope facts for one committed appointment.

    Ids and enums only — never the patient, the phone, the event title or the
    service description. `booking_owner_resolved` answers "did this booking
    get an owner, and does the tenant even have one to give?"; the paired
    event in ai/tools.py::_persist_appointment does the same for the LLM path,
    so both surfaces are countable with one query. `has_owner=False` on a
    single-professional tenant is exactly the regression this round fixed.
    """
    logger.info(
        "booking_owner_resolved",
        tenant_id=str(tenant_id),
        appointment_id=str(appointment.id),
        professional_id=(
            str(appointment.professional_id) if appointment.professional_id else None
        ),
        has_owner=appointment.professional_id is not None,
        source=source,
    )
    logger.info(
        "booking_service_resolved",
        tenant_id=str(tenant_id),
        appointment_id=str(appointment.id),
        professional_id=(
            str(appointment.professional_id) if appointment.professional_id else None
        ),
        has_type=bool(appointment.appointment_type),
        source=source,
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
                        # Read BEFORE the write so the transition log can name
                        # the status we came from; the row is then reused by
                        # the money hook below instead of re-selected.
                        cancelled_appt = await session.scalar(
                            select(Appointment).where(
                                Appointment.google_event_id == result.appointment_cancel_id,
                                Appointment.tenant_id == conv.tenant_id,
                            )
                        )
                        previous_status = (
                            cancelled_appt.status if cancelled_appt is not None else None
                        )
                        await session.execute(
                            update(Appointment)
                            .where(
                                Appointment.google_event_id == result.appointment_cancel_id,
                                Appointment.tenant_id == conv.tenant_id,
                            )
                            .values(status=AppointmentStatus.CANCELLED)
                        )
                        if cancelled_appt is not None:
                            log_status_transition(
                                appointment_id=cancelled_appt.id,
                                tenant_id=conv.tenant_id,
                                old_status=previous_status,
                                new_status=AppointmentStatus.CANCELLED,
                                source=SOURCE_FLOW,
                                idempotency_key=f"cancel:{result.appointment_cancel_id}",
                            )
                        # Money hook: resolve the deposit's outcome for this
                        # cancellation and carry the honest notice through to
                        # the reply dispatched below (PROMPT S3 section 4).
                        if tenant is not None:
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
                    if result.decline_reason:
                        # Business data (churn signal), written in the SAME txn
                        # as the flow state so an answer can never be recorded
                        # without the conversation having moved past the
                        # question. Tenant comes from the conversation, never
                        # from the message. The free text is patient content:
                        # it goes in this row and is never logged.
                        decline = result.decline_reason
                        owns_appointment = await session.scalar(
                            select(Appointment.id).where(
                                Appointment.id == decline["appointment_id"],
                                Appointment.tenant_id == conv.tenant_id,
                            )
                        )
                        if owns_appointment is not None:
                            session.add(
                                RebookingDecline(
                                    tenant_id=conv.tenant_id,
                                    appointment_id=decline["appointment_id"],
                                    reason_code=decline["reason_code"],
                                    reason_text=decline["reason_text"],
                                )
                            )
                    if result.appointment_reschedule:
                        resched = result.appointment_reschedule
                        # Read BEFORE the write (same reason as the cancel
                        # branch above) and reuse the row for the money hook.
                        resched_appt = await session.scalar(
                            select(Appointment).where(
                                Appointment.google_event_id == resched["google_event_id"],
                                Appointment.tenant_id == conv.tenant_id,
                            )
                        )
                        previous_status = (
                            resched_appt.status if resched_appt is not None else None
                        )
                        # The SAME row moves to the new window and stays LIVE -
                        # RESCHEDULED is not a tombstone (PROMPT_FIX_16, see the
                        # taxonomy on models/appointment.py). Its id,
                        # google_event_id and PixDeposit all carry over, which
                        # is exactly why every reader must keep counting it as
                        # upcoming and remindable.
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
                        if resched_appt is not None:
                            log_status_transition(
                                appointment_id=resched_appt.id,
                                tenant_id=conv.tenant_id,
                                old_status=previous_status,
                                new_status=AppointmentStatus.RESCHEDULED,
                                source=SOURCE_FLOW,
                                # The moved window makes a REPLAY of this exact
                                # reschedule recognisable; re-running it is a
                                # no-op write, never a second move.
                                idempotency_key=(
                                    f"resched:{resched['google_event_id']}"
                                    f":{resched['start_at'].isoformat()}"
                                ),
                            )
                        # Money hook: count this reschedule against the
                        # deposit's limit. Non-crashing on a race (entry was
                        # already pre-checked by _apply_deposit_awareness /
                        # the button carrier's own check) — never unwind an
                        # already-persisted reschedule over a counter race.
                        if tenant is not None:
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
        _log_booking_scope(booked_appointment, tenant.id, source=SOURCE_FLOW)
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
    # The professional the patient reached cannot be booked at all because
    # THEIR static config is incomplete. Tell the patient, then alert the
    # clinic AND that doctor. No handover: the other doctors are fine, and a
    # human secretary cannot conjure a schedule either - what is missing is a
    # config change, which is exactly what the email asks for.
    if result.action == "professional_config_incomplete":
        await _handle_professional_config_incomplete(
            reply, result, redis=redis, tenant=tenant, waba_token=waba_token
        )
        return True
    # A scoped-help node escalated: flip to human handover FIRST (mirroring
    # _handle_calendar_unavailable's order - if the send below fails, the
    # human is already on it), then tell the patient. No owner email alert:
    # nothing is broken, the secretary sees the chat in their WhatsApp app.
    if result.action == "handover":
        await _set_conversation_human_active(reply.conversation_id)
        if result.bubbles:
            await _dispatch_bubbles(reply, result.bubbles, tenant=tenant, waba_token=waba_token)
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
    `tenant`/`waba_token` select the per-tenant WhatsApp client. FAIL-CLOSED
    (PROMPT_FIX_21): a missing tenant or missing credentials sends NOTHING and
    returns 0 - it never falls back to the global env scaffold, which would
    answer this clinic's patient from another clinic's number.
    """
    client = _tenant_client(tenant, waba_token)
    if client is None:
        logger.error(
            "worker_bot_reply_no_credential",
            conversation_id=str(reply.conversation_id),
            bubbles=len(bubbles),
        )
        return 0
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
# covers these five code-constant labels; anything else (reactivation's
# Sim/Não, tenant-configurable) is NOT in this map on purpose - see
# _send_greeting's comment. LABEL_RESCHEDULE/LABEL_CANCEL_APPT stay: the
# HAS_UPCOMING(_SOON) trio still sends them (and old threads keep their
# buttons tappable long after the initial trio moved to "gerenciar").
_GREETING_ACTION_IDS: dict[str, str] = {
    LABEL_BOOK: "agendar",
    LABEL_MANAGE_APPOINTMENT: "gerenciar",
    LABEL_RESCHEDULE: "remarcar",
    LABEL_CANCEL_APPT: "cancelar",
    LABEL_OTHER: "outro",
}

# The one greeting-button suffix `_persist_inbound_message`'s flows-disabled
# short-circuit deliberately lets through: "Outro" promises the LLM, and the
# flows-disabled cohort's normal path IS the LLM.
_GREETING_LLM_ESCAPE_SUFFIX = _GREETING_ACTION_IDS[LABEL_OTHER]


async def _send_greeting(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    waba_token: str | None = None,
) -> None:
    """Send a tenant's initial or returning greeting as one verbatim message.

    With configured labels it goes out as an interactive reply-button message
    (the tapped label becomes the patient's next inbound); otherwise as plain
    text. The whole greeting is one WhatsApp message either way.
    """
    body = reply.greeting_override or ""
    # Fail closed (PROMPT_FIX_21) rather than letting the credential error
    # escape into the arq job, which would retry the whole turn forever on
    # what is a configuration problem, not a transient one.
    client = _tenant_client(tenant, waba_token)
    if client is None:
        logger.error(
            "worker_greeting_no_credential",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return
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


async def _handle_service_unavailable(reply: _ReplyContext, redis=None) -> None:
    """The "bot not activated yet" degrade, on the RIGHT sender (PROMPT_FIX_21).

    This path has no conversation (none is created for an inactive tenant), so
    it used to build a bare `WhatsAppClient()` and answer from the global env
    scaffold - i.e. potentially from another clinic's WhatsApp number, and
    without the entitlement gate every other outbound passes. It now resolves
    the tenant carried on the `_ReplyContext`, applies the same fail-closed
    entitlement check as `_send_bot_reply`, and sends on that tenant's own
    credentials or not at all.

    The allowlist was already applied upstream, before this context was ever
    built (see `_persist_inbound_message`).
    """
    if reply.tenant_id is None:
        logger.error("whatsapp_credential_missing", tenant_id=None, missing="tenant")
        return

    async with async_session_factory() as session:
        tenant = await session.get(Tenant, reply.tenant_id)
        if tenant is None:
            logger.error("whatsapp_credential_missing", tenant_id=None, missing="tenant")
            return
        waba_token = await get_waba_token(session, tenant.id)

    summary = await get_entitlements(tenant.id, redis)
    if summary is None or not (summary.active and summary.secretaria_enabled):
        # Fails closed exactly like the main reply path: an unentitled tenant
        # gets no outbound at all, not even this fallback.
        logger.warning(
            "bot_reply_suppressed_unentitled",
            tenant_id=str(tenant.id),
            status=summary.status if summary is not None else None,
        )
        return

    client = _tenant_client(tenant, waba_token)
    if client is None:
        return
    await _send_simple_text(reply.patient_wa_id, SERVICE_UNAVAILABLE_MESSAGE, client=client)


def _tenant_client(tenant: Tenant | None, waba_token: str | None) -> WhatsAppClient | None:
    """This tenant's WhatsApp client, or None when it cannot be built.

    The single fail-closed seam for every worker send (PROMPT_FIX_21). There is
    NO fallback to the global `META_*` env scaffold: without this tenant's own
    `phone_number_id` + decrypted token the message would go out from another
    clinic's WhatsApp number, so it does not go out at all. `for_tenant` emits
    `whatsapp_credential_missing` before raising, so the outcome is always
    visible in the logs; callers just degrade quietly.
    """
    if tenant is None:
        logger.error("whatsapp_credential_missing", tenant_id=None, missing="tenant")
        return None
    try:
        return WhatsAppClient.for_tenant(tenant, waba_token)
    except TenantWhatsAppCredentialMissing:
        return None


async def _send_simple_text(to: str, body: str, *, client: WhatsAppClient) -> None:
    """Send a single plain-text message, swallowing send errors (MVP: no retry).

    `client` is REQUIRED and must be tenant-scoped (`_tenant_client` /
    `WhatsAppClient.for_tenant`): there is no implicit global-scaffold default
    any more, so no caller can accidentally send from the wrong WABA.
    """
    try:
        await client.send_text_message(to=to, body=body)
    except Exception as exc:
        logger.error(
            "worker_simple_text_send_failed",
            error_type=type(exc).__name__,
            to_suffix=wa_suffix(to),
        )


async def _set_conversation_human_active(conversation_id: UUID | None) -> None:
    """Flip one conversation to human handover, in its own short transaction.

    The `_apply_flow_result` "handover" branch's seam (scoped-help
    escalation). Same defensive shape as `_handle_calendar_unavailable`'s
    handover block, minus the owner alert - a failure is logged, never
    raised, so the escalation message still goes out.
    """
    if conversation_id is None:
        return
    try:
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, conversation_id)
                if conversation is not None:
                    await HandoverManager(session).set_human_active(conversation)
    except Exception as exc:
        logger.error(
            "worker_scoped_help_handover_failed",
            error=str(exc),
            conversation_id=str(conversation_id),
        )


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

    # Fail closed (PROMPT_FIX_21): the handover above already happened, so a
    # human still sees the conversation even when the patient-facing notice
    # can't be sent on this tenant's own credentials.
    client = _tenant_client(tenant, waba_token)
    if client is not None:
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
        themselves (`professionals.email`), each only when set. Either may be
        NULL — `professionals.email` is NULL for every row until a clinic fills
        it in — and neither being set is a no-op, never an error: the patient
        has already been answered, so there is nothing left to fail.
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

    # Both addresses, deduped, each only when actually set. Nobody to write to
    # is a normal outcome (a clinic that never filled either in), not a
    # failure: the patient has already been answered.
    recipients: list[str] = []
    for candidate in (alert_tenant.contact_email, professional.email):
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
            patient_phone=reply.patient_wa_id,
        )


async def _handle_show_main_menu(
    reply: _ReplyContext,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
    source: str = "agent_tool",
) -> None:
    """Non-destructive menu return. The ONE way back to the menu.

    Shared by the agent's `show_main_menu` tool (`source="agent_tool"`), the
    `/menu` family of commands (`source="command"`, PROMPT_FIX_18) and the
    malformed-sentinel fallbacks (`source="sentinel_fallback"`), so every
    surface produces the same effective menu and the same state write.

    NOTHING is deleted: `_apply_flow_result` resets the flow fields to MENU
    (its unconditional writes also clear flow_selected_professional_id /
    flow_selected_insurance) and the effective menu bubbles go out. History,
    the patient row, appointments, deposits and Google Calendar events all stay
    untouched - contrast `_handle_remove_context_command`, which is the only
    destructive path and is reachable only by its own exact literal command.

    Idempotent by construction: it consumes no input and derives the menu from
    the tenant + roster, so running it twice sends the same menu twice and
    leaves the same state.
    """
    if tenant is None:
        logger.warning(
            "worker_show_main_menu_without_tenant",
            conversation_id=str(reply.conversation_id),
            source=source,
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
    rendered = await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )
    logger.info(
        "conversation_menu_rendered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        source=source,
        # The bot was active for this turn by construction (handover is checked
        # in `_persist_inbound_message`); recorded so the pairing with
        # `conversation_menu_requested` stays unambiguous in the logs.
        handover="bot_active",
        rendered=rendered,
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
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
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
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
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
        # Only the single-appointment reschedule opens the day picker in this
        # turn; every other branch (cancel, or 2+ appointments to pick from)
        # needs no agenda at all, so nothing is built for them.
        manage_calendar = None
        if action == "reschedule" and len(appointments) == 1:
            manage_calendar = await _appointment_calendar(
                session,
                tenant,
                _appointment_calendar_target(
                    appointments[0], await list_active_professionals(session, tenant.id)
                ),
            )

    result = await enter_manage_action(
        action, tenant, appointments, professionals, calendar=manage_calendar
    )
    await _apply_flow_result(
        reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
    )


async def _handle_start_guided_booking(
    reply: _ReplyContext,
    reply_text: str,
    tenant: Tenant | None,
    professionals: list | None,
    patient_wa: str | None,
    redis=None,
    waba_token: str | None = None,
) -> None:
    """LLM hand-back: resume the booking in the button flow, service in hand.

    The agent's OPTIONAL offer (ai/tools.py::start_guided_booking) — it decided
    the free-text conversation had got as far as naming the service and that
    buttons should take it from there. Where the flow resumes is
    `services/flow_router.py::enter_guided_booking`'s decision, not this
    function's: convênio when the clinic collects it, the day picker when it
    does not, exactly as the "Sim, agendar" tap behaves. This orchestrates —
    it re-reads state, builds the right calendar, and pipes the result to
    `_apply_flow_result` like the other three hand-back handlers.

    Everything is re-read FRESH in its own session, the same reason
    `_handle_manage_appointment` gives: the model may have spent several
    tool-call turns since whatever this turn preloaded, and the roster/selection
    it saw could be stale.

    WHOSE agenda the day picker reads is the one thing worth getting right.
    `_appointment_calendar_target` answers it with the machinery the manage
    flow already uses: the conversation's selected professional when it still
    resolves, the tenant calendar when no doctor was ever picked (on a clinic
    with a single active professional `load_tenant_config` has already resolved
    THEIR credentials into it), and None when a selection no longer resolves —
    which makes the picker reply `calendar_unavailable` instead of listing days
    off whichever agenda happened to be at hand.

    A multi-professional tenant is turned away here rather than served. The
    tool is already withheld from it (`_flow_handback_tools`), but that gate
    judges by the roster THIS TURN loaded, and a failed roster load maps to
    UNKNOWN and hands the tool over anyway — so the roster re-read above is
    also the second lock, and a clinic that turns out to be multi-doctor gets
    the menu instead of a picker built on the clinic-level agenda.
    """
    appointment_type = reply_text[len(START_GUIDED_BOOKING_SENTINEL_PREFIX) :].strip() or None
    if tenant is None or not flows_enabled(tenant):
        # The tool is only ever exposed to flow-enabled tenants, so this is a
        # defensive count-only warning, same style as _handle_manage_appointment.
        logger.warning(
            "worker_start_guided_booking_without_flows",
            conversation_id=str(reply.conversation_id),
        )
        return

    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        selected_id = (
            conversation.flow_selected_professional_id if conversation is not None else None
        )
        selected_insurance = (
            conversation.flow_selected_insurance if conversation is not None else None
        )
        professional_rows = await list_active_professionals(session, tenant.id)
        service_catalog = await load_service_catalog(session, tenant.id)
        booking_calendar = await _appointment_calendar(
            session,
            tenant,
            _appointment_calendar_target({"professional_id": selected_id}, professional_rows),
        )

    # The topology decided on the FRESH roster, not on whatever this turn saw.
    # `_flow_handback_tools` withholds the tool from a multi tenant, but it
    # judges by the roster the turn loaded — and that load can fail, which maps
    # to UNKNOWN and hands the tool over anyway. A clinic that is really
    # multi-doctor would then open a picker built on the CLINIC-level agenda:
    # days no individual doctor may have free. The menu is the honest answer,
    # and it is where a multi-doctor booking is supposed to start.
    if booking_topology(professional_rows) == BOOKING_TOPOLOGY_MULTI:
        logger.warning(
            "worker_start_guided_booking_multi_professional",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        await _handle_show_main_menu(
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="sentinel_fallback",
        )
        return

    # The SAME tenant-shaped snapshot `route()` always receives — never the raw
    # ORM row. It is what resolves the clinic's canonical catalog AND, on a
    # clinic with one active professional, substitutes THAT professional's own
    # services (see `_flow_tenant_snapshot`). Passing the row instead would
    # read the legacy `tenants.appointment_types` column, which is empty on
    # exactly the clinics that configure everything per-professional — the day
    # picker would then slot on the clinic default instead of the service's own
    # duration, and offer the patient the wrong lengths.
    tenant_snapshot = _flow_tenant_snapshot(tenant, professional_rows, service_catalog)

    result = await enter_guided_booking(
        tenant_snapshot,
        booking_calendar,
        appointment_type,
        conversation_id=reply.conversation_id,
        professional_id=selected_id,
        insurance=selected_insurance,
        professionals=professionals,
    )
    logger.info(
        "conversation_guided_booking_entered",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        # Enums and flags only: whether a service came through and which step
        # the flow resumed at. Never the service name, never the convênio.
        has_type=appointment_type is not None,
        flow_step=result.flow_step,
    )
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
        logger.info("worker_audio_rate_limited", wa_id_suffix=wa_suffix(wa_id))
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

    # Fail closed (PROMPT_FIX_21): the tenant's OWN decrypted token, never the
    # global META_ACCESS_TOKEN. It authenticates BOTH the Graph media download
    # and the clarification reply, so a missing credential must stop the job
    # before either - which also means zero STT spend on an unusable turn.
    reply_client = _tenant_client(tenant, waba_token)
    if reply_client is None:
        logger.error("worker_audio_no_token", tenant_id=str(tenant.id), wam_id=message_id)
        return
    token = waba_token

    # A ValueError here means TranscriptionConfig rejected the configured STT
    # model/provider combo - a misconfiguration, not a per-message failure.
    # Deliberately left to propagate loudly into the arq error logs.
    config = _transcription_config()

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


async def _claim_event(key: str) -> bool:
    """Insert `key` into the ProcessedEvent ledger. True iff THIS call claimed it."""
    async with async_session_factory() as session:
        try:
            async with session.begin():
                existing = await session.scalar(
                    select(ProcessedEvent.id).where(ProcessedEvent.event_id == key)
                )
                if existing is not None:
                    return False
                session.add(ProcessedEvent(event_id=key))
        except IntegrityError:
            return False
    return True


async def _release_event(key: str) -> None:
    """Give back a claim whose send did not happen — nothing more.

    A release states one fact: the key was NOT consumed, so whoever runs next
    is free to claim it. It does not schedule anything and it does not promise
    a retry. The caller is what decides whether one exists — in
    `send_cancellation_notice` that means raising `arq.Retry` right after
    releasing, which is the only thing that actually re-runs the job.
    """
    try:
        async with async_session_factory() as session:
            async with session.begin():
                await session.execute(delete(ProcessedEvent).where(ProcessedEvent.event_id == key))
    except Exception as exc:
        logger.warning("cancellation_notice_release_failed", key=key, error=str(exc))


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
