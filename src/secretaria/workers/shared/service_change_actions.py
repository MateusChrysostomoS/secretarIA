"""service_change_actions - the patient side of a change on a paid sinal (2026-10-26; TASK-085 R).

Spec: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md and
tasks/TASK-085/sinal-trocas/contrato.md. Channel-neutral: WhatsApp and the Portal both reach
this through `flow_runner._apply_flow_result` (the reply to a confirmed edit) and
`actions._handle_action_button` (a tap on the refund question or on the three choice buttons).

* `service_change_bubbles` - after the commit, the edit's reply follows what the money decided
  (`service_change.request_paid_change`): `blocked` -> one line, nothing changed;
  `suspended` (TASK-085 R D-R4) -> the value of the difference, both times reserved until the
  deadline and the Pix copia-e-cola at the end of the SAME message; `awaiting_payment` (the
  legacy deferred mode) -> the same without the reservation promise; `applied_awaiting_payment`
  (D-R5) -> the "Pronto" recap, then "já foi alterada; falta o Pix da diferença" with the code;
  `asked` -> the "Pronto" recap and the refund question with two fixed ACTION buttons;
  `applied_now` -> the recap as usual.
* `handle_service_diff_tap` - "Sim, quero de volta" / "Não, abater no valor": honoured only for
  the patient of that sinal, in the tapping conversation's clinic; never the LLM, never money
  moved by us (a "sim" opens an attention item - the clinic returns it in the provider's panel).
* `handle_service_diff_choice` (TASK-085 R D-R4/D-R7) - "Manter a antiga" / "Reenviar o Pix" /
  "Cancelar ambas" after the Pix of a suspended change expired. Same scope as the refund tap.

One message per event: the paid/expired notices of a difference are the default hooks of
`service_change` (after the webhook / the sweep); nothing here repeats them.
"""

import math
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, PixDeposit, Tenant, is_live_status
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.services import reminder_hooks
from secretaria.services.flow_router import ActionButtonsBubble
from secretaria.services.payments import service_change
from secretaria.services.payments.service_change import ChoiceOutcome, ServiceChangeResult
from secretaria.services.tenant_config import get_waba_token, load_tenant_config
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.sender import _reply_sender

logger = get_logger(__name__)


def _ttl_minutes(tenant, expires_at: datetime | None, now: datetime | None = None) -> int:
    """The minutes the patient reads for a difference Pix: the clinic's TTL, or fewer when the
    code expires sooner (a suspended change's Pix is capped before the visits)."""
    ttl = (
        getattr(tenant, "deposit_hold_ttl_minutes", None)
        or service_change.DEFAULT_PIX_TTL_MINUTES
    )
    if expires_at is None:
        return ttl
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    left = math.ceil((expires_at - (now or datetime.now(UTC))).total_seconds() / 60)
    return max(1, min(ttl, left))


def service_change_bubbles(
    bubbles: list, outcome: ServiceChangeResult, *, new_service: str, tenant
) -> list:
    """The reply of a confirmed paid change. `bubbles` is the router's: the "Pronto" recap first,
    then the returning-patient menu. A change named by the engine's `outcome.label` (a unit,
    doctor or convênio change: "Consulta na Unidade Centro") is named so, not by the bare
    service."""
    label = getattr(outcome, "label", None)
    new_service = label or new_service
    recap, rest = (bubbles[0], list(bubbles[1:])) if bubbles else (None, [])
    if outcome.kind == "blocked":
        text = service_change.blocked_text(outcome.reason, unit_only=label is not None)
        return [TextBubble(body=text), *rest]
    if outcome.kind in ("awaiting_payment", "suspended"):
        # ONE message: the explanation with the (fenced) copia-e-cola at its end. Only a
        # suspended change (D-R4) promises both times reserved; the legacy deferred mode does not.
        head = service_change.difference_pix_text(
            new_service,
            outcome.diff_cents,
            outcome.pix_copy_paste,
            _ttl_minutes(tenant, getattr(outcome, "expires_at", None)),
            held_new_slot=outcome.kind == "suspended",
        )
        return [TextBubble(body=head), *rest]
    if outcome.kind == "applied_awaiting_payment":
        # D-R5: the change is already made (the recap says what it is now) + the Pix.
        pix = service_change.difference_pix_applied_text(
            new_service,
            outcome.diff_cents,
            outcome.pix_copy_paste,
            _ttl_minutes(tenant, getattr(outcome, "expires_at", None)),
        )
        return [*([recap] if recap is not None else []), TextBubble(body=pix), *rest]
    if outcome.kind == "asked" and outcome.adjustment_id is not None:
        question = ActionButtonsBubble(
            body=service_change.refund_question_text(new_service, outcome.diff_cents),
            buttons=service_change.refund_question_buttons(outcome.adjustment_id),
        )
        return [*([recap] if recap is not None else []), question]
    if outcome.kind == "applied_now" and outcome.decision == service_change.DECISION_COLLECT:
        # Follow-up B: no Pix could be created - the change stands; the clinic collects.
        note = TextBubble(body=service_change.collect_due_text(new_service, outcome.diff_cents))
        return [*([recap] if recap is not None else []), note, *rest]
    return list(bubbles)


