"""turn_router - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import (
    ConsentEvent,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.schemas.webhook import (
    inbound_routing_text,
)
from secretaria.services.booking_hold import (
    live_hold_in,
)
from secretaria.services.channel_sender import (
    CHANNEL_BRAIN_MESSAGE,
)
from secretaria.services.flow_router import (
    classify_yes_no,
    flows_enabled,
    llm_state_ttl_minutes,
    pending_identity_ttl_minutes,
    reactivation_prompt_enabled,
)
from secretaria.services.greeting_template import (
    CONSENT_ACCEPTED_MESSAGE,
    CONSENT_EVENT_KIND,
)
from secretaria.services.handover import HandoverManager
from secretaria.services.patient_context import (
    PatientOpeningState,
    resolve_patient_opening_state,
)
from secretaria.services.patient_name import (
    NAME_REASKED_STEP,
    parse_patient_name,
)
from secretaria.services.pending_identity import (
    IDENTITY_BACK_ACTION,
    IDENTITY_CHANGE_EMAIL_ACTION,
    identity_action_or_none,
    parse_code,
    parse_email,
)
from secretaria.services.reminder_opening import (
    OPEN,
    SKIP_SWITCH_OFF,
    decide_reminder_opening,
)
from secretaria.workers.shared.context import (
    _ReactivationDirective,
    _ReplyContext,
)
from secretaria.workers.shared.db import (
    _conversation_message_count,
    _get_or_create_conversation,
)
from secretaria.workers.shared.dispatch import (
    _GREETING_LLM_ESCAPE_SUFFIX,
)
from secretaria.workers.shared.greeting import (
    _adapt_greeting_to_state,
    _asks_name_at_first_contact,
    _first_contact_reply,
    _greeting_buttons_for,
    _load_upcoming_greeting_data,
    _menu_vocabulary,
    _select_greeting,
)
from secretaria.workers.shared.state_expiry import (
    _expire_stale_attendee_step,
    _expire_stale_edit_state,
    _expire_stale_llm_state,
    _expire_stale_pending_identity_state,
    _pending_identity_reactivation_offer,
    _reactivation_offer,
)
from secretaria.workers.shared.text import (
    _is_consent_acceptance,
    extract_patient_name,
    is_menu_command,
)

logger = get_logger(__name__)


async def _route_inbound_turn(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient: Patient,
    patient_ref: str,
    channel: str,
    is_returning_patient: bool,
    body: str | None,
    inbound_wam_id: str | None,
    action_button: tuple[str, str] | None = None,
    greeting_button: str | None = None,
    interactive_reply_id: str | None = None,
    attachment: dict | None = None,
) -> _ReplyContext | None:
    """Decide what the bot should answer, for an ALREADY-RESOLVED turn.

    The channel-neutral heart of an inbound message. Everything WhatsApp-shaped
    happens before this is called - resolving a `phone_number_id` to a tenant, a
    `wa_id` to a patient, a `wam_id` to a dedupe claim, and the wa_id allowlist -
    and by the time control arrives here the turn is just: this tenant, this
    patient, this text, on this channel. `POST /internal/brain-message/inbound`
    resolves the same four things its own way and calls this same function.

    THE ORDER OF THE BRANCHES BELOW IS THE PRODUCT. It was extracted verbatim
    from `_persist_inbound_message`, not rewritten, and every comment explaining
    why a gate sits where it does came with it. In sequence: inbound recorded ->
    reminder action button (above handover, because honouring an existing
    booking is not a conversational turn) -> human handover -> a FILE (the fixed
    receipt, Brain-Message only) -> LGPD consent gate
    -> `/menu` -> greeting-button degrade -> pending "quer continuar?" answer ->
    greeting selection -> LLM-state expiry (BEFORE the reactivation offer, so a
    patient who never answers it still leaves LLM mode) -> reactivation offer ->
    normal dispatch. Moving any one of them changes which patients get answered
    and how; see the `conversation-flow-state` skill.

    Runs INSIDE the caller's transaction and inside the caller's session: it
    adds the inbound `Message` and mutates `conversation`/`patient`, and the
    caller commits. It never opens a session of its own.

    Args:
        patient_ref: how to address this patient back on `channel` - their
            `wa_id` on WhatsApp, their `external_id` on Brain-Message. Rides
            out on `_ReplyContext.patient_ref` and is what the channel's
            sender is handed as `to`.
        channel: `CHANNEL_WHATSAPP` or `CHANNEL_BRAIN_MESSAGE`; stamped on
            every `_ReplyContext` built here so `_reply_sender` can pick the
            delivery mechanism without re-reading the patient row.
        inbound_wam_id: the Meta message id, stored on the inbound `Message`.
            None on Brain-Message, where no Meta id exists.
        body: the message as the patient saw it - what they typed, or the
            TITLE of the control they tapped. The only text the inbound
            `Message` stores, because it is what the staff console and the
            patient portal render.
        interactive_reply_id: the raw id of the control tapped
            (schemas/webhook.py::extract_inbound_reply_id on WhatsApp; on
            Brain-Message the portal's own tap, already checked against the
            options this conversation offered by
            `_validated_brain_message_reply_id`), stored beside `body`; None
            for anything typed or transcribed.
        attachment: the stored file record (`messages.attachment`,
            core/attachments.py::StoredAttachment) when a Brain-Message patient
            sent a file - already in the bucket, put there by the API. The row
            keeps it; the answer is the fixed receipt (branch after handover).
    """
    # Everything below DECIDES on the routing text, not on what is stored: for
    # a tap on a data-carrying row it is the title with the row's payload
    # re-attached - "Dra. Ana (<uuid>)", "15:00 (<iso>)" - the string every
    # matcher in services/flow_router.py was written against, and identical to
    # `body` for anything that is not such a tap. Rebinding the name keeps the
    # ladder below exactly what it was before the two were split; the ONLY
    # reader of `stored_body` is the inbound `Message` row.
    stored_body = body
    body = inbound_routing_text(stored_body, interactive_reply_id)

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
    prior_messages = await _conversation_message_count(session, conversation.id)
    is_first_contact = prior_messages == 0

    # Timestamp of the last activity BEFORE this inbound, used to
    # measure the silence gap for the returning-patient offer.
    last_activity_at = await session.scalar(
        select(func.max(Message.created_at)).where(
            Message.conversation_id == conversation.id
        )
    )

    # An OTP is an authentication secret, not conversation content. Keep the
    # real value only in this turn's in-memory routing variable; the staff
    # console / patient transcript receives a fixed redaction. This check is
    # deliberately state- and channel-bound so an ordinary six-digit message
    # elsewhere keeps its original meaning and display.
    persisted_body = stored_body
    if (
        channel == CHANNEL_BRAIN_MESSAGE
        and conversation.flow_state == FlowState.AWAITING_EMAIL_CODE
        and parse_code(body) is not None
    ):
        persisted_body = "[código oculto]"

    session.add(
        Message(
            conversation_id=conversation.id,
            direction=MessageDirection.INBOUND,
            sender=MessageSender.PATIENT,
            wam_id=inbound_wam_id,
            # The title alone, never the payload - see `stored_body` above.
            body=persisted_body,
            interactive_reply_id=interactive_reply_id,
            attachment=attachment,
            # Brain-Message: reaching this row IS delivery to the clinic - same
            # INSERT, same now() as `created_at` (services/message_status.py).
            # WhatsApp inbound keeps NULL: its receipt is the patient's, not ours.
            delivered_at=func.now() if channel == CHANNEL_BRAIN_MESSAGE else None,
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
            channel=channel,
            conversation_id=conversation.id,
            patient_ref=patient_ref,
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

    # A FILE (Brain-Message only): transport, not analysis. The row above already
    # carries it; the bot's whole answer is a fixed receipt, and nothing below -
    # identity steps, the LGPD gate, /menu, the flow router, the LLM - sees the turn.
    # After the handover check (a human who took over answers the file themselves)
    # and BEFORE the identity gate, whose AWAITING_EMAIL_CODE branch would read the
    # placeholder as a wrong code and drop the state. `flow_state` is left as it was,
    # so the patient's next typed message continues whatever the file interrupted.
    # Consent was checked by the API before the bytes were stored.
    if attachment is not None:
        return _ReplyContext(
            channel=channel,
            conversation_id=conversation.id,
            tenant_id=tenant.id,
            patient_ref=patient_ref,
            inbound_body=body or "",
            attachment_received=True,
        )

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
    # --- The name question (BOTH channels) ---------------------
    # ABOVE the Brain-Message identity gate and the LGPD gate: the
    # owner's order is greeting -> [e-mail ->] name -> LGPD, so a
    # patient being asked their name must never be handed the consent
    # re-prompt instead. Below handover and the action buttons for the
    # same reasons the LGPD gate is.
    #
    # Channel-neutral on purpose. The state is only ever ENTERED by a
    # channel-aware decision (`_asks_name_at_first_contact` on WhatsApp,
    # the "e-mail is new" branch of the claim on Brain-Message), and
    # reading the answer is identical on both. Nothing in this block
    # knows about e-mail — WhatsApp never gets an e-mail step.
    #
    # The answer is written to `Patient.name` and ONLY there: that is
    # the field `services/pii_pseudonymization.py::load_pseudonymizer`
    # registers as PACIENTE before any LLM turn (services/patient_name.py).
    if conversation.reactivation_origin == FlowState.AWAITING_NAME.value:
        # The Sim/Não the silence floor below offered. Same shape as the
        # e-mail step's own gate in the Brain-Message block.
        answer = classify_yes_no(body, tenant)
        if answer in ("yes", "no"):
            conversation.reactivation_origin = None
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                patient_ref=patient_ref,
                inbound_body=body or "",
                reactivation=_ReactivationDirective(
                    kind="resume" if answer == "yes" else "reset",
                    origin=FlowState.AWAITING_NAME.value,
                ),
            )
        # Anything else consumes the offer — never re-arms it. The natural
        # reply to "quer continuar?" after a name question is the NAME, so a
        # parseable one is taken and the opening moves on; anything else falls
        # through to the consent gate below. The name is a courtesy: the offer
        # must not become a loop whose only exit is a button (the invariant in
        # the `conversation-flow-state` skill).
        conversation.reactivation_origin = None
        late_name = parse_patient_name(body, not_names=_menu_vocabulary(tenant))
        if late_name is not None:
            patient.name = late_name
            logger.info(
                "worker_patient_name_answered",
                patient_id=str(patient.id),
                channel=channel,
            )
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                patient_ref=patient_ref,
                inbound_body=body or "",
                name_captured=True,
            )

    if conversation.flow_state == FlowState.AWAITING_NAME:
        # The time-based floor first, as everywhere else: a patient who went
        # silent on the question an hour ago is not answering it now.
        if _expire_stale_pending_identity_state(conversation, tenant, last_activity_at):
            conversation.flow_step = None
            logger.info(
                "conversation_name_request_expired",
                conversation_id=str(conversation.id),
                tenant_id=str(tenant.id),
                channel=channel,
                ttl_minutes=pending_identity_ttl_minutes(tenant),
            )
            return _pending_identity_reactivation_offer(
                conversation,
                tenant,
                patient_ref,
                body,
                FlowState.AWAITING_NAME,
                channel=channel,
                pre_consent=patient.lgpd_accepted_at is None,
            )
        # A direct answer first; a self-introduction inside a longer sentence
        # ("meu nome é Ana, tudo bem?") second, through the same extractor the
        # opportunistic capture above uses.
        name = parse_patient_name(body, not_names=_menu_vocabulary(tenant)) or extract_patient_name(
            body
        )
        if name is None and conversation.flow_step != NAME_REASKED_STEP:
            conversation.flow_step = NAME_REASKED_STEP
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                patient_ref=patient_ref,
                inbound_body=body or "",
                name_invalid=True,
            )
        # Leaving the state, with or without a name. A SECOND unreadable
        # answer moves on rather than asking a third time: the name is a
        # courtesy, consent is the obligation, and a state whose only exit
        # is the patient producing a string we like is the stuck shape the
        # `conversation-flow-state` skill forbids.
        conversation.flow_state = FlowState.IDLE
        conversation.flow_step = None
        if name is not None:
            # Overwrites on purpose: on WhatsApp this replaces the Meta
            # profile name the row was created with (owner, 2026-09-20: the
            # profile name is not trusted; the typed answer is).
            patient.name = name
            logger.info(
                "worker_patient_name_answered",
                patient_id=str(patient.id),
                channel=channel,
            )
        else:
            logger.info(
                "worker_patient_name_skipped",
                patient_id=str(patient.id),
                channel=channel,
            )
        return _ReplyContext(
            channel=channel,
            conversation_id=conversation.id,
            tenant_id=tenant.id,
            patient_ref=patient_ref,
            inbound_body=body or "",
            name_captured=True,
        )

    # --- Brain-Message inline identity gate --------------------
    # ABOVE the LGPD gate, which is the entire point: the owner
    # fixed the order as greeting -> e-mail -> LGPD, so a visitor
    # who still owes an address must never be handed the consent
    # notice first. Below handover and the action buttons for the
    # same reasons the LGPD gate is (see its comment) — a human who
    # picked up the conversation is never pre-empted by an
    # identity step.
    #
    # WhatsApp CANNOT reach any of this: the whole block sits
    # inside a channel check, and on WhatsApp the phone number is
    # already the identity. That is the non-regression this
    # feature turns on, so it is expressed as ONE guard at the top
    # rather than as a condition repeated on each branch.
    if channel == CHANNEL_BRAIN_MESSAGE:
        # The time-based exit below reuses the product's existing
        # "quer continuar?" gate. Its answer must be consumed here, before the
        # LGPD gate: AWAITING_EMAIL lives before consent, while
        # AWAITING_EMAIL_CODE lives after booking. The generic reactivation
        # branch further down cannot know that ordering.
        if conversation.reactivation_origin in (
            FlowState.AWAITING_EMAIL.value,
            FlowState.AWAITING_EMAIL_CODE.value,
        ):
            origin = conversation.reactivation_origin
            answer = classify_yes_no(body, tenant)
            if answer in ("yes", "no"):
                conversation.reactivation_origin = None
                return _ReplyContext(
                    channel=channel,
                    conversation_id=conversation.id,
                    tenant_id=tenant.id,
                    patient_ref=patient_ref,
                    inbound_body=body or "",
                    reactivation=_ReactivationDirective(
                        kind="resume" if answer == "yes" else "reset",
                        origin=origin,
                    ),
                )
            # Keep the bounded gate armed and repeat the two explicit choices;
            # an unrelated sentence must not be mistaken for an e-mail or OTP.
            return _pending_identity_reactivation_offer(
                conversation,
                tenant,
                patient_ref,
                body,
                FlowState(origin),
                channel=channel,
                pre_consent=patient.lgpd_accepted_at is None,
            )

        # The time-based floor runs FIRST, before the state is
        # read, exactly as `_expire_stale_llm_state` runs before
        # the state is used further down. A visitor who abandoned
        # the e-mail question an hour ago is not still answering it.
        pending_origin = conversation.flow_state
        if _expire_stale_pending_identity_state(conversation, tenant, last_activity_at):
            logger.info(
                "conversation_pending_identity_state_expired",
                conversation_id=str(conversation.id),
                tenant_id=str(tenant.id),
                ttl_minutes=pending_identity_ttl_minutes(tenant),
            )
            return _pending_identity_reactivation_offer(
                conversation,
                tenant,
                patient_ref,
                body,
                pending_origin,
                channel=channel,
                pre_consent=patient.lgpd_accepted_at is None,
            )

        if conversation.flow_state == FlowState.AWAITING_EMAIL_CODE:
            # A TAP on the code notice's own card, checked BEFORE the six
            # digits because the two cannot collide and the tap is the more
            # specific signal: `identity_action_or_none` only ever matches an
            # id this module minted, and `interactive_reply_id` arrived here
            # already revalidated against the cards this conversation offered
            # (`_validated_brain_message_reply_id`), so a forged id is None by
            # the time it gets here. The LABEL is never consulted — a tap on
            # "⬅️ Voltar" routes on `identity_back`, not on the arrow.
            action = identity_action_or_none(interactive_reply_id)
            if action is not None:
                # The state moves HERE, inside the inbound transaction, for
                # the two actions whose destination does not depend on
                # brain-api. `identity_resend` is the exception: it stays in
                # AWAITING_EMAIL_CODE and `_send_bot_reply` rewrites it only
                # once a new challenge is confirmed, exactly as the
                # reactivation branch does.
                if action == IDENTITY_CHANGE_EMAIL_ACTION:
                    conversation.flow_state = FlowState.AWAITING_EMAIL
                elif action == IDENTITY_BACK_ACTION:
                    conversation.flow_state = FlowState.IDLE
                logger.info(
                    "pending_identity_card_tapped",
                    action=action,
                    conversation_id=str(conversation.id),
                    tenant_id=str(tenant.id),
                )
                return _ReplyContext(
                    channel=channel,
                    conversation_id=conversation.id,
                    tenant_id=tenant.id,
                    patient_ref=patient_ref,
                    inbound_body=body or "",
                    identity_action=action,
                )
            code = parse_code(body)
            if code is not None:
                return _ReplyContext(
                    channel=channel,
                    conversation_id=conversation.id,
                    tenant_id=tenant.id,
                    patient_ref=patient_ref,
                    inbound_body=body or "",
                    pending_code=code,
                )
            # Anything that is not six digits. What happens next depends on
            # whether this wait is a GATE or an OFFER, and the thing that
            # tells them apart is a live hold.
            #
            #   hold  -> the appointment does NOT exist yet and the slot is
            #            reserved (`services/booking_hold.py`). Dropping the
            #            state here would silently cost the patient the very
            #            window they just chose, so the card is repeated with
            #            the reservation spelled out. Not a trap: the card's
            #            back button leaves in one tap, and the silence floor
            #            above still expires the state on the clock.
            #   none  -> the pre-2026-09-20 behaviour, unchanged: the account
            #            is an offer, the appointment is already committed, and
            #            a patient with a different question must not have to
            #            answer this one first.
            held = await live_hold_in(session, conversation.id)
            if held is not None:
                logger.info(
                    "conversation_pending_code_reprompted",
                    conversation_id=str(conversation.id),
                    tenant_id=str(tenant.id),
                )
                return _ReplyContext(
                    channel=channel,
                    conversation_id=conversation.id,
                    tenant_id=tenant.id,
                    patient_ref=patient_ref,
                    inbound_body=body or "",
                    pending_code_reprompt=held,
                )
            conversation.flow_state = FlowState.IDLE
            # TASK-032 R3: leaving the code wait ends a "Marcar outra" booking too.
            conversation.flow_replaces_appointment_id = None
            conversation.flow_edit_draft = None
            logger.info(
                "conversation_pending_code_abandoned",
                conversation_id=str(conversation.id),
                tenant_id=str(tenant.id),
            )

        elif conversation.flow_state == FlowState.AWAITING_EMAIL:
            email = parse_email(body)
            if email is None:
                # No "skip" affordance, deliberately: the owner's
                # words fix the e-mail as step 1 of the flow and
                # say nothing about opting out, and the prompt that
                # ordered this work says to treat an unsignalled
                # escape as blocking rather than invent one. The
                # exit that DOES exist is the silence floor above:
                # it drops the active state after
                # `pending_identity_ttl_minutes` and asks whether to
                # resume. A later consent turn probes brain-api again,
                # so the pause is bounded without becoming a bypass.
                return _ReplyContext(
                    channel=channel,
                    conversation_id=conversation.id,
                    tenant_id=tenant.id,
                    patient_ref=patient_ref,
                    inbound_body=body or "",
                    pending_email_invalid=True,
                )
            # Keep the state until brain-api ACKs the claim. The wire call is
            # made after this transaction commits; `_send_bot_reply` clears it
            # only on CLAIMED. This makes the owner's ordering enforceable:
            # e-mail claimed -> LGPD, never best-effort claim -> LGPD.
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                patient_ref=patient_ref,
                inbound_body=body or "",
                pending_email_claim=email,
            )

    # --- LGPD consent gate -------------------------------------
    # Sits ABOVE `/menu`, the greeting and normal dispatch, and
    # BELOW human handover and reminder action buttons. That
    # position is the whole design:
    #
    #   * below handover, so a human secretary who picks up the
    #     conversation is never blocked by a bot-owned gate;
    #   * below action buttons, so a patient can still cancel or
    #     confirm an EXISTING appointment (honouring a booking they
    #     already made is not new processing to consent to);
    #   * above everything else, so nothing serves a patient whose
    #     legal basis has not been established.
    #
    # Mirrors PreCheck, which parks the session in `LGPD_PENDING`
    # and answers anything but an acceptance with "Reenviar LGPD"
    # (see the `wf_condutor_generico_universal` n8n workflow).
    #
    # It is a gate on a FACT about the subject (`lgpd_accepted_at`),
    # NOT a `FlowState`. That distinction matters: a non-IDLE flow
    # state whose only exit is the patient tapping a button is the
    # shape that permanently parked conversations before (see
    # `_expire_stale_llm_state`). A fact carries no such risk —
    # `flow_state` is untouched here, so whatever the conversation
    # was doing resumes intact the moment consent lands.
    if _is_consent_acceptance(body):
        # Idempotent, like the `/menu` branch below: a second
        # delivery (or a tap on the old button further up the
        # thread) re-sends the same menu and writes nothing new.
        if patient.lgpd_accepted_at is None:
            patient.lgpd_accepted_at = datetime.now(UTC)
            # The Patient column is the operational flag; this row
            # is the immutable audit record. Different lifetimes on
            # purpose - see models/patient.py.
            session.add(
                ConsentEvent(
                    tenant_id=tenant.id,
                    wa_id=patient_ref,
                    kind=CONSENT_EVENT_KIND,
                    legal_basis=(
                        "consentimento (art. 7º, I) — aceite explícito dos "
                        "Termos de Uso e Política de Privacidade "
                        + (
                            "no Portal Brain-Message"
                            if channel == CHANNEL_BRAIN_MESSAGE
                            else "no WhatsApp"
                        )
                    ),
                )
            )
            logger.info(
                "conversation_terms_accepted",
                conversation_id=str(conversation.id),
                tenant_id=str(tenant.id),
            )
        # The FIRST message of the conversation to carry buttons.
        return _ReplyContext(
            channel=channel,
            conversation_id=conversation.id,
            tenant_id=tenant.id,
            patient_ref=patient_ref,
            inbound_body=body or "",
            greeting_override=CONSENT_ACCEPTED_MESSAGE,
            greeting_buttons=_greeting_buttons_for(tenant, CONSENT_ACCEPTED_MESSAGE),
        )

    if patient.lgpd_accepted_at is None:
        if is_first_contact:
            # The state is NOT moved here: `_send_bot_reply` writes
            # AWAITING_NAME only once it is about to ask (past the entitlement
            # gate), exactly as the Portal's e-mail step does. A turn that
            # sends nothing must not leave the next message read as a name.
            ask_name = _asks_name_at_first_contact(channel, patient, is_returning_patient)
            return _first_contact_reply(
                tenant=tenant,
                conversation_id=conversation.id,
                patient_ref=patient_ref,
                channel=channel,
                inbound_body=body or "",
                ask_name=ask_name,
            )
        # Already asked, still not accepted: re-prompt, with the
        # button attached so the way forward is one tap from the
        # newest message rather than a scroll back up the thread.
        logger.info(
            "conversation_consent_pending",
            conversation_id=str(conversation.id),
            tenant_id=str(tenant.id),
        )
        return _ReplyContext(
            channel=channel,
            conversation_id=conversation.id,
            tenant_id=tenant.id,
            patient_ref=patient_ref,
            inbound_body=body or "",
            send_consent_reminder=True,
            # A timed-out e-mail reactivation can leave this local row before
            # consent while brain-api still owns the authoritative visit
            # state. Re-probe on Brain-Message so "Não" pauses the flow without
            # ever becoming a hidden way to skip the required e-mail step.
            probe_pending_identity=(channel == CHANNEL_BRAIN_MESSAGE),
        )

    if is_menu_command(body):
        conversation.reactivation_origin = None
        logger.info(
            "conversation_menu_requested",
            conversation_id=str(conversation.id),
            tenant_id=str(tenant.id),
            source="command",
        )
        return _ReplyContext(
            channel=channel,
            conversation_id=conversation.id,
            tenant_id=tenant.id,
            patient_ref=patient_ref,
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
            channel=channel,
            conversation_id=conversation.id,
            patient_ref=patient_ref,
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
            conversation.flow_attendee_name = None
            conversation.flow_draft = None
            conversation.flow_replaces_appointment_id = None
            conversation.flow_edit_draft = None
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                patient_ref=patient_ref,
                inbound_body=body or "",
                reactivation=_ReactivationDirective(kind="reset", origin=origin),
            )
        if answer == "yes":
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                patient_ref=patient_ref,
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
    if _expire_stale_attendee_step(conversation, tenant, last_activity_at):
        logger.info(
            "conversation_attendee_step_expired",
            conversation_id=str(conversation.id),
            tenant_id=str(tenant.id),
            ttl_minutes=llm_state_ttl_minutes(tenant),
        )
    if _expire_stale_edit_state(conversation, tenant, last_activity_at):
        logger.info(
            "conversation_edit_state_expired",
            conversation_id=str(conversation.id),
            tenant_id=str(tenant.id),
            ttl_minutes=llm_state_ttl_minutes(tenant),
        )

    # --- TASK-032 R3: the appointment reminder as the chat's first message ----
    # After the consent gate, /menu, the pending "quer continuar?" answer and
    # the two silence floors above (so a stale LLM/attendee state is already
    # dropped), and INSTEAD of the returning-patient offer below: a patient with
    # a live future appointment who writes after the reactivation gap sees that
    # appointment's reminder card first (spec §4.3). The patient's message is
    # still answered right after it (`_send_bot_reply_inner`). Returns without a
    # query when the clinic's switch is off.
    if greeting_override is None:
        opening = await decide_reminder_opening(
            session,
            tenant=tenant,
            patient_id=patient.id,
            last_activity_at=last_activity_at,
            now=datetime.now(UTC),
        )
        if opening.reason == OPEN and opening.appointment_id is not None:
            logger.info(
                "reminder_opening_chosen",
                conversation_id=str(conversation.id),
                tenant_id=str(tenant.id),
                appointment_id=str(opening.appointment_id),
            )
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                patient_ref=patient_ref,
                inbound_body=body or "",
                reminder_opening_appointment_id=opening.appointment_id,
            )
        if opening.reason != SKIP_SWITCH_OFF:
            logger.info(
                "reminder_opening_skipped",
                reason=opening.reason,
                conversation_id=str(conversation.id),
                tenant_id=str(tenant.id),
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
            patient_ref,
            body,
            last_activity_at,
            resumable_origin,
        )
        if offer is not None:
            return offer

    greeting_buttons = _greeting_buttons_for(tenant, greeting_override, opening_context)

    # No `send_consent_notice` here: the gate above returns for
    # every subject with a NULL `lgpd_accepted_at`, so anything
    # reaching this far has already accepted. The greeting keeps
    # its buttons on this path, which is why the interactive-body
    # budget (services/greeting_template.py) still governs it.
    return _ReplyContext(
        channel=channel,
        conversation_id=conversation.id,
        patient_ref=patient_ref,
        inbound_body=body or "",
        greeting_override=greeting_override,
        greeting_buttons=greeting_buttons,
    )
