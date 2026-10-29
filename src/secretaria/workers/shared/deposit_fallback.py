"""deposit_fallback - book WITHOUT the deposit when the Pix cannot be taken (TASK-071, Plan 3).

A technical failure of the deposit never stops a patient from booking any more. The router's
"Confirmar" is handled by the gate (`BookingGate.continue_without_deposit`, in
`services/booking_hold.py`); this module is the same decision for the worker paths that run
AFTER a reservation exists:

  * the Portal code was verified and the Pix could not be started
    (`deposit_turn._payment_after_code`);
  * the Pix was created but could not be delivered to the patient
    (`deposit_request._abort_deposit_request`).

In both, the reservation of the payment is gone (`begin_payment` / `abandon_charge` released it),
so the slot is reserved AGAIN in the plain code phase and handed to the ordinary promotion
(`booking_hold._promote_booking_hold`): the slot is re-checked on the agenda, the event and the
`Appointment` are created exactly like any no-deposit Portal booking, and a slot taken meanwhile
gets the same day's other times. No `pix_deposits` row is left waiting, the conversation never
stays in `AWAITING_DEPOSIT` and no deposit deadline can cancel the booking later.

The clinic (and the doctor) are told by `deposit_failure_notice.notify_deposit_failure`, which
never raises. Log lines carry ids and closed vocabularies only.

When the booking cannot be written at all, nobody takes the chat over on their own (owner,
2026-10-29): the patient is told the time was not booked and gets the fixed "quer que eu chame
nosso atendente humano?" card (`flow_router.enter_human_offer`, Sim / Não) - `_offer_human`.
"""

from dataclasses import dataclass, field

from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    Tenant,
)
from secretaria.services.booking_hold import (
    HeldSlot,
    LiveChargeHoldError,
    latest_hold_in,
    place_hold,
)
from secretaria.services.flow_router import (
    HUMAN_OFFER_BOOKING_BODY,
    LABEL_RETRY_MENU,
    LABEL_RETRY_YES,
    STEP_AWAITING_RETRY,
    FlowRouterResult,
    MenuBubble,
    enter_human_offer,
)
from secretaria.services.payments import deposit_failure_notice, deposit_lifecycle
from secretaria.services.payments.failure_reason import FailureReason
from secretaria.services.pending_identity import (
    BOOKING_SLOT_TAKEN_MESSAGE,
)
from secretaria.workers.shared.booking_hold import (
    _PROMOTE_FAILED_MESSAGE,
    SlotTakenAtPromotion,
    _promote_booking_hold,
    _tenant_tzinfo,
)
from secretaria.workers.shared.context import _ReplyContext

logger = get_logger(__name__)


@dataclass(frozen=True)
class WaiverNotice:
    """What `_notify_deposit_waived` needs, kept until the booking is known to exist (review fix,
    2026-10-29): the clinic's e-mail says the patient booked WITHOUT the deposit, so it is sent
    only after a successful promotion; on failure the charge failure is still recorded."""

    held: HeldSlot
    code: str
    reason: FailureReason
    stage: str
    provider: str | None = None
    record: bool = True


@dataclass(frozen=True)
class DepositWaived:
    """`_payment_after_code`'s answer when the deposit was DROPPED for a technical reason (task
    6): the code-phase hold is ready for the promotion, exactly like "no deposit due" (None), and
    `reason` (a `FailureReason` value) goes with it onto the appointment so the agenda can mark
    the booking made without its deposit. `notice` is the clinic's notice, delivered by
    `_promote_waived` once the outcome of the booking is known."""

    reason: str
    notice: WaiverNotice | None = field(default=None, compare=False)


