"""deposit_turn - the turns of a conversation waiting for a Pix or a payer field (TASK-017).

Written in the old monolithic workers/tasks.py by TASK-017 and moved here when the Pix work
was brought onto the split worker package (TASK-050, after TASK-023). Channel-neutral.

  * `_handle_deposit_wait` - one turn of `FlowState.AWAITING_DEPOSIT` (the card's three taps,
    the lazy expiry, anything else), set up by `turn_router._route_inbound_turn`.
  * `_handle_payer_data` / `_keep_payer_data` / `_payer_data_unavailable` /
    `_resume_after_deposit` - the ends of the payer-data wait (decision D7).
  * `_payment_after_code` - the Portal code was verified for a held slot: Pix first?
    Called from `orchestrator._send_bot_reply_inner`.

Every state change goes through `shared/flow_runner.py::_apply_flow_result` (or
`_write_flow_state`), the single persistence seam.
"""

from dataclasses import replace as dataclass_replace
from datetime import UTC, datetime

from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Conversation,
    FlowState,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from secretaria.services import deposit_wait
from secretaria.services.booking_hold import conversation_has_live_charge, latest_hold_in
from secretaria.services.flow_router import (
    LABEL_RETRY_MENU,
    LABEL_RETRY_YES,
    STEP_AWAITING_RETRY,
    FlowRouterResult,
    MenuBubble,
)
from secretaria.services.payments import (
    booking_charge,
    deposit_gate,
    deposit_lifecycle,
    payer_data,
    payer_data_after,
)
from secretaria.services.payments.deposit_failure_notice import (
    BREAKER_STAGE,
    breaker_reason,
    continues_without_deposit,
    failure_reason_for,
)
from secretaria.services.payments.failure_reason import FailureReason
from secretaria.services.pending_identity import BOOKING_HOLD_EXPIRED_MESSAGE
from secretaria.workers.shared.booking_hold import _release_hold, _tenant_tzinfo
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.deposit_fallback import (
    DepositWaived,
    _notify_deposit_waived,
    _offer_human,
    _rehold_for_promotion,
)
from secretaria.workers.shared.deposit_request import _arq_enqueue, _retry_buttons
from secretaria.workers.shared.dispatch import _send_buttons_reply, _send_plain_reply
from secretaria.workers.shared.flow_runner import _apply_flow_result
from secretaria.workers.shared.handover import _set_conversation_human_active
from secretaria.workers.shared.state_expiry import _write_flow_state

logger = get_logger(__name__)


