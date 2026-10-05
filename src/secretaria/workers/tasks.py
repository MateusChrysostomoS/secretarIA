"""arq job functions - facade over the split modules (TASK-023).

Everything that used to live here moved under workers/{shared,whatsapp,portal}/ and
workers/{turn_router,orchestrator}.py. This module only re-exports it so that
`arq_worker.py` and the tests keep importing `secretaria.workers.tasks.<name>`.
Nothing may import this module from inside workers/ - it would create a cycle.
"""

# ruff: noqa: F401
import json
import math
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
    BOOKING_DRAFT_SENTINEL_PREFIX,
    CALENDAR_UNAVAILABLE_SENTINEL,
    HUMAN_HANDOFF_SENTINEL_PREFIX,
    MANAGE_APPOINTMENT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    SHOW_MAIN_MENU_SENTINEL,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
    run_agent,
)
from secretaria.ai.tools import (
    manage_existing_appointment,
    request_human_handoff,
    set_booking_draft,
    start_guided_booking,
)
from secretaria.config import get_settings
from secretaria.core.attachments import attachment_body
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger, wa_suffix
from secretaria.core.whatsapp_limits import (
    EMOJI_SCHEDULE,
    MAX_INTERACTIVE_BODY_CHARS,
    decorate,
    strip_decoration,
    truncate_button_label,
    truncate_plain,
)
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
    WebhookStatus,
    WebhookValue,
    extract_action_button,
    extract_echo_body,
    extract_greeting_button,
    extract_inbound_body,
    extract_inbound_reply_id,
    history_item_is_final,
    inbound_routing_text,
)
from secretaria.services import cancellation_notice
from secretaria.services.appointment_status import (
    SOURCE_BUTTON,
    SOURCE_FLOW,
    log_status_transition,
)
from secretaria.services.attendee import (
    CONSENT_KIND_THIRD_PARTY_BOOKING,
    CONSENT_LEGAL_BASIS_THIRD_PARTY_BOOKING,
)
from secretaria.services.booking_hold import (
    HOLD_TTL_MINUTES,
    BookingGate,
    HeldSlot,
    latest_hold_in,
    live_hold_in,
    release_in as release_hold_in,
)
from secretaria.services.booking_scope import (
    BOOKING_TOPOLOGY_MULTI,
    booking_topology,
    canonical_service_name,
    sole_active_professional,
)
from secretaria.services.brain_professionals import fetch_professional_emails
from secretaria.services.calendar import (
    CalendarService,
    CalendarUnavailableError,
    build_event_description,
    build_patient_calendar_link,
)
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
    CHANNEL_WHATSAPP,
    BrainMessageSender,
    ChannelSender,
    sender_persists_outbound,
)
from secretaria.services.email import (
    send_calendar_alert,
    send_cancellation_escalation_alert,
    send_professional_config_incomplete_alert,
    send_transactional_email_message,
)
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.flow_router import (
    ATTENDEE_STEPS,
    LABEL_BOOK,
    LABEL_CANCEL_APPT,
    LABEL_MANAGE_APPOINTMENT,
    LABEL_OTHER,
    LABEL_RESCHEDULE,
    SCOPED_HELP_ESCALATE_MESSAGE,
    STEP_AWAITING_ATTENDEE_AUTH,
    STEP_MANAGE_CANCEL_CONFIRM,
    STEP_MANAGE_DAY,
    STEP_MANAGE_DAY_ESCAPE,
    STEP_MANAGE_DAY_RETRY,
    WHEN_FORMAT,
    FlowRouterResult,
    MenuBubble,
    _enter_professional_services,
    _start_booking,
    classify_yes_no,
    enter_booking,
    enter_decline_reasons,
    enter_guided_booking,
    enter_manage_action,
    enter_rebooking,
    flows_enabled,
    llm_state_ttl_minutes,
    manage_label,
    match_insurance_plan,
    menu_buttons_for,
    menu_label,
    pending_identity_ttl_minutes,
    reactivation_choice_buttons,
    reactivation_continue_prompt,
    reactivation_enabled,
    reactivation_gap_minutes,
    reactivation_prompt_enabled,
    rebooking_candidates,
    resume_bubbles,
    route,
)
from secretaria.services.greeting_template import (
    CONSENT_ACCEPTED_MESSAGE,
    CONSENT_BUTTON_LABEL,
    CONSENT_EVENT_KIND,
    CONSENT_REMINDER_MESSAGE,
    LGPD_CONSENT_MESSAGE,
    clinic_description_budget,
    render_greeting,
)
from secretaria.services.handoff_notification import activate_human_handoff, notify_human_handoff
from secretaria.services.handover import HandoverManager
from secretaria.services.insurance_catalog import (
    TenantInsurance,
    load_tenant_insurance,
    resolve_booking_plan_ids,
)
from secretaria.services.llm_context import build_conversation_state
from secretaria.services.message_status import apply_whatsapp_statuses
from secretaria.services.patient_context import (
    PatientOpeningContext,
    PatientOpeningState,
    load_upcoming_appointments,
    resolve_patient_opening_state,
)
from secretaria.services.patient_name import (
    NAME_INVALID_MESSAGE,
    NAME_PAUSED_MESSAGE,
    NAME_REASKED_STEP,
    NAME_REQUEST_AFTER_EMAIL_MESSAGE,
    NAME_REQUEST_MESSAGE,
    parse_patient_name,
)
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.payments.money import format_brl
from secretaria.services.pending_identity import (
    ACCOUNT_CODE_GIVE_UP_MESSAGE,
    BOOKING_HOLD_EXPIRED_MESSAGE,
    CODE_ACCEPTED_MESSAGE,
    CODE_GIVE_UP_MESSAGE,
    CODE_INVALID_MESSAGE,
    CODE_NOTICE_BUTTONS,
    EMAIL_CLAIM_RETRY_MESSAGE,
    EMAIL_INVALID_MESSAGE,
    EMAIL_PAUSED_MESSAGE,
    EMAIL_REQUEST_MESSAGE,
    EXISTING_ACCOUNT_CODE_BUTTONS,
    IDENTITY_BACK_ACTION,
    IDENTITY_CHANGE_EMAIL_ACTION,
    IDENTITY_RESEND_ACTION,
    ClaimOutcome,
    ClaimResult,
    IdentityState,
    RequestCodeOutcome,
    RequestCodeResult,
    VerifyOutcome,
    VerifyResult,
    booking_gate_body,
    booking_gate_reprompt_body,
    cancel_pending_code,
    claim_email,
    code_notice_body,
    existing_account_code_body,
    identity_action_or_none,
    parse_code,
    parse_email,
    probe_identity,
    report_name,
    request_code,
    verify_code,
)
from secretaria.services.pii_pseudonymization import remember_attendee_name
from secretaria.services.sensitive_claim_guard import guard_reply
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
from secretaria.services.turn_safety_net import (
    TURN_FALLBACK_MESSAGE,
    begin_turn,
    end_turn,
    fallback_allowed,
    sends_in_turn,
)
from secretaria.services.usage_events import emit_usage_event
from secretaria.services.whatsapp import (
    TenantWhatsAppCredentialMissing,
    WhatsAppClient,
    interactive_buttons_record,
    interactive_list_record,
)
from secretaria.workers.orchestrator import (
    _send_bot_reply,
    _send_bot_reply_inner,
    _send_turn_fallback,
)
from secretaria.workers.portal.attachments import (
    ATTACHMENT_RECEIVED_MESSAGE,
    _handle_attachment_received,
)
from secretaria.workers.portal.identity import (
    _account_code_dead_end,
    _adopt_account_name,
    _continue_after_email_claim,
    _conversation_patient_has_name,
    _finish_account_code_before_consent,
    _handle_identity_card_action,
    _handle_pre_consent_identity,
    _record_verified_account_consent,
    _send_code_notice,
)
from secretaria.workers.portal.inbound import (
    BRAIN_MESSAGE_TAP_WINDOW,
    _log_discarded_attachment,
    _persist_brain_message_inbound,
    _validated_brain_message_reply_id,
    offered_reply_ids,
    process_brain_message_inbound,
)
from secretaria.workers.portal.merge import merge_brain_message_visit
from secretaria.workers.portal.open import (
    _open_brain_message_conversation,
    _open_ledger_key,
    _portal_conversation_has,
    process_brain_message_enter,
    process_brain_message_open,
)
from secretaria.workers.shared.actions import (
    _APPOINTMENT_NOT_FOUND_TEXT,
    _GREETING_ACTION_UNAVAILABLE_DEFAULT,
    _GREETING_ACTION_UNAVAILABLE_TEXT,
    _calendar_for_appointment,
    _execute_appointment_cancel,
    _handle_action_button,
    _handle_greeting_button_unavailable,
)
from secretaria.workers.shared.booking_hold import (
    _PROMOTE_FAILED_MESSAGE,
    _hold_minutes_left,
    _hold_when,
    _is_agent_sentinel,
    _log_booking_scope,
    _patient_display_name,
    _promote_booking_hold,
    _release_hold,
    _send_booking_gate_notice,
    _tenant_tzinfo,
)
from secretaria.workers.shared.bubbles import (
    _bubble_buttons,
    _bubble_history_body,
    _bubble_interactive,
    _send_bubble,
    _slots_rows,
)
from secretaria.workers.shared.context import (
    _ReactivationDirective,
    _ReplyContext,
)
from secretaria.workers.shared.db import (
    _conversation_message_count,
    _event_already_processed,
    _get_or_create_conversation,
)
from secretaria.workers.shared.degrade import (
    _handle_service_unavailable,
)
from secretaria.workers.shared.deposit import (
    _RESCHEDULE_PRECHECK_STEPS,
    _apply_deposit_awareness,
    _hours_until_start,
    _pix_retention_warning_line,
    _send_reschedule_limit_buttons,
)
from secretaria.workers.shared.dispatch import (
    _GREETING_ACTION_IDS,
    _GREETING_LLM_ESCAPE_SUFFIX,
    _dispatch_bubbles,
    _send_buttons_reply,
    _send_consent_notice,
    _send_greeting,
    _send_plain_reply,
)
from secretaria.workers.shared.flow_runner import (
    _apply_flow_result,
    _run_flow,
)
from secretaria.workers.shared.greeting import (
    _adapt_greeting_has_upcoming,
    _adapt_greeting_to_state,
    _appointment_doctor_name,
    _asks_name_at_first_contact,
    _compose_upcoming_greeting_body,
    _first_contact_reply,
    _fit_clinic_description,
    _flow_tenant_snapshot,
    _format_appointment_when,
    _greeting_buttons_for,
    _load_upcoming_greeting_data,
    _menu_vocabulary,
    _select_greeting,
    _UpcomingGreetingData,
)
from secretaria.workers.shared.handover import (
    _handle_calendar_unavailable,
    _handle_human_handoff,
    _handle_professional_config_incomplete,
    _set_conversation_human_active,
)
from secretaria.workers.shared.jobs import (
    _claim_event,
    _release_event,
    check_handover_timeouts,
    send_transactional_email,
)
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
    _appointment_context_text,
    _flow_handback_tools,
    _flow_turn_calendar,
    _label_match_body,
    _llm_activation_reason,
    _manage_owner_calendar_target,
    _should_inject_appointment_context,
    _should_inject_post_consult_knowledge,
)
from secretaria.workers.shared.sender import (
    _extract_sent_wam_id,
    _record_outbound,
    _reply_sender,
    _send_simple_text,
    _tenant_client,
)
from secretaria.workers.shared.sentinels import (
    _handle_manage_appointment,
    _handle_select_professional,
    _handle_set_booking_draft,
    _handle_show_main_menu,
    _handle_start_guided_booking,
)
from secretaria.workers.shared.state_expiry import (
    _expire_stale_attendee_step,
    _expire_stale_llm_state,
    _expire_stale_pending_identity_state,
    _pending_identity_reactivation_offer,
    _reactivation_offer,
    _write_flow_state,
)
from secretaria.workers.shared.text import (
    _MENU_COMMANDS,
    _NAME_PATTERNS,
    _NAME_STOPWORDS,
    AUDIO_UNINTELLIGIBLE_MESSAGE,
    CALENDAR_UNAVAILABLE_MESSAGE,
    GREETING_ACTION_HINT,
    GREETING_BRIEF_HEADER,
    GREETING_BRIEF_KEEP,
    GREETING_DETAIL_MAX_CHARS,
    GREETING_REQUIREMENTS_HEADER,
    GREETING_REQUIREMENTS_KEEP,
    JUST_HAD_CONSULT_ATTENDED_LINE,
    JUST_HAD_CONSULT_NEUTRAL_LINE,
    REMOVE_CONTEXT_COMMAND,
    SERVICE_UNAVAILABLE_MESSAGE,
    _as_utc,
    _is_consent_acceptance,
    _render_greeting_template,
    extract_patient_name,
    is_menu_command,
    is_remove_context_command,
)
from secretaria.workers.turn_router import (
    _route_inbound_turn,
)
from secretaria.workers.whatsapp.audio import (
    _mark_audio_event_processed,
    _transcription_config,
    transcribe_audio_message,
)
from secretaria.workers.whatsapp.coexistence import (
    _handle_history,
    _handle_human_echoes,
    _handle_smb_app_state_sync,
    _mark_mode_resolved,
    _persist_human_echo,
)
from secretaria.workers.whatsapp.db import (
    _get_or_create_patient,
    _mark_connected,
    _resolve_tenant,
)
from secretaria.workers.whatsapp.inbound import (
    _STATUS_RETRY_DELAY,
    _apply_statuses,
    _handle_message_statuses,
    _handle_patient_messages,
    _persist_inbound_message,
    process_message_statuses,
    process_webhook_event,
)
from secretaria.workers.whatsapp.notifications import (
    CANCEL_NOTICE_MAX_TRIES,
    CANCEL_NOTICE_RETRY_DEFER_S,
    CANCEL_NOTICE_VALIDITY_S,
    _cancellation_retry_decision,
    _emit_cancellation_usage,
    _escalate_cancellation_failure,
    send_cancellation_notice,
    send_patient_notification,
)
from secretaria.workers.whatsapp.rate_limit import (
    _is_rate_limited,
)
from secretaria.workers.whatsapp.remove_context import (
    REMOVE_CONTEXT_PRESERVED_MESSAGE,
    _handle_remove_context_command,
)

logger = get_logger(__name__)
