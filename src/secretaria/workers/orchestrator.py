"""orchestrator - split out of workers/tasks.py (TASK-023)."""

from types import SimpleNamespace
from uuid import uuid4

from secretaria.ai.formatter import (
    parse,
)
from secretaria.ai.graph import (
    BOOKING_DRAFT_SENTINEL_PREFIX,
    CALENDAR_UNAVAILABLE_SENTINEL,
    HUMAN_HANDOFF_OFFER_SENTINEL,
    HUMAN_HANDOFF_SENTINEL_PREFIX,
    MANAGE_APPOINTMENT_SENTINEL_PREFIX,
    SELECT_PROFESSIONAL_SENTINEL_PREFIX,
    SHOW_MAIN_MENU_SENTINEL,
    START_GUIDED_BOOKING_SENTINEL_PREFIX,
    run_agent,
    split_handback_intro,
)
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    FlowState,
    Patient,
    Professional,
    Tenant,
)
from secretaria.plugins.base import InboundContext
from secretaria.plugins.registry import agent_tools_for, run_on_inbound
from secretaria.services.appointment_edit import EditContext
from secretaria.services.booking_scope import (
    booking_topology,
)
from secretaria.services.calendar import (
    CalendarService,
)
from secretaria.services.entitlements_client import get_entitlements
from secretaria.services.flow_router import (
    LABEL_CANCEL_APPT,
    LABEL_MANAGE_APPOINTMENT,
    LABEL_OTHER,
    LABEL_RESCHEDULE,
    FlowRouterResult,
    MenuBubble,
    ai_draft_v2_enabled,
    flows_enabled,
    main_menu_buttons,
    manage_label,
    menu_label,
    resume_bubbles,
)
from secretaria.services.greeting_template import (
    CONSENT_REMINDER_MESSAGE,
)
from secretaria.services.insurance_catalog import (
    load_tenant_insurance,
)
from secretaria.services.llm_context import build_conversation_state
from secretaria.services.patient_context import (
    PatientOpeningState,
    load_upcoming_appointments,
    resolve_patient_opening_state,
)
from secretaria.services.patient_name import (
    NAME_INVALID_MESSAGE,
    NAME_PAUSED_MESSAGE,
    NAME_REQUEST_MESSAGE,
)
from secretaria.services.pending_identity import (
    CODE_ACCEPTED_MESSAGE,
    CODE_GIVE_UP_MESSAGE,
    CODE_INVALID_MESSAGE,
    CODE_NOTICE_BUTTONS,
    EMAIL_CLAIM_RETRY_MESSAGE,
    EMAIL_INVALID_MESSAGE,
    EMAIL_PAUSED_MESSAGE,
    EMAIL_REQUEST_MESSAGE,
    ClaimOutcome,
    ClaimResult,
    RequestCodeOutcome,
    RequestCodeResult,
    VerifyOutcome,
    VerifyResult,
    booking_gate_reprompt_body,
    claim_email,
    report_name,
    request_code,
    verify_code,
)
from secretaria.services.sensitive_claim_guard import guard_reply
from secretaria.services.service_catalog import (
    load_service_catalog,
)
from secretaria.services.tenant_config import (
    get_waba_token,
    list_active_professionals,
    load_tenant_config,
    resolve_professional_calendar,
)
from secretaria.services.turn_safety_net import (
    TURN_FALLBACK_MESSAGE,
    begin_turn,
    end_turn,
    fallback_allowed,
    hold_intro,
    sends_in_turn,
    take_held_intro,
)
from secretaria.services.typing_indicator import clear_typing, mark_typing
from secretaria.workers.portal.attachments import (
    _handle_attachment_received,
)
from secretaria.workers.portal.identity import (
    _account_code_dead_end,
    _continue_after_email_claim,
    _finish_account_code_before_consent,
    _handle_identity_card_action,
    _handle_pre_consent_identity,
    _send_code_notice,
)
from secretaria.workers.shared.actions import (
    _handle_action_button,
    _handle_greeting_button_unavailable,
)
from secretaria.workers.shared.appointment_edit_support import build_edit_context
from secretaria.workers.shared.booking_hold import (
    _hold_minutes_left,
    _hold_when,
    _is_agent_sentinel,
    _promote_booking_hold,
)
from secretaria.workers.shared.channel_policy import policy_for
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.degrade import (
    _handle_service_unavailable,
)
from secretaria.workers.shared.dispatch import (
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
    _flow_professionals,
    _flow_tenant_snapshot,
)
from secretaria.workers.shared.handover import (
    _handle_calendar_unavailable,
    _handle_human_handoff,
)
from secretaria.workers.shared.llm_context import (
    _ai_toolset_v2,
    _appointment_context_text,
    _flow_handback_tools,
    _flow_turn_calendar,
    _label_match_body,
    _llm_activation_reason,
    _manage_owner_calendar_target,
    _should_inject_appointment_context,
    _should_inject_post_consult_knowledge,
)
from secretaria.workers.shared.reminder_opening import (
    _send_reminder_opening,
)
from secretaria.workers.shared.sentinels import (
    _handle_manage_appointment,
    _handle_offer_human_handoff,
    _handle_select_professional,
    _handle_set_booking_draft,
    _handle_show_main_menu,
    _handle_start_guided_booking,
)
from secretaria.workers.shared.state_expiry import (
    _write_flow_state,
)