async def _handle_deposit_wait(
    reply: _ReplyContext,
    *,
    tenant: Tenant | None,
    waba_token: str | None,
    redis,
    patient_wa: str | None,
) -> None:
    """One turn of `AWAITING_DEPOSIT`: the card's three taps, or anything else.

    Deterministic, zero-LLM. "Já paguei" asks the provider and, if it agrees, runs the same
    promotion the webhook would; "Trocar horário" and "Cancelar reserva" void the charge first
    (a paid charge is never deleted, it is settled) and only then free the slot. The state moves
    ONLY through `_apply_flow_result`, the single persistence seam.
    """
    if tenant is None:
        return
    action = reply.deposit_wait_action

    async def say(body: str, event: str) -> None:
        await _send_plain_reply(reply, tenant=tenant, waba_token=waba_token, body=body, event=event)

    if action == "expired":
        # The window is over by OUR clock, but the provider is the authority on the money: a
        # patient who paid in the last seconds (webhook still in flight) must never hear that the
        # Pix expired. Ask first; if it was paid, the chat goes back to waiting for the promotion.
        if await _expired_but_paid(reply, tenant, redis):
            await _write_flow_state(reply.conversation_id, FlowState.AWAITING_DEPOSIT)
            await say(deposit_lifecycle.BEFORE_PAYMENT_FOUND_TEXT, "deposit_wait_payment_found")
            return
        await _send_buttons_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=deposit_lifecycle.BEFORE_EXPIRED_TEXT,
            buttons=_retry_buttons(),
            event="deposit_wait_expired_notice_sent",
        )
        return
    if action == "paid_pending":
        await say(deposit_lifecycle.BEFORE_PAYMENT_FOUND_TEXT, "deposit_wait_paid_pending_sent")
        return

    async with async_session_factory() as session:
        deposit = (
            await session.get(PixDeposit, reply.deposit_wait_id) if reply.deposit_wait_id else None
        )
        conversation = await session.get(Conversation, reply.conversation_id)
    if deposit is None or deposit.tenant_id != tenant.id or conversation is None:
        await say(deposit_lifecycle.BEFORE_EXPIRED_TEXT, "deposit_wait_gone_sent")
        return

    if action == "paid":
        await _tell_payment_check(reply, tenant, deposit, redis, say)
        return

    if action in ("change", "cancel"):
        async with async_session_factory() as session:
            outcome = await booking_charge.abandon_charge(session, tenant, deposit.id)
        if outcome == "paid":
            # The provider has the money: settle it (never delete a paid charge).
            await _tell_payment_check(reply, tenant, deposit, redis, say)
            return
        if action == "change":
            result = FlowRouterResult(
                action="reply",
                bubbles=[
                    MenuBubble(
                        body=deposit_lifecycle.BEFORE_CHANGE_TEXT,
                        labels=[LABEL_RETRY_YES, LABEL_RETRY_MENU],
                    )
                ],
                flow_state=FlowState.SERVICE_CATALOG,
                flow_step=STEP_AWAITING_RETRY,
                flow_selected_type=conversation.flow_selected_type,
                flow_selected_day=conversation.flow_selected_day,
                flow_selected_slot=conversation.flow_selected_slot,
                flow_selected_professional_id=conversation.flow_selected_professional_id,
                flow_selected_insurance=conversation.flow_selected_insurance,
                flow_attendee_name=conversation.flow_attendee_name,
            )
        else:
            result = FlowRouterResult(
                action="reply",
                bubbles=[TextBubble(body=deposit_lifecycle.BEFORE_RELEASED_TEXT)],
                flow_state=FlowState.IDLE,
            )
        await _apply_flow_result(
            reply, result, patient_wa, redis=redis, tenant=tenant, waba_token=waba_token
        )
        return

    # Anything else: the patient is still paying, so repeat where things stand. The card keeps
    # the three ways out one tap away; the state does not move.
    until = deposit.expires_at.astimezone(_tenant_tzinfo(tenant)).strftime("%H:%M")
    await _send_buttons_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=deposit_lifecycle.before_wait_card_text(until),
        buttons=list(deposit_lifecycle.DEPOSIT_WAIT_BUTTONS),
        event="deposit_wait_card_repeated",
    )


async def _expired_but_paid(reply: _ReplyContext, tenant: Tenant, redis) -> bool:
    """The lazy exit of `AWAITING_DEPOSIT`: does the provider say the charge was paid after all?
    Fail-safe: any doubt (no deposit, provider error) is "not paid" and the retry card stands;
    the reconciliation job still promotes a late payment afterwards."""
    if reply.deposit_wait_id is None:
        return False
    async with async_session_factory() as session:
        deposit = await session.get(PixDeposit, reply.deposit_wait_id)
        if deposit is None or deposit.tenant_id != tenant.id:
            return False
        if deposit.status == PixDepositStatus.PAID:
            return True
        check = await deposit_wait.check_paid(session, tenant, deposit, _arq_enqueue(redis))
        await session.commit()
    return check.outcome == "paid"


async def _tell_payment_check(reply, tenant, deposit, redis, say) -> None:
    """ "Já paguei": ask the provider, inject the paid event if it agrees, answer honestly."""
    async with async_session_factory() as session:
        check = await deposit_wait.check_paid(session, tenant, deposit, _arq_enqueue(redis))
    if check.outcome == "paid":
        await say(deposit_lifecycle.BEFORE_PAYMENT_FOUND_TEXT, "deposit_wait_payment_found")
    else:
        await say(deposit_lifecycle.BEFORE_STILL_WAITING_TEXT, "deposit_wait_still_waiting")


async def _keep_payer_data(reply: _ReplyContext, *, tenant: Tenant | None, redis) -> bool:
    """Store the just-given value (encrypted, short TTL). False = it could not be kept."""
    if tenant is None or reply.conversation_id is None or reply.payer_data_answer is None:
        return False
    return await payer_data.remember(
        redis,
        tenant_id=tenant.id,
        conversation_id=reply.conversation_id,
        kind=reply.payer_data_kind,
        value=reply.payer_data_answer,
    )


