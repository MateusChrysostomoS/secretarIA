"""The conversation side of a pending Pix: `FlowState.AWAITING_DEPOSIT` (spec sections 4.2, 9).

While a `before` deposit is open the conversation waits in `AWAITING_DEPOSIT`. The skill
`conversation-flow-state` requires every non-IDLE state to have an exit that does NOT depend on
the patient choosing it. This one has three, none of them the patient:

1. the payment lands -> `fulfillment.py` calls `leave_wait` (the consultation is confirmed);
2. the Pix window ends -> the expiry job runs `on_before_charge_expired` (registered below into
   `jobs.EXPIRY_HOOKS`), which frees the hold, parks the chat on the "choose another time?" card
   and tells the patient;
3. an inbound message arrives after the window (the cron has not run yet, or the worker was
   down) -> the lazy exit in `workers/tasks.py::_persist_inbound_message` uses `is_wait_over`.

The patient's own ways out (`Trocar horario`, `Cancelar reserva`, `/menu`) void the charge
first (`booking_charge.abandon_charge`) so the slot they may still be paying for is never
released while the Pix is payable.

Everything here is tenant-scoped and best-effort where it touches a chat: a patient who cannot be
told is logged, never an error that unwinds the money state.
"""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Conversation, FlowState, Patient, PixDeposit, Tenant
from secretaria.models.pix_deposit import DEPOSIT_TIMING_BEFORE, PixDepositStatus
from secretaria.services.payments import booking_charge, deposit_lifecycle, jobs, notify

logger = get_logger(__name__)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def waiting_deposit(
    session: AsyncSession, conversation: Conversation
) -> PixDeposit | None:
    """The open `before` deposit this conversation waits for (the newest), or None."""
    return await session.scalar(
        select(PixDeposit)
        .where(
            PixDeposit.tenant_id == conversation.tenant_id,
            PixDeposit.conversation_id == conversation.id,
            PixDeposit.deposit_timing == DEPOSIT_TIMING_BEFORE,
            PixDeposit.status == PixDepositStatus.AWAITING,
        )
        .order_by(PixDeposit.created_at.desc())
        .limit(1)
    )


def is_wait_over(deposit: PixDeposit | None, *, now: datetime | None = None) -> bool:
    """True when there is nothing left to wait for: no open deposit, or its window ended.

    The window is the hold's TTL (`expires_at`). The cron voids the charge on its own tick; an
    inbound that arrives first must not keep the conversation parked on a dead Pix.
    """
    if deposit is None or deposit.status != PixDepositStatus.AWAITING:
        return True
    if deposit.expires_at is None:
        return False
    return _aware(deposit.expires_at) <= (now or datetime.now(UTC))


def _clear_selection(conversation: Conversation) -> None:
    conversation.flow_step = None
    conversation.flow_selected_type = None
    conversation.flow_selected_day = None
    conversation.flow_selected_slot = None
    conversation.flow_selected_professional_id = None
    conversation.flow_selected_insurance = None
    conversation.flow_attendee_name = None


async def _other_open_deposit(
    session: AsyncSession, conversation: Conversation, deposit: PixDeposit
) -> bool:
    found = await session.scalar(
        select(PixDeposit.id).where(
            PixDeposit.tenant_id == conversation.tenant_id,
            PixDeposit.conversation_id == conversation.id,
            PixDeposit.status == PixDepositStatus.AWAITING,
            PixDeposit.id != deposit.id,
        )
    )
    return found is not None


async def _conversation_of(
    session: AsyncSession, deposit: PixDeposit
) -> Conversation | None:
    if deposit.conversation_id is None:
        return None
    conversation = await session.get(Conversation, deposit.conversation_id)
    if conversation is None or conversation.tenant_id != deposit.tenant_id:
        return None
    return conversation


async def leave_wait(session: AsyncSession, deposit: PixDeposit) -> bool:
    """The deposit is settled (paid and promoted): take its conversation out of
    `AWAITING_DEPOSIT` back to IDLE, clearing the booking selection. A conversation that is no
    longer waiting - or that waits for ANOTHER open deposit - is left alone. Does not commit."""
    conversation = await _conversation_of(session, deposit)
    if conversation is None or conversation.flow_state != FlowState.AWAITING_DEPOSIT:
        return False
    if await _other_open_deposit(session, conversation, deposit):
        return False
    conversation.flow_state = FlowState.IDLE
    _clear_selection(conversation)
    return True


async def on_before_charge_expired(session: AsyncSession, deposit: PixDeposit) -> None:
    """Expiry hook (`jobs.EXPIRY_HOOKS`): the Pix window ended and the charge was voided.

    Frees the reservation, parks the conversation on the existing "choose another time?" retry
    step (the selection is carried, so choosing the same slot again is one tap) and tells the
    patient once (`expired_notice_sent_at`). A patient who already moved on gets no notice.
    """
    if deposit.deposit_timing != DEPOSIT_TIMING_BEFORE:
        return
    await booking_charge.release_hold_of_charge(session, deposit)

    tenant = await session.get(Tenant, deposit.tenant_id)
    conversation = await _conversation_of(session, deposit)
    waiting = (
        conversation is not None
        and conversation.flow_state == FlowState.AWAITING_DEPOSIT
        and not await _other_open_deposit(session, conversation, deposit)
    )
    if waiting:
        from secretaria.services.flow_router import STEP_AWAITING_RETRY

        conversation.flow_state = FlowState.SERVICE_CATALOG
        conversation.flow_step = STEP_AWAITING_RETRY
    notice_owed = deposit.expired_notice_sent_at is None
    deposit.conversation_id = None
    deposit.expired_notice_sent_at = datetime.now(UTC)
    await session.commit()

    if not (waiting and notice_owed and tenant is not None and deposit.patient_id):
        return
    patient = await session.get(Patient, deposit.patient_id)
    if patient is None or patient.tenant_id != tenant.id:
        return
    from secretaria.services.flow_router import LABEL_RETRY_MENU, LABEL_RETRY_YES

    try:
        await notify.deliver(
            session,
            tenant,
            patient,
            [
                notify.Buttons(
                    deposit_lifecycle.BEFORE_EXPIRED_TEXT,
                    (("retry_yes", LABEL_RETRY_YES), ("retry_menu", LABEL_RETRY_MENU)),
                )
            ],
            conversation_id=conversation.id,
        )
    except notify.DeliveryError as exc:
        logger.warning("payment_expired_notice_undeliverable", code=exc.code)


jobs.register_expiry_hook(on_before_charge_expired)

__all__ = [
    "is_wait_over",
    "leave_wait",
    "on_before_charge_expired",
    "waiting_deposit",
]