logger = get_logger(__name__)


async def _send_turn_fallback(reply: _ReplyContext, redis, *, cause: str) -> None:
    """Send the fixed apology for a turn that ended without an answer. Never raises.

    Re-checks the entitlement itself: the one silence that IS intended is the
    unentitled tenant (`bot_reply_suppressed_unentitled`), and the net must not
    turn a deliberate no-reply into a reply from a clinic that is not paying.
    A tenant whose entitlement cannot be read is treated the same way it is on
    the main path (fail closed) - but logged as `entitlement_unknown`, not as
    "unentitled", so an outage at brain-api is not mistaken for churn.
    """
    try:
        tenant_id = reply.tenant_id
        async with async_session_factory() as session:
            if tenant_id is None and reply.conversation_id is not None:
                conversation = await session.get(Conversation, reply.conversation_id)
                tenant_id = conversation.tenant_id if conversation is not None else None
            tenant = await session.get(Tenant, tenant_id) if tenant_id is not None else None
            waba_token = await get_waba_token(session, tenant.id) if tenant is not None else None
        if tenant is None:
            logger.error(
                "turn_fallback_no_tenant",
                cause=cause,
                conversation_id=str(reply.conversation_id),
            )
            return
        summary = await get_entitlements(tenant.id, redis)
        if summary is None or not (summary.active and summary.secretaria_enabled):
            logger.warning(
                "turn_fallback_skipped_not_entitled",
                cause=cause,
                tenant_id=str(tenant.id),
                entitlement_unknown=summary is None,
                conversation_id=str(reply.conversation_id),
            )
            return
        if not await fallback_allowed(redis, reply.conversation_id):
            logger.warning(
                "turn_fallback_throttled",
                cause=cause,
                tenant_id=str(tenant.id),
                conversation_id=str(reply.conversation_id),
            )
            return
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=TURN_FALLBACK_MESSAGE,
            event="turn_fallback_sent",
        )
    except Exception as exc:
        logger.error(
            "turn_fallback_failed",
            cause=cause,
            error_type=type(exc).__name__,
            conversation_id=str(reply.conversation_id),
        )