async def _handle_payer_data(
    reply: _ReplyContext, *, tenant: Tenant | None, waba_token: str | None, redis
) -> None:
    """The ends of the payer-data wait (the inbound ladder already moved the state).

    "invalid" asks again in plain text (no button; a typed refusal still ends the wait);
    "captured_after" stores the answer and resumes the Pix of the consultation that was waiting;
    "expired" offers the retry card in the `before` timing (the slot can be picked again);
    "optout" and "exhausted" in `before` say the slot was NOT reserved and offer a person with
    the fixed Sim / Não card (owner, 2026-10-29: the patient chooses, nobody takes over on their
    own); "optout", "exhausted" and "expired" in `after` hand the conversation to a human with a
    message that says the consultation stands. All of them forget whatever was stored.
    Best-effort sends, like every other reply of the worker.
    """
    if tenant is None or reply.conversation_id is None:
        return
    action, kind, timing = reply.payer_data_action, reply.payer_data_kind, reply.payer_data_timing
    if action == "captured_after":
        if not await _keep_payer_data(reply, tenant=tenant, redis=redis):
            await _payer_data_unavailable(reply, tenant=tenant, waba_token=waba_token, redis=redis)
            return
        await _resume_after_deposit(reply, tenant=tenant, waba_token=waba_token, redis=redis)
        return
    if action == "invalid":
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=payer_data.invalid_text(kind),
            event="payer_data_reasked",
        )
        return
    await payer_data.discard(redis, tenant_id=tenant.id, conversation_id=reply.conversation_id)
    if action == "expired" and timing == "before":
        await _send_buttons_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=payer_data.expired_text("before"),
            buttons=_retry_buttons(),
            event="payer_data_expired_notice_sent",
        )
        return
    body = {
        "optout": payer_data.optout_text(timing, kind),
        "exhausted": payer_data.exhausted_text(timing),
    }.get(action or "", payer_data.expired_text(timing))
    if timing == "before" and action in ("optout", "exhausted"):
        logger.info(
            "payer_data_human_offered",
            outcome=action,
            conversation_id=str(reply.conversation_id),
        )
        await _apply_flow_result(
            reply, _offer_human(body), None, redis=redis, tenant=tenant, waba_token=waba_token
        )
        return
    await _set_conversation_human_active(reply.conversation_id)
    await _send_plain_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=body,
        event=f"payer_data_{action}_notice_sent",
    )


async def _payer_data_unavailable(
    reply: _ReplyContext, *, tenant: Tenant | None, waba_token: str | None, redis
) -> _ReplyContext | None:
    """The value could not be kept (Redis unreachable).

    `before` (TASK-071 Plan 3): a technical failure of the deposit never stops the booking. The
    answer is dropped and the turn goes on as the "Confirmar" it completes, with the deposit
    step waived: the returned context is the one the caller continues with (the booking gate
    books WITHOUT the deposit and tells the clinic). `after`: the consultation stands; a human
    takes over the Pix and None is returned (the turn is over)."""
    if tenant is None or reply.conversation_id is None:
        return None
    logger.error(
        "payer_data_store_unavailable",
        conversation_id=str(reply.conversation_id),
        tenant_id=str(tenant.id),
        timing=reply.payer_data_timing,
    )
    if reply.payer_data_timing != "after":
        return dataclass_replace(
            reply,
            payer_data_answer=None,
            deposit_waived_reason=FailureReason.unknown.value,
        )
    await _write_flow_state(reply.conversation_id, FlowState.IDLE)
    await _set_conversation_human_active(reply.conversation_id)
    await _send_plain_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=payer_data.AFTER_FAILED_TEXT,
        event="payer_data_unavailable_notice_sent",
    )
    return None


