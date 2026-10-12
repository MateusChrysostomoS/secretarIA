"""identity - split out of workers/tasks.py (TASK-023)."""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    ConsentEvent,
    Conversation,
    FlowState,
    Patient,
    Tenant,
)
from secretaria.services.patient_name import (
    NAME_REQUEST_AFTER_EMAIL_MESSAGE,
    NAME_REQUEST_MESSAGE,
)
from secretaria.services.pending_identity import (
    ACCOUNT_CODE_GIVE_UP_MESSAGE,
    CODE_ACCEPTED_MESSAGE,
    CODE_GIVE_UP_MESSAGE,
    CODE_NOTICE_BUTTONS,
    EMAIL_REQUEST_MESSAGE,
    EXISTING_ACCOUNT_CODE_BUTTONS,
    IDENTITY_BACK_ACTION,
    IDENTITY_CHANGE_EMAIL_ACTION,
    IDENTITY_RESEND_ACTION,
    ClaimResult,
    IdentityState,
    RequestCodeOutcome,
    RequestCodeResult,
    cancel_pending_code,
    code_notice_body,
    existing_account_code_body,
    probe_identity,
    request_code,
)
from secretaria.workers.shared.context import (
    _ReplyContext,
)
from secretaria.workers.shared.dispatch import (
    _send_buttons_reply,
    _send_consent_notice,
    _send_name_question,
    _send_plain_reply,
)
from secretaria.workers.shared.opening import (
    _send_context_opening,
)
from secretaria.workers.shared.sentinels import (
    _handle_show_main_menu,
)
from secretaria.workers.shared.state_expiry import (
    _write_flow_state,
)

logger = get_logger(__name__)


async def _send_code_notice(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    waba_token: str | None,
    email_masked: str | None,
    event: str,
    pre_consent: bool = False,
) -> None:
    """The code notice, as the button card, naming the masked inbox.

    `pre_consent` picks the KNOWN-ADDRESS wording
    (`existing_account_code_body`): a code asked before consent, for an
    e-mail that already had an account, with no appointment to mention. It
    also picks the two-button card (`EXISTING_ACCOUNT_CODE_BUTTONS`, owner,
    2026-09-25): "Voltar" on this card only led forward to the LGPD notice,
    which reads as the button doing nothing — the three-button card
    (`CODE_NOTICE_BUTTONS`) is for the post-consent/post-booking notice,
    where "Voltar" has a real destination (the menu).

    One spelling for the two places `_send_bot_reply` emits it (a resumed wait
    and a resend); `plugins/pending_identity.py` emits the third, the one that
    follows a booking, through `BrainMessageSender` directly because it has no
    `_ReplyContext` to hand. All three build the body and the buttons from
    `services/pending_identity.py`, so the card cannot drift between them.
    """
    await _send_buttons_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=(
            existing_account_code_body(email_masked)
            if pre_consent
            else code_notice_body(email_masked)
        ),
        buttons=list(EXISTING_ACCOUNT_CODE_BUTTONS if pre_consent else CODE_NOTICE_BUTTONS),
        event=event,
    )

async def _continue_after_email_claim(
    reply: _ReplyContext,
    claim: ClaimResult,
    *,
    tenant: Tenant,
    waba_token: str | None,
) -> None:
    """Pick the question that follows an acknowledged e-mail, before consent.

    The e-mail is what tells a new visitor from a known one (owner,
    2026-09-20), so this is the Portal half of the name step's channel
    decision — the WhatsApp half is `_asks_name_at_first_contact`.

      * `account_exists` -> NO name question: the code, straight away, on the
        existing three-button card, naming the inbox with the mask brain-api
        returned on the claim (falling back to the one the code request
        returns — brain-api guarantees they are the same string). The wait
        reuses AWAITING_EMAIL_CODE and `verify_code` as they are; what differs
        is only what a success and a dead end lead to, and that is decided by
        consent (`_finish_account_code_before_consent`), not by a new state.
        If the code could not be mailed there is nothing to wait for: the
        conversation carries on to the LGPD notice, still without asking a
        known person their name.
      * a new address -> AWAITING_NAME and the name question; the LGPD notice
        follows the answer (`name_captured`).
    """
    if claim.account_exists:
        result = RequestCodeResult(RequestCodeOutcome.UNAVAILABLE)
        if reply.tenant_id is not None:
            result = await request_code(reply.tenant_id, reply.patient_ref)
        if result.outcome is RequestCodeOutcome.SENT:
            await _write_flow_state(reply.conversation_id, FlowState.AWAITING_EMAIL_CODE)
            await _send_code_notice(
                reply,
                tenant=tenant,
                waba_token=waba_token,
                email_masked=claim.email_masked or result.email_masked,
                event="existing_account_code_requested",
                pre_consent=True,
            )
            return
        logger.warning(
            "existing_account_code_unavailable",
            outcome=result.outcome.value,
            conversation_id=str(reply.conversation_id),
            tenant_id=str(reply.tenant_id),
        )
        await _write_flow_state(reply.conversation_id, FlowState.IDLE)
        await _send_consent_notice(reply, tenant=tenant, waba_token=waba_token)
        return

    await _write_flow_state(reply.conversation_id, FlowState.AWAITING_NAME)
    # With "📩 Mudar e-mail": the address was typed a message ago and may be wrong.
    await _send_name_question(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=NAME_REQUEST_AFTER_EMAIL_MESSAGE,
        event="patient_name_requested",
        offer_email_change=True,
    )