async def _notify_deposit_waived(
    reply: _ReplyContext,
    tenant: Tenant,
    held: HeldSlot,
    *,
    code: str,
    reason: FailureReason,
    stage: str,
    redis,
    provider: str | None = None,
    record: bool = True,
    send_email: bool = True,
) -> None:
    """Log the dropped deposit (ids + closed vocabulary) and tell the clinic. Never raises.

    `record=False` is the breaker's skip (task 5): the clinic is told, but no new failure is
    recorded, so the breaker's cooldown keeps counting from the last REAL charge failure.
    `send_email=False`: the booking was not made - record only, no "booked without deposit" mail."""
    logger.warning(
        "deposit_waived_after_hold",
        code=code,
        reason=str(reason),
        stage=stage,
        tenant_id=str(tenant.id),
        conversation_id=str(reply.conversation_id),
    )
    if not deposit_failure_notice.tells_the_clinic(stage):
        # The charge itself did not fail (e.g. our message could not be delivered): the
        # account stays healthy and the clinic gets no deposit-failure e-mail.
        return
    patient_name = held.attendee_name
    if not patient_name:
        try:
            async with async_session_factory() as session:
                patient_id = held.patient_id or await session.scalar(
                    select(Conversation.patient_id).where(Conversation.id == reply.conversation_id)
                )
                if patient_id is not None:
                    patient_name = await session.scalar(
                        select(Patient.name).where(Patient.id == patient_id)
                    )
        except Exception as exc:  # noqa: BLE001 - a name is never worth a booking
            logger.warning("deposit_waived_patient_name_failed", error_type=type(exc).__name__)
    await deposit_failure_notice.notify_deposit_failure(
        tenant_id=tenant.id,
        patient_name=patient_name,
        service=held.appointment_type,
        start_at=held.start_at,
        reason=reason,
        stage=stage,
        professional_id=held.professional_id,
        provider=provider or tenant.payment_provider or "asaas",
        redis=redis,
        record=record,
        send_email=send_email,
    )


async def _hold_again(held: HeldSlot) -> HeldSlot | None:
    """Reserve the same window again, in the plain code phase (what `_promote_booking_hold`
    promotes). None = somebody else took it meanwhile. Raises `LiveChargeHoldError` when this
    conversation still has a payable charge (then nothing may be confirmed here)."""
    return await place_hold(
        tenant_id=held.tenant_id,
        conversation_id=held.conversation_id,
        patient_id=held.patient_id,
        professional_id=held.professional_id,
        appointment_type=held.appointment_type,
        insurance=held.insurance,
        start_at=held.start_at,
        end_at=held.end_at,
        attendee_name=held.attendee_name,
    )


def _slot_taken_retry(held: HeldSlot, tenant: Tenant) -> FlowRouterResult:
    """The router's own `slot_taken` answer, rebuilt from the reservation: one tap to the same
    day's other times."""
    local = held.start_at.astimezone(_tenant_tzinfo(tenant))
    return FlowRouterResult(
        action="reply",
        bubbles=[
            MenuBubble(body=BOOKING_SLOT_TAKEN_MESSAGE, labels=[LABEL_RETRY_YES, LABEL_RETRY_MENU])
        ],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_RETRY,
        flow_selected_type=held.appointment_type,
        flow_selected_day=local.strftime("%Y-%m-%d"),
        flow_selected_slot=local.strftime("%Y-%m-%dT%H:%M"),
        flow_selected_professional_id=held.professional_id,
        flow_selected_insurance=held.insurance,
        flow_attendee_name=held.attendee_name,
    )


def _offer_human(
    sentence: str, held: HeldSlot | None = None, *, draft=None
) -> FlowRouterResult:
    """`sentence` (what happened), then the Sim / Não card of `enter_human_offer` asking
    `HUMAN_OFFER_BOOKING_BODY` (the time was not booked).

    Owner, 2026-10-29: in the deposit flow the patient CHOOSES - "✅ Sim" hands the chat to the
    team exactly like any accepted offer (`flow_router._handle_human_offer`), "❌ Não" keeps it
    with the AI. The reservation's doctor / service survive (when known: `held`, or `draft` - any
    object with the `flow_selected_*` / `flow_attendee_name` fields, such as the failed result or
    the conversation) so an accepted offer still alerts the right doctor."""
    result = enter_human_offer(body=HUMAN_OFFER_BOOKING_BODY)
    if draft is not None:
        result.flow_selected_type = getattr(draft, "flow_selected_type", None)
        result.flow_selected_professional_id = getattr(
            draft, "flow_selected_professional_id", None
        )
        result.flow_selected_insurance = getattr(draft, "flow_selected_insurance", None)
        result.flow_attendee_name = getattr(draft, "flow_attendee_name", None)
    if held is not None:
        result.flow_selected_type = held.appointment_type
        result.flow_selected_professional_id = held.professional_id
        result.flow_selected_insurance = held.insurance
        result.flow_attendee_name = held.attendee_name
    result.bubbles = [TextBubble(body=sentence), *result.bubbles]
    return result


