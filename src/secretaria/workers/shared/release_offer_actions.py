"""release_offer_actions - the patient's taps on a released PAID slot (TASK-052).

Owner decisions 2026-10-12 (3rd round). Channel-neutral: WhatsApp and the Portal reach
`handle_release_offer_tap` through `actions._handle_action_button`, which calls it first for the
`rel*|<released appointment id>` family. Before answering it applies the deadlines that may have
passed since the last sweep (B, E, and the clinic's 24 h to decide "Manter") and records the
patient's first contact.

* "Manter horário" only ASKS the clinic (`request_keep`): the clinic accepts or refuses in the
  TASK-054 list. After S it answers "O horário da consulta já passou" with Remarcar / Cancelar
  (a NEW WhatsApp message; the Portal also shows that card when the patient opens the
  conversation after S - `offer_card_after_start`).
* "Remarcar" at any time while the offer is open: carries the deposit, or charges again when the
  clinic already marked it refunded. After E ("cancelada por falta de resposta") nothing reopens.
* "Cancelar" always tells the clinic at once (unless it already marked the refund).
* "Recebido" / "Não recebido" after the clinic marked the refund.

Returns None when answered here, or `(action, prefix)` for the caller: ("rebooksame", line above
the day list) enters "Outro horário"; ("rebookno", None) asks why. A tap whose offer is missing,
belongs to another patient or another clinic is answered like an unknown appointment.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, Patient, PixDeposit, Tenant
from secretaria.models.deposit_release_offer import (
    DECISION_CANCELLED,
    DECISION_KEPT,
    DECISION_NO_ANSWER,
    DECISION_NOT_OFFERED,
    KEEP_FROM,
    KEEP_OUTCOME_TIMEOUT,
    DepositReleaseOffer,
)
from secretaria.services import release_offer as ro, release_offer_dispatch
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.payments.money import format_brl
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.sender import _reply_sender

logger = get_logger(__name__)

_TERMINAL = (DECISION_KEPT, "rebooked", DECISION_NO_ANSWER, DECISION_NOT_OFFERED)


@dataclass
class _Answer:
    text: str | None = None
    buttons: list[tuple[str, str]] | None = None
    follow: tuple[str, str | None] | None = None
    dispatch: bool = False


async def handle_release_offer_tap(
    reply: _ReplyContext, action: str, appointment_id: UUID
) -> tuple[str, str | None] | None:
    if reply.conversation_id is None:
        return None
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return None
        timezone, tenant_id, conversation_id = tenant.timezone, tenant.id, conversation.id
        client = _reply_sender(reply, tenant, await get_waba_token(session, tenant.id))
        offer = await ro.find_offer(
            session, tenant_id=tenant_id, released_appointment_id=appointment_id
        )
        if (
            offer is None
            or not offer.deposit_paid
            or offer.patient_id is None
            or offer.patient_id != conversation.patient_id
        ):
            if client is not None:
                await client.send_text_message(to=reply.patient_ref, body=ro.NOT_FOUND_TEXT)
            return None
        offer_id = offer.id
        now = datetime.now(UTC)
        settled = await ro.settle_if_expired(session, offer=offer, now=now)
        settled = await ro.close_if_no_answer(session, offer=offer, now=now) or settled
        settled = (
            await ro.decline_keep(session, offer=offer, outcome=KEEP_OUTCOME_TIMEOUT, now=now)
            or settled
        )
        offer = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
        if action in ro.OFFER_ACTIONS and offer.decision not in _TERMINAL:
            await ro.record_contact(session, offer_id=offer_id, now=now)
        await session.commit()

        if action == ro.ACTION_KEEP:
            answer = await _on_keep(session, offer, now, timezone)
        elif action == ro.ACTION_RESCHEDULE:
            answer = await _on_reschedule(session, offer, conversation_id, now)
        elif action == ro.ACTION_CANCEL:
            answer = await _on_cancel(session, offer, now)
        elif action == ro.ACTION_RECEIVED:
            answer = await _on_received(session, offer, now)
        else:
            answer = await _on_not_received(session, offer, now)
        answer.dispatch = answer.dispatch or settled

        if client is not None and answer.text:
            if answer.buttons:
                await client.send_buttons(
                    to=reply.patient_ref, body=answer.text, buttons=answer.buttons
                )
            else:
                await client.send_text_message(to=reply.patient_ref, body=answer.text)
        logger.info(
            "deposit_release_offer_tap",
            tenant_id=str(tenant_id),
            offer_id=str(offer_id),
            action=action,
        )

    # After the session closed (never two sessions open on one engine).
    if answer.dispatch:
        try:
            async with async_session_factory() as session:
                await release_offer_dispatch.dispatch_due(session, offer_id=offer_id)
        except Exception as exc:  # noqa: BLE001 - the sweep retries what did not go now
            logger.warning("deposit_release_offer_dispatch_failed", error_type=type(exc).__name__)
    return answer.follow


def _closed_answer(offer: DepositReleaseOffer, action: str) -> _Answer:
    """Kept / rebooked / cancelled-for-no-answer: what each button says."""
    if offer.decision == DECISION_NO_ANSWER:
        return _Answer(text=ro.OFFER_CLOSED_NO_ANSWER_TEXT)
    if action == ro.ACTION_CANCEL:
        return _Answer(text=ro.TAP_CANCEL_AFTER_KEEP_TEXT)
    return _Answer(text=ro.TAP_ALREADY_REBOOKED_TEXT)


async def _on_keep(session, offer, now, timezone) -> _Answer:
    fallback = ro.keep_fallback_buttons(offer.released_appointment_id)
    released = await session.get(Appointment, offer.released_appointment_id)
    when = (
        ro.slot_text(released.start_at, timezone)
        if released is not None and released.start_at is not None
        else ""
    )
    outcome = await ro.request_keep(session, offer=offer, now=now)
    await session.commit()
    if outcome == ro.KEEP_REQUESTED_NOW:
        return _Answer(text=ro.KEEP_REQUESTED_TEXT.format(when=when), dispatch=True)
    if outcome == ro.KEEP_ALREADY_REQUESTED:
        return _Answer(text=ro.KEEP_ALREADY_REQUESTED_TEXT)
    if outcome == ro.KEEP_AFTER_START:
        return _Answer(text=ro.AFTER_START_TEXT, buttons=fallback)
    if outcome == ro.KEEP_AFTER_REFUND:
        return _Answer(text=ro.KEEP_AFTER_REFUND_TEXT, buttons=fallback)
    if outcome == ro.KEEP_DECLINED_BEFORE:
        return _Answer(text=ro.KEEP_DECLINED_TAP_TEXT, buttons=fallback)
    current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    if current.decision == DECISION_CANCELLED:
        return _Answer(text=ro.TAP_KEEP_AFTER_CANCEL_TEXT)
    if current.decision == DECISION_KEPT:
        return _Answer(text=ro.TAP_ALREADY_KEPT_TEXT.format(when=when))
    return _closed_answer(current, ro.ACTION_KEEP)


async def _on_reschedule(session, offer, conversation_id, now) -> _Answer:
    outcome = await ro.choose_reschedule(
        session, offer=offer, conversation_id=conversation_id, now=now
    )
    await session.commit()
    if outcome == ro.RESCHEDULE_CARRYING:
        return _Answer(follow=("rebooksame", ro.RESCHEDULE_PREFIX))
    if outcome == ro.RESCHEDULE_NEW_CHARGE:
        return _Answer(follow=("rebooksame", ro.RESCHEDULE_NEW_CHARGE_PREFIX))
    current = await session.get(DepositReleaseOffer, offer.id, populate_existing=True)
    return _closed_answer(current, ro.ACTION_RESCHEDULE)


async def _on_cancel(session, offer, now) -> _Answer:
    offer_id, deposit_id = offer.id, offer.deposit_id
    outcome = await ro.choose_cancel(session, offer=offer, now=now)
    await session.commit()
    if outcome == ro.CANCEL_NOTIFIED:
        deposit = await session.get(PixDeposit, deposit_id)
        amount = format_brl(deposit.amount_cents) if deposit is not None else ""
        return _Answer(
            text=ro.CANCEL_DONE_TEXT.format(amount=amount),
            follow=("rebookno", None),
            dispatch=True,
        )
    if outcome == ro.CANCEL_AFTER_REFUND:
        return _Answer(text=ro.CANCEL_AFTER_MARKED_TEXT, follow=("rebookno", None))
    current = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
    if current.decision == DECISION_CANCELLED:
        return _Answer(text=ro.TAP_REFUND_ALREADY_TEXT)
    return _closed_answer(current, ro.ACTION_CANCEL)


async def _on_received(session, offer, now) -> _Answer:
    outcome = await ro.record_received(session, offer=offer, now=now)
    await session.commit()
    if outcome == "closed":
        return _Answer(text=ro.RECEIVED_TEXT)
    if outcome == "already_closed":
        return _Answer(text=ro.CHECK_CLOSED_TEXT)
    return _Answer(text=ro.CHECK_TOO_EARLY_TEXT)


async def _on_not_received(session, offer, now) -> _Answer:
    outcome = await ro.request_reminder(session, offer=offer, now=now)
    await session.commit()
    if outcome == "sent":
        return _Answer(text=ro.NOT_RECEIVED_TEXT, dispatch=True)
    if outcome == "recent":
        return _Answer(text=ro.NOT_RECEIVED_RECENT_TEXT)
    if outcome == "closed":
        return _Answer(text=ro.CHECK_CLOSED_TEXT)
    return _Answer(text=ro.CHECK_TOO_EARLY_TEXT)


async def offer_card_after_start(
    tenant_id: UUID, external_id: str, *, now: datetime | None = None
) -> bool:
    """Portal entry after S with the offer still unanswered: ONE card with Remarcar / Cancelar."""
    now = now or datetime.now(UTC)
    async with async_session_factory() as session:
        patient = await session.scalar(
            select(Patient).where(
                Patient.tenant_id == tenant_id,
                Patient.channel == CHANNEL_BRAIN_MESSAGE,
                Patient.external_id == external_id,
            )
        )
        if patient is None:
            return False
        offer = await session.scalar(
            select(DepositReleaseOffer)
            .where(
                DepositReleaseOffer.tenant_id == tenant_id,
                DepositReleaseOffer.patient_id == patient.id,
                DepositReleaseOffer.deposit_paid.is_(True),
                DepositReleaseOffer.decision.in_(KEEP_FROM),
                DepositReleaseOffer.slot_start_at <= now,
                DepositReleaseOffer.expires_at > now,
                DepositReleaseOffer.after_start_card_at.is_(None),
            )
            .order_by(DepositReleaseOffer.released_at.desc())
            .limit(1)
        )
        if offer is None or offer.conversation_id is None:
            return False
        conversation_id, released_id = offer.conversation_id, offer.released_appointment_id
        claimed = await session.execute(
            update(DepositReleaseOffer)
            .where(
                DepositReleaseOffer.id == offer.id,
                DepositReleaseOffer.after_start_card_at.is_(None),
            )
            .values(after_start_card_at=now)
            .execution_options(synchronize_session=False)
        )
        await session.commit()
        if claimed.rowcount != 1:
            return False
        sender = BrainMessageSender(conversation_id=conversation_id, session=session)
        await sender.send_buttons(
            to=external_id,
            body=ro.AFTER_START_TEXT,
            buttons=ro.keep_fallback_buttons(released_id),
        )
        await session.commit()
    logger.info("deposit_release_offer_after_start_card", tenant_id=str(tenant_id))
    return True