async def _account_code_dead_end(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    waba_token: str | None,
    event: str,
) -> None:
    """A known-address code that will not happen: carry on to consent.

    The pre-consent twin of `CODE_GIVE_UP_MESSAGE`, which would promise a
    consultation that does not exist. Leaves the wait and sends the LGPD
    notice, so the visitor continues exactly as a new one would.
    """
    await _write_flow_state(reply.conversation_id, FlowState.IDLE)
    await _send_plain_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=ACCOUNT_CODE_GIVE_UP_MESSAGE,
        event=event,
    )
    await _send_consent_notice(reply, tenant=tenant, waba_token=waba_token)

async def _finish_account_code_before_consent(
    reply: _ReplyContext,
    *,
    verified: bool,
    tenant: Tenant | None,
    waba_token: str | None,
    professionals: list | None,
    patient_wa: str | None,
    redis,
    account_name: str | None = None,
) -> None:
    """Close the known-address code wait (anything but a wrong code).

    Verified: the visit is now a verified account, so the same path a verified
    visitor takes on first contact applies — `_handle_pre_consent_identity`
    re-probes brain-api, mirrors the account's consent locally
    (`account_terms_verified`) and opens the menu. If that cannot be recorded
    the local LGPD notice goes out instead: a verified remote account never
    justifies skipping a local gate we failed to persist.

    `account_name` is the name brain-api returned with the verification (the
    account gave it at another clinic). Written first, so the identity branch
    finds it and does not ask a known person their name.
    """
    if tenant is None:
        return
    if not verified:
        await _account_code_dead_end(
            reply, tenant=tenant, waba_token=waba_token, event="existing_account_code_failed"
        )
        return
    if account_name:
        await _adopt_account_name(reply.conversation_id, account_name)
    await _write_flow_state(reply.conversation_id, FlowState.IDLE)
    await _send_plain_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=CODE_ACCEPTED_MESSAGE,
        event="existing_account_code_verified",
    )
    if await _handle_pre_consent_identity(
        reply,
        tenant=tenant,
        professionals=professionals,
        patient_wa=patient_wa,
        redis=redis,
        waba_token=waba_token,
    ):
        return
    await _send_consent_notice(reply, tenant=tenant, waba_token=waba_token)