async def _owned_adjustment(session, tenant_id: UUID, conversation, adjustment_id: UUID):
    """The adjustment, only when it is this clinic's AND its sinal is the tapping patient's."""
    adjustment = await session.scalar(
        select(DepositAdjustment).where(
            DepositAdjustment.id == adjustment_id, DepositAdjustment.tenant_id == tenant_id
        )
    )
    deposit = (
        await session.get(PixDeposit, adjustment.deposit_id) if adjustment is not None else None
    )
    if (
        adjustment is None
        or deposit is None
        or deposit.tenant_id != tenant_id
        or deposit.patient_id is None
        or deposit.patient_id != conversation.patient_id
    ):
        return None
    return adjustment


async def handle_service_diff_tap(reply: _ReplyContext, action: str, adjustment_id: UUID) -> None:
    """A tap on the refund question. Answers with ONE text; idempotent (a second tap says the
    question was already answered). A tap on someone else's question changes nothing."""
    if reply.conversation_id is None:
        return
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        tenant_id = tenant.id
        client = _reply_sender(reply, tenant, await get_waba_token(session, tenant_id))
        if client is None:
            logger.error("service_diff_tap_no_sender", tenant_id=str(tenant_id))
            return
        adjustment = await _owned_adjustment(session, tenant_id, conversation, adjustment_id)
        if adjustment is None:
            outcome, diff_cents = "not_found", 0
        else:
            diff_cents = adjustment.diff_cents
            outcome = await service_change.answer_refund_question(
                session,
                tenant_id=tenant_id,
                adjustment_id=adjustment.id,
                accept=action == service_change.ACTION_REFUND_YES,
                now=datetime.now(UTC),
            )
            await session.commit()
        await client.send_text_message(
            to=reply.patient_ref, body=service_change.refund_answer_text(outcome, diff_cents)
        )
    logger.info(
        "service_diff_refund_tap",
        tenant_id=str(tenant_id),
        adjustment_id=str(adjustment_id),
        outcome=outcome,
    )


async def _cancel_old_consultation(
    session, tenant, appointment_id: UUID, patient_id, waba_token
) -> tuple[str | None, bool]:
    """"Cancelar ambas" (OQ-14): the old consultation is cancelled as the PATIENT's own
    cancellation - Calendar, status and the paid sinal's cancellation rules
    (`actions._execute_appointment_cancel`). Returns (its text, whether it was live)."""
    # Lazy: actions.py routes the taps to this module (import cycle otherwise).
    from secretaria.workers.shared.actions import _execute_appointment_cancel

    appointment = await session.scalar(
        select(Appointment).where(
            Appointment.id == appointment_id,
            Appointment.tenant_id == tenant.id,
            Appointment.patient_id == patient_id,
        )
    )
    if appointment is None or not is_live_status(appointment.status):
        return None, False
    tenant_config = await load_tenant_config(session, tenant)
    text = await _execute_appointment_cancel(
        session, tenant, tenant_config, appointment, waba_token
    )
    return text, True