async def _resume_after_deposit(
    reply: _ReplyContext, *, tenant: Tenant, waba_token: str | None, redis
) -> None:
    """`after` timing: the answer is stored; create and send the Pix of the consultation that
    stands. A Pix that cannot be made is told to the patient and handed to a human (never a
    silent skip for a patient who was just asked)."""
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        patient = await session.get(Patient, conversation.patient_id) if conversation else None
        fresh = await session.get(Tenant, tenant.id)
        if conversation is None or patient is None or fresh is None:
            return
        verdict = await payer_data_after.resume_after_booking(
            session,
            tenant=fresh,
            patient=patient,
            conversation_id=reply.conversation_id,
            waba_token=waba_token,
            redis=redis,
        )
    logger.info(
        "payer_data_after_resumed", conversation_id=str(reply.conversation_id), verdict=verdict
    )
    if verdict in ("failed", "nothing_pending"):
        await _set_conversation_human_active(reply.conversation_id)
        await _send_plain_reply(
            reply,
            tenant=tenant,
            waba_token=waba_token,
            body=payer_data.AFTER_FAILED_TEXT,
            event="payer_data_after_failed_notice_sent",
        )


def _payer_data_request(held, tenant: Tenant, kind: str, provider: str | None) -> FlowRouterResult:
    """Ask for the payer field after the Portal code, replayable: the selection is rebuilt from the
    reservation that was just released (`flow_selected_slot` is the clinic's local time)."""
    local = held.start_at.astimezone(_tenant_tzinfo(tenant))
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=payer_data.ask_text(kind, provider))],
        flow_state=FlowState.AWAITING_PAYER_DATA,
        flow_step=payer_data.step_for("before", kind, 0),
        flow_selected_type=held.appointment_type,
        flow_selected_day=local.strftime("%Y-%m-%d"),
        flow_selected_slot=local.strftime("%Y-%m-%dT%H:%M"),
        flow_selected_professional_id=held.professional_id,
        flow_selected_insurance=held.insurance,
        flow_attendee_name=held.attendee_name,
    )