async def _send_bot_reply(reply: _ReplyContext, redis=None) -> None:
    """Answer one inbound turn - and guarantee the patient is never left in silence.

    Wraps the real pipeline (`_send_bot_reply_inner`). Two ways a turn used to
    end with nothing sent: it RAISED (the exception left the arq job and the
    patient saw no answer), or it RETURNED having sent nothing (a model reply
    that parsed to zero bubbles, a downstream send that failed and was
    logged-and-forgotten). Both are caught here by the turn's send ledger (a
    counter bumped where a message really leaves - see `begin_turn`), and
    answered with one fixed, LLM-free apology (`services/turn_safety_net.py`,
    rate-limited per conversation).

    A turn without a conversation (the inactive-tenant degrade) has nobody to
    rate-limit against and its own dedicated handler, so it is left alone.
    """
    token = begin_turn()
    typing_on = (
        policy_for(reply.channel).shows_typing_indicator and reply.conversation_id is not None
    )
    typing_turn_id = uuid4().hex if typing_on else None
    cause = "silent_return"
    try:
        if typing_on:
            await mark_typing(redis, reply.conversation_id, "automation", turn_id=typing_turn_id)
        try:
            await _send_bot_reply_inner(reply, redis=redis)
        except Exception as exc:
            cause = "exception"
            logger.error(
                "turn_failed",
                error_type=type(exc).__name__,
                conversation_id=str(reply.conversation_id),
                exc_info=True,
            )
        sent = sends_in_turn()
    finally:
        end_turn(token)
        if typing_on:
            await clear_typing(redis, reply.conversation_id, "automation", turn_id=typing_turn_id)
    if sent > 0 or reply.conversation_id is None:
        return
    logger.warning(
        "turn_unanswered",
        cause=cause,
        conversation_id=str(reply.conversation_id),
        channel=reply.channel,
    )
    await _send_turn_fallback(reply, redis, cause=cause)