def _choice_reply(outcome: ChoiceOutcome, tenant, cancel_text: str | None):
    """(body, buttons or None): the ONE message answering a tap on the choice."""
    answer = service_change.choice_answer_text(outcome)
    if outcome.kind == "resent":
        pix = service_change.difference_pix_text(
            outcome.new_service or "",
            outcome.diff_cents,
            outcome.pix_copy_paste,
            _ttl_minutes(tenant, getattr(outcome, "expires_at", None)),
            held_new_slot=True,
        )
        return f"{answer}\n\n{pix}", None
    if outcome.kind == "cancel_old" and cancel_text:
        return f"{answer}\n\n{cancel_text}", None
    if outcome.kind in ("no_window", "resend_failed") and outcome.adjustment_id is not None:
        # Nothing changed: the choice is still open - the buttons again ("Reenviar" only when it
        # may still work).
        buttons = service_change.choice_buttons(
            outcome.adjustment_id, resend=outcome.kind == "resend_failed"
        )
        return answer, buttons
    return answer, None


async def handle_service_diff_choice(
    reply: _ReplyContext, action: str, adjustment_id: UUID, redis=None
) -> None:
    """TASK-085 R (D-R4): a tap on "Manter a antiga" / "Reenviar o Pix" / "Cancelar ambas"
    (`service_change.CHOICE_ACTIONS`). Honoured only for the patient of that sinal, in the
    tapping conversation's clinic (as `handle_service_diff_tap`); runs
    `service_change.choose_after_expiry`, commits, then replies ONE message
    (`choice_answer_text`, + the new Pix on "resent", the buttons again - without "Reenviar" on
    "no_window" - on "no_window"/"resend_failed"), runs `followup_kept_old` on "kept_old" and,
    on "cancel_old", cancels the old consultation as the patient's own cancellation in the SAME
    transaction as the choice (never a window where the change is undone but the old visit not
    cancelled)."""
    if reply.conversation_id is None or action not in service_change.CHOICE_ACTIONS:
        return
    now = datetime.now(UTC)
    cancel_text: str | None = None
    was_live = False
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        tenant_id = tenant.id
        waba_token = await get_waba_token(session, tenant_id)
        client = _reply_sender(reply, tenant, waba_token)
        if client is None:
            logger.error("service_diff_choice_no_sender", tenant_id=str(tenant_id))
            return
        adjustment = await _owned_adjustment(session, tenant_id, conversation, adjustment_id)
        if adjustment is None:
            outcome = ChoiceOutcome(kind="not_found")
        else:
            outcome = await service_change.choose_after_expiry(
                session,
                tenant_id=tenant_id,
                adjustment_id=adjustment.id,
                choice=service_change.CHOICE_OF_ACTION[action],
                now=now,
            )
            if outcome.kind == "cancel_old" and outcome.appointment_id is not None:
                cancel_text, was_live = await _cancel_old_consultation(
                    session, tenant, outcome.appointment_id, conversation.patient_id, waba_token
                )
            await session.commit()
        body, buttons = _choice_reply(outcome, tenant, cancel_text)
        if buttons:
            await client.send_buttons(reply.patient_ref, body, buttons)
        else:
            await client.send_text_message(to=reply.patient_ref, body=body)
    logger.info(
        "service_diff_choice_tap",
        tenant_id=str(tenant_id),
        adjustment_id=str(adjustment_id),
        outcome=outcome.kind,
    )
    if outcome.kind == "kept_old" and outcome.appointment_id is not None:
        await service_change.followup_kept_old(tenant_id, outcome.appointment_id)
    if outcome.kind == "cancel_old" and was_live and outcome.appointment_id is not None:
        from secretaria.workers.shared.actions import _announce_button_cancel

        if reminder_hooks.enabled_for(tenant):
            await reminder_hooks.after_appointment_closed(
                outcome.appointment_id, reason="cancelled"
            )
        await _announce_button_cancel(redis, tenant, outcome.appointment_id, was_live)