async def _handle_identity_card_action(
    reply: _ReplyContext,
    *,
    tenant: Tenant | None,
    waba_token: str | None,
    professionals: list | None,
    patient_wa: str | None,
    redis,
    pre_consent: bool = False,
) -> None:
    """Own the whole turn for a tap on the code notice's card.

    `pre_consent`: the card was the KNOWN-ADDRESS one (asked before consent),
    which does not offer "Voltar" at all (owner, 2026-09-25 —
    `services/pending_identity.py::EXISTING_ACCOUNT_CODE_BUTTONS`); a resend
    re-sends that same wording.

    Three exits from `AWAITING_EMAIL_CODE` that do not require the patient to
    have the code in front of them — which is the point of the card. The flow
    state for two of them was already written inside the inbound transaction
    (`_route_inbound_turn`); only `identity_resend` waits on brain-api and
    therefore writes its state here.

    Fails the same way the rest of this leg does: a dead end says the
    appointment stands (`CODE_GIVE_UP_MESSAGE`) and leaves the conversation
    usable, never parked in a state whose only exit is a code that was never
    mailed.
    """
    if tenant is None:
        logger.warning(
            "pending_identity_card_action_without_tenant",
            conversation_id=str(reply.conversation_id),
        )
        return

    if reply.identity_action == IDENTITY_BACK_ACTION:
        # Out to the menu. `_handle_show_main_menu` re-writes the flow state
        # through `_apply_flow_result` (to MENU), which supersedes the IDLE
        # written upstream; that IDLE is what remains if this send fails, and
        # IDLE is the right resting place either way. The account offer is not
        # withdrawn: the code is still valid, and the existing reactivation
        # machinery can still bring the patient back to it.
        await _handle_show_main_menu(
            reply,
            tenant,
            professionals,
            patient_wa,
            redis=redis,
            waba_token=waba_token,
            source="identity_card",
        )
        return

    if reply.identity_action == IDENTITY_CHANGE_EMAIL_ACTION:
        # Back to the address question. The state is already AWAITING_EMAIL,
        # so the next syntactically valid address is claimed exactly as a
        # first one would be — brain-api overwrites the claim on the same
        # visit (`services/pending_identity.py::claim_email`), which is what
        # makes "I typed it wrong" recoverable without a new visit.
        #
        # The OLD address's code wait is cancelled first (best-effort, never
        # blocks the message below): otherwise the Portal composer keeps
        # reading brain-api's `/pending/status` as `otp_sent` and locks the
        # field to 6 digits while this very message is asking for an e-mail
        # (owner, 2026-09-25, reported live).
        if reply.tenant_id is not None:
            await cancel_pending_code(reply.tenant_id, reply.patient_ref)
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=EMAIL_REQUEST_MESSAGE,
            event="pending_email_change_requested",
        )
        return

    if reply.identity_action != IDENTITY_RESEND_ACTION:
        # A fourth button added to the card without a branch here. Say so
        # rather than silently treating it as a resend, which would mail the
        # patient a code they did not ask for.
        logger.warning(
            "pending_identity_card_action_unhandled",
            action=reply.identity_action,
            conversation_id=str(reply.conversation_id),
        )
        return

    # identity_resend: a NEW challenge, then the same card again.
    result = RequestCodeResult(RequestCodeOutcome.UNAVAILABLE)
    if reply.tenant_id is not None:
        result = await request_code(reply.tenant_id, reply.patient_ref)
    if result.outcome is RequestCodeOutcome.SENT:
        # Re-asserted rather than assumed: the state is already
        # AWAITING_EMAIL_CODE on the happy path, and writing it again costs
        # one short transaction while covering the case where something else
        # moved it between the tap and here.
        await _write_flow_state(reply.conversation_id, FlowState.AWAITING_EMAIL_CODE)
        await _send_code_notice(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            email_masked=result.email_masked,
            event="pending_code_resent",
            pre_consent=pre_consent,
        )
        return
    if pre_consent:
        await _account_code_dead_end(
            reply, tenant=tenant, waba_token=waba_token, event="pending_code_resend_unavailable"
        )
        return
    # NOT_PENDING / NO_EMAIL / UNAVAILABLE. Leave the wait: the patient just
    # asked for a code that is not coming, so keeping them in a state that
    # only reads six digits would strand them until the silence floor.
    await _write_flow_state(reply.conversation_id, FlowState.IDLE)
    await _send_plain_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=CODE_GIVE_UP_MESSAGE,
        event="pending_code_resend_unavailable",
    )

async def _record_verified_account_consent(conversation_id: UUID | None) -> bool:
    """Mirror brain-api's verified-account fact into the local LGPD gate.

    `IdentityState.VERIFIED` is authoritative: brain-api already completed the
    account consent and e-mail proof. This local stamp prevents a new clinic's
    freshly-created Patient row from asking for the same account consent again.
    The distinct audit kind avoids pretending the click happened in this chat.
    """
    if conversation_id is None:
        return False
    try:
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None or conversation.patient_id is None:
                    return False
                patient = await session.get(Patient, conversation.patient_id)
                if patient is None:
                    return False
                subject_ref = patient.external_id or patient.wa_id
                if not subject_ref:
                    return False
                if patient.lgpd_accepted_at is None:
                    patient.lgpd_accepted_at = datetime.now(UTC)
                    session.add(
                        ConsentEvent(
                            tenant_id=conversation.tenant_id,
                            wa_id=subject_ref,
                            kind="account_terms_verified",
                            legal_basis=(
                                "consentimento de conta previamente verificado "
                                "pelo brain-api"
                            ),
                        )
                    )
        return True
    except Exception as exc:
        logger.warning(
            "verified_account_consent_write_failed",
            error_type=type(exc).__name__,
            conversation_id=str(conversation_id),
        )
        return False

