"""state_expiry - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    FlowState,
    Patient,
    Tenant,
)
from secretaria.services.flow_router import (
    ATTENDEE_STEPS,
    llm_state_ttl_minutes,
    pending_identity_ttl_minutes,
    reactivation_choice_buttons,
    reactivation_continue_prompt,
    reactivation_enabled,
    reactivation_gap_minutes,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.greeting import (
    _greeting_buttons_for,
)
from secretaria.workers.shared.text import (
    _as_utc,
    _render_greeting_template,
)

logger = get_logger(__name__)


def _expire_stale_pending_identity_state(
    conversation: Conversation,
    tenant: Tenant,
    last_activity_at: datetime | None,
) -> bool:
    """Drop an abandoned identity wait before offering an explicit resume.

    The sibling of `_expire_stale_llm_state`, for the two states
    `services/pending_identity.py` introduces, and it exists for exactly the
    reason that one does: `Conversation` is one row per (tenant, patient)
    forever, so a state nobody clears is not "until this conversation ends" —
    it is permanent. The `conversation-flow-state` skill states the invariant
    both of these answer: every non-IDLE state needs an exit that does not
    depend on the patient or the agent choosing it.

    What each state would cost without this:

      * AWAITING_NAME (both channels) parks the patient BEFORE the LGPD
        notice, on a question the product can live without. Same treatment
        as AWAITING_EMAIL: the wait is dropped and the caller asks whether to
        resume ("Sim" re-asks the name, "Não" pauses).
      * AWAITING_EMAIL parks the visitor BEFORE the LGPD notice. The floor
        clears the active wait and the caller asks whether they want to resume;
        it never silently skips the still-unclaimed address.
      * AWAITING_EMAIL_CODE parks the visitor AFTER the appointment is already
        committed. It has a second, immediate exit (any non-6-digit message
        leaves it — see the gate in `_persist_inbound_message`), so the floor
        here catches the narrower case of a patient who sends nothing at all
        and comes back tomorrow to a prompt about a code that expired an hour
        into the silence.

    No consent, appointment or history row is touched; only the transient
    column moves. The caller owns the visible "quer continuar?" prompt. The
    appointment made during the visit is NOT affected in any way — it was
    committed before AWAITING_EMAIL_CODE was ever entered.

    Returns True when the state was expired (the caller logs it).
    """
    if conversation.flow_state not in (
        FlowState.AWAITING_EMAIL,
        FlowState.AWAITING_EMAIL_CODE,
        FlowState.AWAITING_NAME,
    ):
        return False
    if last_activity_at is None:
        return False
    gap = datetime.now(UTC) - _as_utc(last_activity_at)
    if gap < timedelta(minutes=pending_identity_ttl_minutes(tenant)):
        return False
    conversation.flow_state = FlowState.IDLE
    return True

def _pending_identity_reactivation_offer(
    conversation: Conversation,
    tenant: Tenant,
    patient_ref: str,
    body: str | None,
    origin: FlowState,
    *,
    channel: str,
    pre_consent: bool = False,
) -> _ReplyContext:
    """Arm the existing Sim/Não gate for an expired e-mail, OTP or name wait.

    `channel` is required: the e-mail waits only exist on Brain-Message but
    the name wait lives on both, and a default would silently send a WhatsApp
    patient's offer down the wrong channel (`channel-aware-dispatch`). `pre_consent` picks the true
    sentence for a code wait: a code asked BEFORE consent (an e-mail that
    already had an account) has no consultation behind it to reassure about.
    """
    conversation.reactivation_origin = origin.value
    if origin == FlowState.AWAITING_EMAIL_CODE and not pre_consent:
        prefix = "Sua consulta continua marcada."
    else:
        prefix = "Seu atendimento ficou pausado antes da etapa de privacidade."
    prompt = reactivation_continue_prompt(tenant)
    return _ReplyContext(
        channel=channel,
        conversation_id=conversation.id,
        tenant_id=tenant.id,
        patient_ref=patient_ref,
        inbound_body=body or "",
        greeting_override=f"{prefix}\n\n{prompt}",
        greeting_buttons=reactivation_choice_buttons(tenant),
    )

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
        #
        # The pitch used to be read from `greeting_message`. That column is gone
        # (it held WHOLE greetings, and the first-contact message is a product
        # frame now), so the fallback follows the pitch to where it actually
        # lives: `clinic_description`, the clinic's own slot in that frame. This
        # is also strictly lighter - the slot is capped at ~180 chars, where the
        # old column could hold a full 1024-char greeting.
        returning = (tenant.clinic_description or "").strip()
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
            patient_ref=wa_id,
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
        patient_ref=wa_id,
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
    # The attendee IS cleared, unlike the two below: it belongs to one
    # booking, and a later chat booking must never inherit a third party's
    # name from a conversation that went quiet mid-flow (services/attendee.py).
    conversation.flow_attendee_name = None
    # The AI's parked draft belongs to the same abandoned booking (TASK-030 P2).
    conversation.flow_draft = None
    # A "Marcar outra" booking abandoned in LLM mode: the original stays
    # (TASK-032 R3) - the marker belongs to the booking that just expired.
    conversation.flow_replaces_appointment_id = None
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

def _expire_stale_attendee_step(
    conversation: Conversation,
    tenant: Tenant,
    last_activity_at: datetime | None,
) -> bool:
    """Drop a long-idle pra-quem / attendee-name / authorization step.

    Unlike the rest of SERVICE_CATALOG (which `_expire_stale_llm_state`
    deliberately leaves alone because it re-prompts on unexpected input), the
    NAME step accepts free text: a patient who walks away there and comes back
    days later with "Maria, bom dia" would otherwise have their greeting read
    as a third party's name. So the three steps get the same universal,
    config-free floor as full LLM mode (skill conversation-flow-state:
    every non-IDLE state needs a time-bounded exit not gated on tenant config).

    Silent, in place, `flow_*` only - the shape of the LLM floor above. Nothing
    of the booking is lost: these steps come BEFORE any other booking choice.
    """
    if conversation.flow_state != FlowState.SERVICE_CATALOG:
        return False
    # The three attendee steps, AND any later booking step that carries an
    # authorized attendee: a list left open for weeks must not book for a
    # third party on a stale tap. A self-booking mid-catalog keeps today's
    # behaviour (those steps re-prompt and self-correct).
    if conversation.flow_step not in ATTENDEE_STEPS and not conversation.flow_attendee_name:
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
    conversation.flow_attendee_name = None
    conversation.flow_draft = None
    conversation.flow_replaces_appointment_id = None
    return True

async def _write_flow_state(conversation_id: UUID | None, state: FlowState) -> None:
    """Move `flow_state` from OUTSIDE the inbound transaction. Best-effort.

    The inbound transaction has already committed by the time `_send_bot_reply`
    runs, so the two identity steps that only learn their outcome from brain-api
    (the first-contact probe, and spending a code) cannot use the in-place
    mutation the rest of `_persist_inbound_message` uses. They open their own
    short session instead — the same thing the fire-and-forget send paths do
    (see `_handle_calendar_unavailable`) and what `plugins/base.py` tells an
    `on_inbound` hook to do.

    Writes ONLY `flow_state`. The other `flow_*` columns are the booking flow's
    working memory and neither identity state has any business clearing them: a
    patient can be asked for a code while a service selection is still parked,
    and losing it would cost them the step they had already answered.

    A failure is logged and swallowed. The cost of not writing is bounded by
    `_expire_stale_pending_identity_state`, which drops whatever is there after
    the silence budget.
    """
    if conversation_id is None:
        return
    try:
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None:
                    return
                conversation.flow_state = state
    except Exception as exc:
        logger.warning(
            "pending_identity_flow_state_write_failed",
            error=str(exc),
            conversation_id=str(conversation_id),
            state=state.value,
        )