async def _send_bot_reply_inner(reply: _ReplyContext, redis=None) -> None:
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

    # A file arrived: the fixed receipt, behind the same entitlement gate.
    if reply.attachment_received:
        await _handle_attachment_received(reply, redis=redis)
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
    edit_context: EditContext | None = None
    patient_name = None
    patient_wa = reply.patient_ref
    # Whether this patient still owes LGPD consent. Only the identity turns
    # read it: a code asked BEFORE consent (an e-mail that already had an
    # account) has no appointment behind it, so every wording and every exit of
    # that wait differs from the post-booking code gate's.
    patient_owes_consent = False
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
                        patient_owes_consent = patient.lgpd_accepted_at is None
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
                    # Same one-read-per-turn rule for the convênio catalog: the
                    # clinic's plans and which doctor takes which, so the pure
                    # router can mark doctors without a query of its own.
                    tenant_insurance = await load_tenant_insurance(session, tenant.id)
                    flow_professionals = _flow_professionals(professional_rows, service_catalog)
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
                            flow_attendee_name=conversation.flow_attendee_name,
                            flow_draft=conversation.flow_draft,
                            flow_replaces_appointment_id=conversation.flow_replaces_appointment_id,
                            flow_edit_draft=conversation.flow_edit_draft,
                            patient_id=conversation.patient_id,
                        ),
                        _flow_tenant_snapshot(
                            tenant, professional_rows, service_catalog, tenant_insurance
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
                        conversation.flow_state in (
                            FlowState.MANAGE_BOOKING, FlowState.LLM, FlowState.EDIT_BOOKING,
                        )
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
                    if conversation.flow_state == FlowState.EDIT_BOOKING:
                        # TASK-032 R6: every agenda the edit may touch + the Pix guards.
                        edit_context = await build_edit_context(
                            session, tenant, tenant_config, conversation,
                            professional_rows, upcoming_appointments,
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
            # `summary is None` is "could not READ the entitlement" (brain-api
            # down / no tenant resolved), not "tenant does not pay". The two
            # look identical to the patient (silence) and opposite to the
            # operator, so the log has to tell them apart.
            entitlement_unknown=summary is None,
            tenant_resolved=tenant is not None,
            conversation_id=str(reply.conversation_id),
        )
        return

    # First-contact/returning greeting: deterministic, verbatim and sent with
    # this tenant's own WhatsApp number/token. This intentionally runs only
    # after entitlement and tenant credentials have been resolved.
    # Consent still owed and the subject said something other than "Concordo":
    # the re-prompt is the WHOLE turn. Placed before the greeting branch so
    # nothing else can answer first.
    # --- Brain-Message inline identity turns ---------------------------
    # All three own the whole turn and all three sit ABOVE the consent
    # reminder, mirroring the gate order in `_persist_inbound_message`: a
    # visitor still owing an address is asked for it, not for consent.
    # Unreachable on WhatsApp — only the Brain-Message branch of that gate
    # ever sets these fields.
    # The name question's own "quer continuar?" answer. Channel-neutral, like
    # the state: "Sim" asks again, "Não" pauses and the next message meets the
    # LGPD re-prompt — the name is never a second wall in front of consent.
    if (
        reply.reactivation is not None
        and reply.reactivation.origin == FlowState.AWAITING_NAME.value
    ):
        if reply.reactivation.kind == "reset":
            await _send_plain_reply(
                reply,
                tenant=tenant,
                waba_token=waba_token,
                body=NAME_PAUSED_MESSAGE,
                event="patient_name_reactivation_declined",
            )
            return
        await _write_flow_state(reply.conversation_id, FlowState.AWAITING_NAME)
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=NAME_REQUEST_MESSAGE,
            event="patient_name_reactivated",
        )
        return

    if reply.reactivation is not None and reply.reactivation.origin in (
        FlowState.AWAITING_EMAIL.value,
        FlowState.AWAITING_EMAIL_CODE.value,
    ):
        origin = FlowState(reply.reactivation.origin)
        if reply.reactivation.kind == "reset":
            # A code wait BEFORE consent has no consultation to reassure
            # about: it pauses exactly like the e-mail step it came from.
            body = (
                EMAIL_PAUSED_MESSAGE
                if origin == FlowState.AWAITING_EMAIL or patient_owes_consent
                else CODE_GIVE_UP_MESSAGE
            )
            await _send_plain_reply(
                reply,
                tenant=tenant,
                waba_token=waba_token,
                body=body,
                event="pending_identity_reactivation_declined",
            )
            return

        if origin == FlowState.AWAITING_EMAIL:
            await _write_flow_state(reply.conversation_id, FlowState.AWAITING_EMAIL)
            await _send_plain_reply(
                reply,
                tenant=tenant,
                waba_token=waba_token,
                body=EMAIL_REQUEST_MESSAGE,
                event="pending_email_reactivated",
            )
            return

        result = RequestCodeResult(RequestCodeOutcome.UNAVAILABLE)
        if reply.tenant_id is not None:
            result = await request_code(reply.tenant_id, reply.patient_ref)
        if result.outcome is RequestCodeOutcome.SENT:
            await _write_flow_state(reply.conversation_id, FlowState.AWAITING_EMAIL_CODE)
            # The card, not a bare line: a resumed wait offers the same three
            # exits the original notice did, and names the same inbox.
            await _send_code_notice(
                reply,
                tenant=tenant,
                waba_token=waba_token,
                email_masked=result.email_masked,
                event="pending_code_reactivation_resolved",
                pre_consent=patient_owes_consent,
            )
            return
        if patient_owes_consent:
            await _account_code_dead_end(
                reply,
                tenant=tenant,
                waba_token=waba_token,
                event="pending_code_reactivation_resolved",
            )
            return
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=CODE_GIVE_UP_MESSAGE,
            event="pending_code_reactivation_resolved",
        )
        return

    if reply.identity_action is not None:
        await _handle_identity_card_action(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            professionals=flow_professionals,
            patient_wa=patient_wa,
            redis=redis,
            pre_consent=patient_owes_consent,
        )
        return

    if reply.pending_code_reprompt is not None:
        # The card again, saying the reservation is still standing. No mask:
        # brain-api hands one back only on a fresh `request_code`, and this
        # turn asked for no new code - so the notice names the window and the
        # minutes instead of inventing an inbox.
        held = reply.pending_code_reprompt
        await _send_buttons_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=booking_gate_reprompt_body(
                None,
                when=_hold_when(held, tenant),
                hold_minutes=_hold_minutes_left(held),
            ),
            buttons=list(CODE_NOTICE_BUTTONS),
            event="booking_gate_reprompt_sent",
        )
        return

    if reply.pending_email_invalid:
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=EMAIL_INVALID_MESSAGE,
            event="pending_email_reprompt_sent",
        )
        return

    # --- The name question's two answers (both channels) -----------------
    if reply.name_invalid:
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=NAME_INVALID_MESSAGE,
            event="patient_name_reprompt_sent",
        )
        return

    if reply.name_captured:
        # The name (if any) is already on `Patient.name`, committed with the
        # inbound. On the Portal it also goes to brain-api, which keeps it for
        # the next clinic this ACCOUNT opens (`report_name`); best effort, after
        # the commit, so a failure costs one future question and nothing now.
        if (
            policy_for(reply.channel).reports_name_to_account
            and reply.tenant_id is not None
            and (patient_name or "").strip()
        ):
            await report_name(reply.tenant_id, reply.patient_ref, patient_name)
        if not patient_owes_consent:
            # A verified account asked for its name at a clinic it had never
            # talked to (`_handle_pre_consent_identity`): consent was mirrored
            # from the account before the question, so the menu is next.
            await _handle_show_main_menu(
                reply,
                tenant,
                flow_professionals,
                patient_wa,
                redis=redis,
                waba_token=waba_token,
                source="name_captured",
            )
            return
        # Consent is next, on every channel — the same notice, with the same
        # button, that followed the greeting before this step existed.
        await _send_consent_notice(reply, tenant=tenant, waba_token=waba_token)
        return

    if reply.pending_email_claim is not None:
        # Claim first, THEN consent. The owner fixed that sequence, so a failed
        # wire call keeps the state and asks again; it is never converted into
        # implicit permission to skip the e-mail step.
        claim = ClaimResult(ClaimOutcome.UNAVAILABLE)
        if reply.tenant_id is not None:
            claim = await claim_email(reply.tenant_id, reply.patient_ref, reply.pending_email_claim)
        outcome = claim.outcome
        if outcome is ClaimOutcome.CLAIMED and patient_owes_consent:
            # The opening: brain-api just told us whether this inbox is new.
            await _continue_after_email_claim(reply, claim, tenant=tenant, waba_token=waba_token)
            return
        if outcome is ClaimOutcome.CLAIMED:
            # A patient who already consented re-typing an address ("📩 Mudar
            # e-mail" on the post-booking card): unchanged from before the
            # name step existed.
            await _write_flow_state(reply.conversation_id, FlowState.IDLE)
            await _send_consent_notice(reply, tenant=tenant, waba_token=waba_token)
            return
        # A second browser may have completed the same account between the
        # first-contact probe and this claim. Re-read only on the authoritative
        # NOT_PENDING answer so that race becomes the verified-account path,
        # not an hour of pointless e-mail retries.
        if outcome is ClaimOutcome.NOT_PENDING and await _handle_pre_consent_identity(
            reply,
            tenant=tenant,
            professionals=flow_professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        ):
            return
        logger.warning(
            "pending_email_claim_not_recorded",
            outcome=outcome.value,
            conversation_id=str(reply.conversation_id),
            tenant_id=str(reply.tenant_id),
        )
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=EMAIL_CLAIM_RETRY_MESSAGE,
            event="pending_email_claim_retry_sent",
        )
        return

    if reply.pending_code is not None:
        verification = VerifyResult(VerifyOutcome.UNAVAILABLE)
        if reply.tenant_id is not None:
            verification = await verify_code(reply.tenant_id, reply.patient_ref, reply.pending_code)
        result = verification.outcome
        if patient_owes_consent and result is not VerifyOutcome.INVALID:
            # The KNOWN-ADDRESS code, asked before consent. No hold, no
            # appointment: success makes this a verified account, which is
            # exactly the case `_handle_pre_consent_identity` already handles
            # (mirror the account's consent, open the menu); failure carries on
            # as a new visitor would, with the LGPD notice. A wrong code falls
            # through to the shared re-prompt below.
            await _finish_account_code_before_consent(
                reply,
                verified=result is VerifyOutcome.VERIFIED,
                tenant=tenant,
                waba_token=waba_token,
                professionals=flow_professionals,
                patient_wa=patient_wa,
                redis=redis,
                account_name=verification.patient_name,
            )
            return
        if result is VerifyOutcome.VERIFIED:
            body, next_state = CODE_ACCEPTED_MESSAGE, FlowState.IDLE
            # The code was a GATE for this conversation: the appointment does
            # not exist yet and this is the moment it is allowed to. Runs
            # BEFORE the acceptance message is sent so the patient reads the
            # two facts in the order they happened - account active, then
            # appointment confirmed - and so a promotion that fails never
            # follows a message implying it worked.
            promoted = await _promote_booking_hold(
                reply,
                tenant=tenant,
                waba_token=waba_token,
                professionals=flow_professionals,
                redis=redis,
            )
            if promoted is not None:
                body = CODE_ACCEPTED_MESSAGE + chr(10) + chr(10) + promoted
        elif result is VerifyOutcome.INVALID:
            # Stay in the state: brain-api owns the attempt budget and the
            # 10-minute life of the challenge, and re-prompting is what lets
            # the patient spend the attempts they still have. Duplicating that
            # count here would be a second source of truth (see
            # `services/pending_identity.py`).
            body, next_state = CODE_INVALID_MESSAGE, FlowState.AWAITING_EMAIL_CODE
        else:
            # NOT_PENDING / NO_EMAIL / UNAVAILABLE: nothing the patient can act
            # on. Leave the state so the conversation is usable again, and say
            # the appointment stands — the fact they actually need.
            body, next_state = CODE_GIVE_UP_MESSAGE, FlowState.IDLE
        if next_state is not FlowState.AWAITING_EMAIL_CODE:
            await _write_flow_state(reply.conversation_id, next_state)
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=body,
            event="pending_code_reply_sent",
        )
        return

    if reply.send_consent_reminder:
        if reply.probe_pending_identity and await _handle_pre_consent_identity(
            reply,
            tenant=tenant,
            professionals=flow_professionals,
            patient_wa=patient_wa,
            redis=redis,
            waba_token=waba_token,
        ):
            return
        await _send_consent_notice(
            reply, tenant=tenant, waba_token=waba_token, body=CONSENT_REMINDER_MESSAGE
        )
        return

    if reply.greeting_override is not None:
        await _send_greeting(reply, tenant=tenant, waba_token=waba_token)
        if reply.send_name_request:
            # WhatsApp first contact: the name question in the consent
            # notice's slot (`_asks_name_at_first_contact`); the notice
            # follows the answer. The state is written here, past the
            # entitlement gate, as the Portal's e-mail step writes its own.
            await _write_flow_state(reply.conversation_id, FlowState.AWAITING_NAME)
            await _send_plain_reply(
                reply,
                tenant=tenant,
                waba_token=waba_token,
                body=NAME_REQUEST_MESSAGE,
                event="patient_name_requested",
            )
            return
        if reply.send_consent_notice:
            # Brain-Message first contact: the e-mail question takes this slot
            # when — and only when — brain-api says the visitor still owes an
            # address. Probed rather than assumed, because a patient who
            # already verified at ANOTHER clinic is a first contact HERE and
            # must not be asked to prove the same address twice
            # (`IdentityState.VERIFIED`).
            #
            # Every other answer falls through to today's behaviour, including
            # UNAVAILABLE: if brain-api cannot be reached, consent is the thing
            # that must still happen, and an unasked address costs nothing that
            # a later turn cannot recover.
            if reply.probe_pending_identity and await _handle_pre_consent_identity(
                reply,
                tenant=tenant,
                professionals=flow_professionals,
                patient_wa=patient_wa,
                redis=redis,
                waba_token=waba_token,
            ):
                return
            await _send_consent_notice(reply, tenant=tenant, waba_token=waba_token)
        return

    # TASK-032 R3: the appointment reminder opens the chat. Past the entitlement
    # gate on purpose (an unentitled clinic sends nothing and plans no row); the
    # patient's own message is then answered by everything below, minus the
    # generic menu (`_run_flow`, and the `show_main_menu` hand-back further down).
    if reply.reminder_opening_appointment_id is not None and tenant is not None:
        await _send_reminder_opening(reply, tenant=tenant, waba_token=waba_token)

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
                        labels=main_menu_buttons(),
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
                    flow_attendee_name=conv_snapshot.flow_attendee_name,
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
            edit_context=edit_context,
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
            # TASK-030 P3: the "(ref ...)" manage_existing_appointment v2 takes.
            with_refs=ai_draft_v2_enabled(tenant),
            service_guides=tenant_config.service_guides if tenant_config is not None else None,
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

    conversation_state_text = build_conversation_state(
        flow_snapshot[0] if flow_snapshot is not None else None,
        flow_snapshot[1] if flow_snapshot is not None else None,
        flow_professionals,
    )
    logger.info(
        "llm_conversation_state_built",
        conversation_id=str(reply.conversation_id),
        has_state=conversation_state_text is not None,
    )

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
        conversation_state=conversation_state_text,
        toolset_v2=_ai_toolset_v2(tenant),
    )

    # The model may not announce an action no tool performed. Applied HERE -
    # after run_agent and before every send path below, so there is exactly
    # one place an LLM reply can reach the patient from and exactly one place
    # this is checked. The sentinels are protocol strings, not prose, so they
    # are passed through untouched. `proven_actions` is empty because the
    # agent has no identity/payment tool at all today; the day one exists it
    # passes its own key and the sentence becomes sayable. See
    # services/sensitive_claim_guard.py for the production incident.
    # A hand-back may carry the agent's own words for the patient (TASK-038,
    # ai/graph.py::HANDBACK_INTRO_PREFIX): split them off first so every sentinel
    # below matches exactly as before.
    handback_intro, reply_text = split_handback_intro(reply_text)
    if not _is_agent_sentinel(reply_text):
        reply_text = guard_reply(
            reply_text,
            proven_actions=frozenset(),
            conversation_id=str(reply.conversation_id),
        )
    elif handback_intro:
        # Goes out in front of the card the hand-back renders (held until then, see
        # services/turn_safety_net.py::hold_intro), so the patient reads the answer to
        # what they asked instead of the same list again as if unheard - and a
        # hand-back that sends nothing still gets the safety-net apology. Same honesty
        # filter as any LLM prose; words it would rewrite are dropped, never apologised
        # for in front of a card.
        guarded = guard_reply(
            handback_intro,
            proven_actions=frozenset(),
            conversation_id=str(reply.conversation_id),
        )
        if guarded == handback_intro:
            hold_intro(handback_intro)

    # A tool failed because the calendar is unreachable: tell the patient and
    # hand the conversation to a human secretary instead of faking success.
    if reply_text == CALENDAR_UNAVAILABLE_SENTINEL:
        await _handle_calendar_unavailable(reply, redis=redis, tenant=tenant, waba_token=waba_token)
        return

    # The agent asked to hand the patient back to the deterministic flow:
    # non-destructive menu return, or re-entry at a confirmed doctor's
    # greeting + services. Mirrors the calendar sentinel short-circuit above.
    if reply_text == SHOW_MAIN_MENU_SENTINEL:
        if reply.reminder_opening_appointment_id is not None:
            # The reminder card that opened this turn already offers "Outro".
            logger.info(
                "reminder_opening_menu_suppressed",
                source="agent",
                conversation_id=str(reply.conversation_id),
            )
            # The agent's short answer rode in as a held intro for the menu card
            # that is not coming: send it on its own so the question is answered.
            held = take_held_intro()
            if held and tenant is not None:
                await _send_plain_reply(
                    reply,
                    tenant=tenant,
                    waba_token=waba_token,
                    body=held,
                    event="reminder_opening_held_intro_sent",
                )
            return
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
    if reply_text == HUMAN_HANDOFF_OFFER_SENTINEL:
        await _handle_offer_human_handoff(
            reply,
            tenant,
            flow_snapshot,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
        )
        return
    if reply_text.startswith(HUMAN_HANDOFF_SENTINEL_PREFIX):
        await _handle_human_handoff(
            reply,
            reply_text[len(HUMAN_HANDOFF_SENTINEL_PREFIX) :],
            tenant,
            flow_snapshot,
            redis=redis,
            waba_token=waba_token,
        )
        return

    if reply_text.startswith(BOOKING_DRAFT_SENTINEL_PREFIX):
        await _handle_set_booking_draft(
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