async def _conversation_patient_has_name(conversation_id: UUID | None) -> bool:
    """Whether this conversation's Patient row already carries a name.

    A read failure answers True — i.e. "do not ask": the name is a courtesy the
    flow can live without (`services/patient_name.py`), so an unreadable row
    must not park a verified account in a question it may already have answered.
    """
    if conversation_id is None:
        return True
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, conversation_id)
            if conversation is None or conversation.patient_id is None:
                return True
            patient = await session.get(Patient, conversation.patient_id)
            return patient is None or bool((patient.name or "").strip())
    except Exception as exc:
        logger.warning(
            "patient_name_read_failed",
            error_type=type(exc).__name__,
            conversation_id=str(conversation_id),
        )
        return True

async def _adopt_account_name(conversation_id: UUID | None, name: str) -> None:
    """Put the account's name on this conversation's Patient row, if it has none.

    Only ever called with a name brain-api returned for a PROVEN account (the
    verification of the known-address code). Never overwrites: a name typed here
    is the patient's own and outranks one learned from another clinic. Written
    to `Patient.name` and nowhere else, so the pseudonymizer masks it like any
    other (`services/patient_name.py`). A failed write is logged and swallowed:
    the worst case is that the name is asked once.
    """
    if conversation_id is None:
        return
    try:
        async with async_session_factory() as session:
            async with session.begin():
                conversation = await session.get(Conversation, conversation_id)
                if conversation is None or conversation.patient_id is None:
                    return
                patient = await session.get(Patient, conversation.patient_id)
                if patient is not None and not (patient.name or "").strip():
                    patient.name = name
    except Exception as exc:
        logger.warning(
            "account_name_write_failed",
            error_type=type(exc).__name__,
            conversation_id=str(conversation_id),
        )

async def _handle_pre_consent_identity(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    professionals: list | None,
    patient_wa: str | None,
    redis,
    waba_token: str | None,
) -> bool:
    """Resolve brain-api identity before sending any local LGPD notice.

    Returns True when the identity branch sent the complete answer for this
    turn. PENDING_CLAIMED/UNKNOWN/UNAVAILABLE return False so the caller keeps
    the pre-existing LGPD behaviour.
    """
    if reply.tenant_id is None:
        return False
    state = await probe_identity(reply.tenant_id, reply.patient_ref)
    if state is IdentityState.PENDING_UNCLAIMED:
        await _write_flow_state(reply.conversation_id, FlowState.AWAITING_EMAIL)
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=EMAIL_REQUEST_MESSAGE,
            event="pending_email_requested",
        )
        return True
    if state is not IdentityState.VERIFIED:
        return False
    if not await _record_verified_account_consent(reply.conversation_id):
        # Fail closed on the local audit write: a verified remote account does
        # not justify bypassing a local gate we failed to persist.
        return False
    if not await _conversation_patient_has_name(reply.conversation_id):
        # The clinic needs the name — the calendar event's title, the
        # professional's e-mail, the PreCheck hand-off (owner, 2026-09-24).
        # brain-api sends it on the `open` when the ACCOUNT already gave one at
        # another clinic, and then this is skipped; otherwise it is asked once,
        # here, and still no e-mail and no code. The answer leaves AWAITING_NAME
        # through `name_captured`, which sends a consented patient to the menu.
        await _write_flow_state(reply.conversation_id, FlowState.AWAITING_NAME)
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=NAME_REQUEST_MESSAGE,
            event="verified_account_name_requested",
        )
        return True
    # A known account: the context-aware opening (upcoming appointment, the
    # first visit after a consult, or the menu) instead of the bare menu.
    await _send_context_opening(
        reply,
        tenant,
        patient_wa,
        redis=redis,
        waba_token=waba_token,
        source="verified_account",
    )
    return True