def _confirm_failed(held: HeldSlot | None = None) -> FlowRouterResult:
    """The booking itself could not be written (database / agenda): say the time was not booked
    and offer a person (Sim / Não)."""
    return _offer_human(deposit_lifecycle.BEFORE_CONFIRM_FAILED_TEXT, held)


async def _rehold_for_promotion(
    reply: _ReplyContext, tenant: Tenant, held: HeldSlot
) -> FlowRouterResult | None:
    """Reserve the slot again; None = ready for `_promote_waived` (which tells the clinic).

    Otherwise the result to apply instead: the slot was taken meanwhile (other times), or this
    conversation still has a live charge (nothing is booked; a person is offered)."""
    try:
        fresh = await _hold_again(held)
    except LiveChargeHoldError:
        logger.error(
            "deposit_waived_live_charge",
            tenant_id=str(tenant.id),
            conversation_id=str(reply.conversation_id),
        )
        return _confirm_failed(held)
    if fresh is None:
        return _slot_taken_retry(held, tenant)
    return None


async def _appointment_exists(reply: _ReplyContext, held: HeldSlot | None) -> bool:
    """Whether the promotion wrote the appointment of `held` for this conversation. A failed
    read is "no" (the e-mail is skipped; the charge failure is still recorded)."""
    if held is None:
        return False
    try:
        async with async_session_factory() as session:
            found = await session.scalar(
                select(Appointment.id).where(
                    Appointment.conversation_id == reply.conversation_id,
                    Appointment.start_at == held.start_at,
                    Appointment.status == AppointmentStatus.SCHEDULED,
                )
            )
    except Exception as exc:  # noqa: BLE001
        logger.warning("deposit_waived_booking_check_failed", error_type=type(exc).__name__)
        return False
    return found is not None


async def _promote_waived(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    waba_token: str | None,
    redis,
    waived: DepositWaived,
) -> str | SlotTakenAtPromotion | FlowRouterResult | None:
    """Promote the code-phase hold of a booking whose deposit was DROPPED, then tell the clinic.

    The promotion never hands the chat over (`hand_over=False`); a booking that cannot be written
    answers the explanation + Sim / Não card (`_confirm_failed`, a `FlowRouterResult`) and its hold
    is already released. The clinic's "booked without the deposit" e-mail goes out only when the
    booking exists; otherwise the charge failure is recorded and nothing is mailed. Other answers
    are the promotion's own (confirmation text, `SlotTakenAtPromotion`, expiry text, None)."""
    async with async_session_factory() as session:
        held = await latest_hold_in(session, reply.conversation_id)
    promoted = await _promote_booking_hold(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        redis=redis,
        deposit_waived_reason=waived.reason,
        hand_over=False,
    )
    failed = promoted == _PROMOTE_FAILED_MESSAGE
    # A positive signal, never "any other text": the promotion also answers texts with nothing
    # booked (expiry, a taken slot without a re-offer). Booked = the appointment row exists.
    booked = isinstance(promoted, str) and not failed and await _appointment_exists(reply, held)
    notice = waived.notice
    if notice is not None:
        await _notify_deposit_waived(
            reply,
            tenant,
            notice.held,
            code=notice.code,
            reason=notice.reason,
            stage=notice.stage,
            redis=redis,
            provider=notice.provider,
            record=notice.record,
            send_email=booked,
        )
    if failed:
        return _confirm_failed(held)
    return promoted


async def _confirm_without_deposit(
    reply: _ReplyContext,
    *,
    tenant: Tenant,
    held: HeldSlot,
    code: str,
    reason: FailureReason,
    stage: str,
    waba_token: str | None,
    redis,
) -> FlowRouterResult:
    """Book the reserved slot WITHOUT the deposit and return what the patient is told: the
    normal confirmation, the same day's other times, or the offer of a person when it cannot be
    written."""
    instead = await _rehold_for_promotion(reply, tenant, held)
    if instead is not None:
        return instead
    promoted = await _promote_waived(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        redis=redis,
        waived=DepositWaived(
            str(reason), WaiverNotice(held, code=code, reason=reason, stage=stage)
        ),
    )
    if isinstance(promoted, SlotTakenAtPromotion):
        return promoted.reoffer
    if isinstance(promoted, FlowRouterResult):
        return promoted  # not written: the explanation + Sim / Não card
    if promoted is None:
        return _confirm_failed(held)
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=promoted)],
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
    )
