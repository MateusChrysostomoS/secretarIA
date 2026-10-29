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
"""

from dataclasses import dataclass

from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, FlowState, Patient, Tenant
from secretaria.services.booking_hold import HeldSlot, LiveChargeHoldError, place_hold
from secretaria.services.flow_router import (
    LABEL_RETRY_MENU,
    LABEL_RETRY_YES,
    STEP_AWAITING_RETRY,
    FlowRouterResult,
    MenuBubble,
)
from secretaria.services.payments import deposit_failure_notice, deposit_lifecycle
from secretaria.services.payments.failure_reason import FailureReason
from secretaria.services.pending_identity import BOOKING_SLOT_TAKEN_MESSAGE
from secretaria.workers.shared.booking_hold import (
    _PROMOTE_FAILED_MESSAGE,
    SlotTakenAtPromotion,
    _promote_booking_hold,
    _tenant_tzinfo,
)
from secretaria.workers.shared.context import _ReplyContext

logger = get_logger(__name__)


@dataclass(frozen=True)
class DepositWaived:
    """`_payment_after_code`'s answer when the deposit was DROPPED for a technical reason (task
    6): the code-phase hold is ready for `_promote_booking_hold`, exactly like "no deposit due"
    (None), and `reason` (a `FailureReason` value) goes with it onto the appointment so the
    agenda can mark the booking made without its deposit. The clinic was already told."""

    reason: str


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
) -> None:
    """Log the dropped deposit (ids + closed vocabulary) and tell the clinic. Never raises.

    `record=False` is the breaker's skip (task 5): the clinic is told, but no new failure is
    recorded, so the breaker's cooldown keeps counting from the last REAL charge failure."""
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


def _confirm_failed() -> FlowRouterResult:
    """The booking itself could not be written (database / agenda): a person takes over."""
    return FlowRouterResult(
        action="handover",
        bubbles=[TextBubble(body=deposit_lifecycle.BEFORE_CONFIRM_FAILED_TEXT)],
        flow_state=FlowState.IDLE,
    )


async def _rehold_for_promotion(
    reply: _ReplyContext,
    tenant: Tenant,
    held: HeldSlot,
    *,
    code: str,
    reason: FailureReason,
    stage: str,
    redis,
    provider: str | None = None,
) -> FlowRouterResult | None:
    """Reserve the slot again and tell the clinic; None = ready for `_promote_booking_hold`.

    Otherwise the result to apply instead: the slot was taken meanwhile (other times), or this
    conversation still has a live charge (a person takes over)."""
    try:
        fresh = await _hold_again(held)
    except LiveChargeHoldError:
        logger.error(
            "deposit_waived_live_charge",
            tenant_id=str(tenant.id),
            conversation_id=str(reply.conversation_id),
        )
        return _confirm_failed()
    if fresh is None:
        return _slot_taken_retry(held, tenant)
    await _notify_deposit_waived(
        reply,
        tenant,
        held,
        code=code,
        reason=reason,
        stage=stage,
        redis=redis,
        provider=provider,
    )
    return None


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
    normal confirmation, the same day's other times, or a hand-over when it cannot be written."""
    instead = await _rehold_for_promotion(
        reply, tenant, held, code=code, reason=reason, stage=stage, redis=redis
    )
    if instead is not None:
        return instead
    promoted = await _promote_booking_hold(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        redis=redis,
        deposit_waived_reason=str(reason),
    )
    if isinstance(promoted, SlotTakenAtPromotion):
        return promoted.reoffer
    if promoted is None or promoted == _PROMOTE_FAILED_MESSAGE:
        # Not written (the promotion already handed the chat to a person); its own message
        # speaks of an account activation that this path never had.
        return _confirm_failed()
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=promoted)],
        flow_state=FlowState.IDLE,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_slot=None,
    )