async def _payment_after_code(
    reply: _ReplyContext, *, tenant: Tenant | None, redis
) -> FlowRouterResult | DepositWaived | None:
    """The Portal code was verified for a held slot: does the clinic want a deposit first?

    None = no, promote exactly as before. Otherwise the result to apply: the Pix request on the
    SAME hold (extended to the payment phase, `booking_charge.begin_payment`), or - when the
    deposit cannot be asked for (`charge_pending` / `no_patient` only) - a hand-over, or a
    plain "the slot was released" when someone else took it meanwhile. A TECHNICAL failure of
    the deposit (TASK-071 Plan 3) is a `DepositWaived`: promote it like None (the clinic is
    told; the slot was reserved again when `begin_payment` had released it), handing its
    `reason` to `_promote_booking_hold` so the appointment carries the agenda mark (task 6).
    """
    if tenant is None or reply.conversation_id is None:
        return None
    async with async_session_factory() as session:
        held = await latest_hold_in(session, reply.conversation_id)
        if held is None or held.phase != "code" or held.expires_at <= datetime.now(UTC):
            return None
        patient_id = await session.scalar(
            select(Conversation.patient_id).where(Conversation.id == reply.conversation_id)
        )
        try:
            quote = await deposit_gate.quote_deposit(
                session,
                tenant,
                patient_id=patient_id,
                appointment_type=held.appointment_type,
                professional_id=held.professional_id,
                insurance=held.insurance,
                redis=redis,
            )
        except Exception as exc:  # noqa: BLE001 - as the gate: a technical failure, `quote_failed`
            logger.warning(
                "deposit_after_code_quote_failed",
                error_type=type(exc).__name__,
                conversation_id=str(reply.conversation_id),
            )
            quote = deposit_gate.DepositQuote("blocked", "quote_failed")
    if not quote.required and not quote.blocked:
        return None

    async def blocked(reason: str) -> FlowRouterResult:
        logger.warning(
            "deposit_after_code_blocked",
            reason=reason,
            conversation_id=str(reply.conversation_id),
        )
        await _release_hold(held.id)
        return FlowRouterResult(
            action="handover",
            bubbles=[TextBubble(body=deposit_lifecycle.BEFORE_BLOCKED_TEXT)],
            flow_state=FlowState.IDLE,
        )

    if quote.blocked:
        if not continues_without_deposit(quote.reason):
            return await blocked(quote.reason)
        if await conversation_has_live_charge(reply.conversation_id):
            # Ruling R13 (as the gate): a dropped deposit never books beside a payable Pix of
            # this conversation - that is `charge_pending`, not a technical failure.
            return await blocked("charge_pending")
        # TASK-071 Plan 3 (ruling R6): the deposit cannot be asked for for a technical reason.
        # The code-phase hold is untouched: tell the clinic and promote it exactly as a booking
        # with no deposit due (None).
        await _notify_deposit_waived(
            reply,
            tenant,
            held,
            code=quote.reason,
            reason=failure_reason_for(quote.reason),
            stage="quote",
            redis=redis,
        )
        return DepositWaived(str(failure_reason_for(quote.reason)))
    # TASK-071 Plan 3, task 5: the clinic's account is known to be failing for an account-level
    # reason (the breaker is open). No CPF question, no customer, no charge: the code-phase hold
    # is promoted exactly as a booking with no deposit due, and the clinic is told (no new
    # failure is recorded, so the cooldown keeps counting from the last real one).
    provider = quote.provider or tenant.payment_provider or "asaas"
    breaker = await breaker_reason(tenant.id, provider)
    if breaker is not None:
        await _notify_deposit_waived(
            reply,
            tenant,
            held,
            code="breaker_open",
            reason=breaker,
            stage=BREAKER_STAGE,
            redis=redis,
            provider=provider,
            record=False,
        )
        return DepositWaived(str(breaker))
    payer = await payer_data.recall_all(
        redis, tenant_id=tenant.id, conversation_id=reply.conversation_id
    )
    try:
        payment = await booking_charge.begin_payment(
            tenant_id=tenant.id,
            conversation_id=reply.conversation_id,
            patient_id=patient_id,
            quote=quote,
            professional_id=held.professional_id,
            appointment_type=held.appointment_type or "Consulta",
            insurance=held.insurance,
            attendee_name=held.attendee_name,
            start_at=held.start_at,
            end_at=held.end_at,
            existing_hold=held,
            payer_document=payer.document,
            payer_email=payer.email,
        )
    except booking_charge.ChargeStartError as exc:
        needed = payer_data.kind_for_code(exc.code)
        if needed is not None and not payer.has(needed):
            # Decision D12 then D7: the code first (the address is proven), then the payer field.
            # `begin_payment` already released the reservation; the answer replays "Confirmar".
            return _payer_data_request(held, tenant, needed, quote.provider)
        await payer_data.discard(redis, tenant_id=tenant.id, conversation_id=reply.conversation_id)
        if not continues_without_deposit(exc.code, exc.reason):
            return await blocked(exc.code)
        # TASK-071 Plan 3: a technical failure never stops the booking. `begin_payment` released
        # the reservation; it is placed again (code phase) and the caller promotes it like any
        # booking with no deposit due (None), re-checking the slot on the agenda first.
        waived = failure_reason_for(exc.code, exc.reason)
        instead = await _rehold_for_promotion(
            reply,
            tenant,
            held,
            code=exc.code,
            reason=waived,
            stage=exc.stage or "charge",
            redis=redis,
            provider=quote.provider,
        )
        return instead if instead is not None else DepositWaived(str(waived))
    if payment is None:
        return FlowRouterResult(
            action="reply",
            bubbles=[TextBubble(body=BOOKING_HOLD_EXPIRED_MESSAGE)],
            flow_state=FlowState.IDLE,
        )
    await payer_data.discard(redis, tenant_id=tenant.id, conversation_id=reply.conversation_id)
    return FlowRouterResult(
        action="reply",
        flow_state=FlowState.AWAITING_DEPOSIT,
        flow_selected_type=held.appointment_type,
        flow_selected_professional_id=held.professional_id,
        flow_selected_insurance=held.insurance,
        flow_attendee_name=held.attendee_name,
        deposit_request={
            "deposit_id": payment.deposit_id,
            "amount_cents": payment.amount_cents,
            "copy_paste": payment.copy_paste,
            "expires_at": payment.expires_at,
            "start_at": payment.start_at,
            "end_at": payment.end_at,
            "service": payment.service,
            "minutes": payment.minutes,
            "payment_note": None,
        },
    )
