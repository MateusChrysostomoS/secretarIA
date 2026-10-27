"""deposit_request - the `before` timing Pix request and its voids (TASK-017, ported by TASK-050).

Written in the old monolithic workers/tasks.py by TASK-017 and moved here when the Pix work
was brought onto the split worker package (TASK-023). Channel-neutral: every send goes
through `_reply_sender` / `_send_plain_reply`, never a channel comparison.

  * `_send_deposit_request_notice` / `_abort_deposit_request` - sent from
    `shared/flow_runner.py::_apply_flow_result` when the router held a slot for a deposit.
  * `_void_waiting_deposit` - `/menu` (and the agent's `show_main_menu`) while a Pix is open.
  * `_arq_enqueue` / `_retry_buttons` - small helpers shared with `shared/deposit_turn.py`.

Imports only `shared/` modules that never import `flow_runner` or `sentinels`, so both of
those may import this module without a cycle.
"""

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Conversation, Tenant
from secretaria.models.conversation import FlowState
from secretaria.services import deposit_wait
from secretaria.services.channel_sender import sender_persists_outbound
from secretaria.services.flow_router import (
    LABEL_RETRY_MENU,
    LABEL_RETRY_YES,
    WHEN_FORMAT,
)
from secretaria.services.payments import booking_charge, deposit_lifecycle
from secretaria.services.whatsapp import interactive_buttons_record
from secretaria.workers.shared.booking_hold import _tenant_tzinfo
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.dispatch import _send_plain_reply
from secretaria.workers.shared.handover import _set_conversation_human_active
from secretaria.workers.shared.sender import _record_outbound, _reply_sender
from secretaria.workers.shared.state_expiry import _write_flow_state

logger = get_logger(__name__)


async def _send_deposit_request_notice(
    reply: _ReplyContext,
    request: dict,
    *,
    tenant: Tenant,
    waba_token: str | None,
) -> bool:
    """Ask the patient to pay, on this turn's channel: what it is for with the Pix code at the
    end of the SAME message (fenced: monospace on WhatsApp, a copy box in the
    Portal) and the card that keeps the conversation while it is open.

    Unlike `_send_plain_reply` this REPORTS failure: in `before` timing a Pix nobody received
    must not keep the slot (`_abort_deposit_request`). Each message is retried once; the
    sender is the same channel-aware seam every other reply uses (`_reply_sender`), so a Portal
    patient with no phone gets it as Portal messages.
    """
    sender = _reply_sender(reply, tenant, waba_token)
    if sender is None:
        logger.error(
            "deposit_request_no_sender",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return False
    tz = _tenant_tzinfo(tenant)
    until_text = request["expires_at"].astimezone(tz).strftime("%H:%M")
    steps: list[tuple[str, str]] = [
        (
            "text",
            deposit_lifecycle.before_request_text(
                tenant.clinic_name,
                service=request["service"],
                when_text=request["start_at"].astimezone(tz).strftime(WHEN_FORMAT),
                amount_cents=request["amount_cents"],
                minutes=request["minutes"],
                until_text=until_text,
                payment_note=request.get("payment_note"),
                copy_paste=request["copy_paste"],
            ),
        ),
        ("buttons", deposit_lifecycle.before_wait_card_text(until_text)),
    ]
    for kind, body in steps:
        for attempt in (1, 2):
            try:
                if kind == "buttons":
                    buttons = list(deposit_lifecycle.DEPOSIT_WAIT_BUTTONS)
                    result = await sender.send_buttons(
                        to=reply.patient_ref, body=body, buttons=buttons
                    )
                    interactive = interactive_buttons_record(body, buttons)
                else:
                    result = await sender.send_text_message(to=reply.patient_ref, body=body)
                    interactive = None
            except Exception as exc:
                logger.warning(
                    "deposit_request_send_failed",
                    error_type=type(exc).__name__,
                    attempt=attempt,
                    conversation_id=str(reply.conversation_id),
                )
                if attempt == 2:
                    return False
                continue
            if reply.conversation_id is not None and not sender_persists_outbound(sender):
                await _record_outbound(reply.conversation_id, body, result, interactive)
            break
    logger.info("deposit_request_sent", conversation_id=str(reply.conversation_id))
    return True


async def _abort_deposit_request(
    reply: _ReplyContext,
    request: dict,
    *,
    tenant: Tenant,
    waba_token: str | None,
    redis,
) -> None:
    """The Pix could not be delivered: void the charge, free the slot and hand the conversation
    to a human (nothing was created, nothing is owed). No clinic e-mail (owner answer 13)."""
    try:
        async with async_session_factory() as session:
            await booking_charge.abandon_charge(session, tenant, request["deposit_id"])
    except Exception as exc:
        logger.error("deposit_request_abort_failed", error_type=type(exc).__name__)
    await _write_flow_state(reply.conversation_id, FlowState.IDLE)
    await _set_conversation_human_active(reply.conversation_id)
    await _send_plain_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=deposit_lifecycle.BEFORE_BLOCKED_TEXT,
        event="deposit_request_blocked_notice_sent",
    )


def _arq_enqueue(redis):
    """An `enqueue(job_name, argument)` over this worker's arq pool, or None without one."""
    if redis is None:
        return None

    async def enqueue(name: str, argument: str) -> None:
        await redis.enqueue_job(name, argument)

    return enqueue


def _retry_buttons() -> list[tuple[str, str]]:
    return [("retry_yes", LABEL_RETRY_YES), ("retry_menu", LABEL_RETRY_MENU)]


async def _void_waiting_deposit(reply: _ReplyContext, tenant: Tenant) -> None:
    """`/menu` (and the agent's `show_main_menu`) while a Pix is open: void it FIRST, so the slot
    the patient may still be paying for is never released while the Pix is payable. A payment
    the provider already has stays settled by the normal paid path. Best-effort."""
    if reply.conversation_id is None:
        return
    try:
        async with async_session_factory() as session:
            conversation = await session.get(Conversation, reply.conversation_id)
            if conversation is None or conversation.flow_state != FlowState.AWAITING_DEPOSIT:
                return
            current = await deposit_wait.current_deposit(session, conversation)
            if current is None or deposit_wait.wait_verdict(current) == "over":
                return
            outcome = await booking_charge.abandon_charge(session, tenant, current.id)
        logger.info("deposit_wait_menu_voided", outcome=outcome)
    except Exception as exc:
        logger.warning("deposit_wait_menu_void_failed", error_type=type(exc).__name__)
