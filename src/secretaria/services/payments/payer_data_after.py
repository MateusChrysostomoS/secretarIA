"""`after` timing: the consultation already exists; ask for what the provider needs, then resume.

`plugins/pix_deposit.py` runs off the hot path, right after the booking committed. When the
provider says it needs a payer field (`PayerDataNeeded`, only raised with `collect_payer_data=True`)
the patient is asked, with the same copy, the same redaction and the same time limits as in the
`before` timing, and the conversation waits in `FlowState.AWAITING_PAYER_DATA` with
`flow_step = "payer_data:after:<field>:<invalid answers>"`. The consultation stands the whole time:
the only thing waiting is its Pix.

The consultation to resume is not written in the conversation (a column would be a migration);
it is an id in a Redis key with the same 15-minute life as the answer
(`payer_data.remember_pending`). Every failure to ask is the behaviour the `after` timing always
had: the Pix is skipped, the reason is logged, the consultation stands.
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, FlowState, Patient, Tenant
from secretaria.services.payments import deposit_lifecycle, notify, payer_data

logger = get_logger(__name__)

# The states a chat may be in for the question to take it over: nothing else is waiting for the
# patient. A chat in the middle of another flow (a booking, a code, a cancellation) is left alone
# and the Pix is skipped, as it always was, rather than hijacking a turn.
_PARKABLE = (FlowState.IDLE, FlowState.LLM)


async def _leave(session: AsyncSession, conversation: Conversation, redis, tenant_id: UUID) -> None:
    conversation.flow_state = FlowState.IDLE
    conversation.flow_step = None
    await session.commit()
    await payer_data.discard(redis, tenant_id=tenant_id, conversation_id=conversation.id)


async def ask_after_booking(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient: Patient,
    appointment: Appointment,
    kind: payer_data.Kind,
    provider: str | None,
    waba_token: str | None,
    redis,
    conversation_id: UUID | None,
    whatsapp_cls=None,
) -> bool:
    """Ask the patient for `kind` and arm the wait. True when the question went out; False when it
    could not be asked (no Redis, no conversation, the chat is busy, the message was not
    delivered): the Pix is then skipped and the consultation stands."""
    if redis is None or conversation_id is None:
        logger.warning("payer_data_after_not_asked", reason="no_redis_or_conversation")
        return False
    conversation = await session.get(Conversation, conversation_id)
    if conversation is None or conversation.tenant_id != tenant.id:
        logger.warning("payer_data_after_not_asked", reason="no_conversation")
        return False
    if conversation.flow_state not in _PARKABLE:
        logger.warning(
            "payer_data_after_not_asked",
            reason="chat_busy",
            flow_state=str(conversation.flow_state),
        )
        return False
    armed = await payer_data.remember_pending(
        redis,
        tenant_id=tenant.id,
        conversation_id=conversation_id,
        appointment_id=appointment.id,
    )
    if not armed:
        return False
    conversation.flow_state = FlowState.AWAITING_PAYER_DATA
    conversation.flow_step = payer_data.step_for("after", kind, 0)
    await session.commit()
    question = notify.Text(payer_data.ask_text(kind, provider))
    try:
        await notify.deliver(
            session,
            tenant,
            patient,
            [question],
            conversation_id=conversation_id,
            waba_token=waba_token,
            whatsapp_cls=whatsapp_cls,
        )
    except notify.DeliveryError as exc:
        logger.warning("payer_data_after_not_delivered", code=exc.code)
        await _leave(session, conversation, redis, tenant.id)
        return False
    return True


async def resume_after_booking(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient: Patient,
    conversation_id: UUID,
    waba_token: str | None,
    redis,
    whatsapp_cls=None,
) -> str:
    """The answer was stored: create and send the Pix of the consultation that was waiting.

    Returns "created" (the Pix was sent), "asked_again" (the provider needs the OTHER field too and
    the patient was asked for it), "nothing_pending" (the wait expired: nothing to resume) or
    "failed" (the provider or the delivery refused: the caller tells the patient and calls a
    human). Whatever was kept is forgotten in every case but "asked_again"."""
    appointment_id = await payer_data.recall_pending(
        redis, tenant_id=tenant.id, conversation_id=conversation_id
    )
    appointment = await session.get(Appointment, appointment_id) if appointment_id else None
    if appointment is None or appointment.tenant_id != tenant.id:
        await payer_data.discard(redis, tenant_id=tenant.id, conversation_id=conversation_id)
        return "nothing_pending"
    try:
        deposit = await deposit_lifecycle.maybe_create_deposit(
            session,
            tenant=tenant,
            patient=patient,
            appointment=appointment,
            waba_token=waba_token,
            collect_payer_data=True,
            redis=redis,
            conversation_id=conversation_id,
        )
        await session.commit()
    except payer_data.PayerDataNeeded as need:
        await session.rollback()
        # The rollback expires every loaded instance and the question below reads them (an async
        # session cannot lazy-load): reload the three it uses. Nothing was written before the raise.
        for instance in (tenant, patient, appointment):
            await session.refresh(instance)
        asked = await ask_after_booking(
            session,
            tenant=tenant,
            patient=patient,
            appointment=appointment,
            kind=need.kind,
            provider=need.provider,
            waba_token=waba_token,
            redis=redis,
            conversation_id=conversation_id,
            whatsapp_cls=whatsapp_cls,
        )
        if asked:
            return "asked_again"
        deposit = None
    await payer_data.discard(redis, tenant_id=tenant.id, conversation_id=conversation_id)
    return "created" if deposit is not None else "failed"
