"""Portal identity gate, moved out of the turn router (TASK-024)."""

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import (
    FlowState,
    Patient,
    Tenant,
)
from secretaria.services.booking_hold import (
    live_hold_in,
)
from secretaria.services.flow_router import (
    classify_yes_no,
    pending_identity_ttl_minutes,
)
from secretaria.services.pending_identity import (
    IDENTITY_BACK_ACTION,
    IDENTITY_CHANGE_EMAIL_ACTION,
    identity_action_or_none,
    parse_code,
    parse_email,
)
from secretaria.workers.shared.context import (
    _ReactivationDirective,
    _ReplyContext,
)
from secretaria.workers.shared.state_expiry import (
    _expire_stale_pending_identity_state,
    _pending_identity_reactivation_offer,
)

logger = get_logger(__name__)

# The gate's fall-through. A distinct object because the gate's `return`s may be None
# ("end this turn silently"), so None cannot also mean "carry on".
NO_DECISION = object()


async def run_identity_gate(
    *,
    session: AsyncSession,
    tenant: Tenant,
    patient: Patient,
    patient_ref: str,
    channel: str,
    body: str | None,
    interactive_reply_id: str | None,
    conversation,
    last_activity_at,
) -> object:
    """The Brain-Message inline identity gate: e-mail -> code -> "quer continuar?".

    Moved verbatim out of `turn_router._route_inbound_turn` (TASK-024). It sits ABOVE the LGPD
    gate, below human handover and the reminder action buttons. Returns `NO_DECISION` when this
    turn is not an identity turn (the router carries on with the LGPD gate); otherwise returns
    what the router must return - a `_ReplyContext`, or `None` to end the turn silently.
    """
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
        pre_consent = patient.lgpd_accepted_at is None
        # The shape of what was not recognised, never its text (TASK-042): enough to tell
        # a stray tap from a typo'd code from a sentence if the 2026-10-09 incident returns.
        logger.info(
            "conversation_pending_code_unrecognized",
            conversation_id=str(conversation.id),
            tenant_id=str(tenant.id),
            kind="interactive" if interactive_reply_id else ("text" if body else "empty"),
            length=len(body or ""),
            pre_consent=pre_consent,
        )
        held = await live_hold_in(session, conversation.id)
        if held is None and pre_consent:
            # The KNOWN-address wait (`_continue_after_email_claim`): asked before
            # consent, so there is no appointment and no hold behind it. Dropping it here
            # used to send a returning patient into the NEW-visitor LGPD notice, one stray
            # message away from their account. The card is repeated instead and the
            # wait's exit is the clock (`_expire_stale_pending_identity_state` above) or
            # the card's "Mudar e-mail". The post-booking OFFER below is unchanged.
            return _ReplyContext(
                channel=channel,
                conversation_id=conversation.id,
                tenant_id=tenant.id,
                patient_ref=patient_ref,
                inbound_body=body or "",
                existing_account_code_reprompt=True,
            )
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
    return NO_DECISION
