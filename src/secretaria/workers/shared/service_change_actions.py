"""service_change_actions - the patient side of a change on a paid sinal (2026-10-26; TASK-085 R).

Spec: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md and
tasks/TASK-085/sinal-trocas/contrato.md. Channel-neutral: WhatsApp and the Portal both reach
this through `flow_runner._apply_flow_result` (the reply to a confirmed edit) and
`actions._handle_action_button` (a tap on the refund question or on the three choice buttons).

* `service_change_bubbles` - after the commit, the edit's reply follows what the money decided
  (`service_change.request_paid_change`): `blocked` -> one line, nothing changed;
  `suspended` (TASK-085 R D-R4/D-R10) -> the value of the difference, the old consultation off
  the agenda and the new time reserved until the deadline, and the Pix copia-e-cola at the end of
  the SAME message; `awaiting_payment` (the
  legacy deferred mode) -> the same without the reservation promise; `applied_awaiting_payment`
  (D-R5) -> the "Pronto" recap, then "já foi alterada; falta o Pix da diferença" with the code;
  `asked` -> the "Pronto" recap and the refund question with two fixed ACTION buttons;
  `applied_now` -> the recap as usual.
* `handle_service_diff_tap` - "Sim, quero de volta" / "Não, abater no valor": honoured only for
  the patient of that sinal, in the tapping conversation's clinic; never the LLM, never money
  moved by us (a "sim" opens an attention item - the clinic returns it in the provider's panel).
* `handle_service_diff_choice` (TASK-085 R D-R4/D-R7) - "Manter a antiga" / "Reenviar o Pix" /
  "Cancelar ambas" after the Pix of a suspended change expired. Same scope as the refund tap.
  3rd round (D-R10/D-R11): the old consultation left the agenda meanwhile, so "Manter a antiga"
  puts it back only if its slot is still free (`old_slot.restore_old_slot`), and "Outro horário"
  opens the patient's reschedule flow on it (`flow_router._manage_reschedule` rebooks it and
  `flow_runner` closes the change with `old_slot.finish_rebook`).

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
from secretaria.services.flow_router import ActionButtonsBubble, enter_manage_action
from secretaria.services.patient_context import load_upcoming_appointments
from secretaria.services.payments import old_slot, service_change
from secretaria.services.payments.service_change import ChoiceOutcome, ServiceChangeResult
from secretaria.services.service_catalog import load_service_catalog
from secretaria.services.tenant_config import (
    get_waba_token,
    list_active_professionals,
    load_tenant_config,
)
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)
from secretaria.workers.shared.patient_roster import build_patient_roster
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
        # suspended change (D-R4/D-R10) reserves the new time; the legacy deferred mode does not.
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
    if outcome.kind == "slot_taken" and outcome.adjustment_id is not None:
        # D-R11 / mapa-u §7.3: "Manter a antiga" found the old slot taken - nothing changed;
        # "Outro horário" replaces "Manter a antiga" on the buttons sent again.
        return answer, service_change.choice_buttons(outcome.adjustment_id, old_slot_free=False)
    if outcome.kind == "restore_failed" and outcome.adjustment_id is not None:
        # The agenda did not answer: nothing changed, the same three choices again.
        return answer, service_change.choice_buttons(outcome.adjustment_id)
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
    """TASK-085 R (D-R4): a tap on "Manter a antiga" / "Reenviar o Pix" / "Cancelar ambas" /
    "Outro horário" (`service_change.CHOICE_ACTIONS`). Honoured only for the patient of that
    sinal, in the tapping conversation's clinic (as `handle_service_diff_tap`).

    * "Manter a antiga" (3rd round, D-R10/D-R11) -> `_handle_keep`: `old_slot.restore_old_slot(
      trigger="tap")`, which commits itself;
    * "Outro horário" (D-R11) -> `_handle_other`: `choose_after_expiry(choice="other")`, commit,
      then the patient's reschedule flow on the old consultation;
    * the others run `service_change.choose_after_expiry`, commit, then reply ONE message
      (`choice_answer_text`, + the new Pix on "resent", the buttons again - without "Reenviar"
      on "no_window" - on "no_window"/"resend_failed") and, on "cancel_old", cancel the old
      consultation as the patient's own cancellation in the SAME transaction as the choice
      (never a window where the change is undone but the old visit not cancelled)."""
    if reply.conversation_id is None or action not in service_change.CHOICE_ACTIONS:
        return
    choice = service_change.CHOICE_OF_ACTION.get(action)
    if choice == service_change.CHOICE_KEEP:
        # TASK-085 R 3rd round (D-R10/D-R11): the old consultation left the agenda - "Manter a
        # antiga" puts it back only when its slot is still free (`old_slot.restore_old_slot`).
        await _handle_keep(reply, adjustment_id)
        return
    if choice == getattr(service_change, "CHOICE_OTHER", "other"):
        # D-R11: "Outro horário" - the patient picks a new slot for the old consultation.
        await _handle_other(reply, adjustment_id, redis=redis)
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


# --------------------------------------------------------------------------
# TASK-085 R 3rd round (D-R10/D-R11): the old consultation left the agenda while the patient
# waited - "Manter a antiga" puts it back if its slot is still free; "Outro horário" moves it.
# --------------------------------------------------------------------------

# `old_slot.restore_old_slot` -> the `ChoiceOutcome.kind` the patient is answered with.
_KEEP_KIND = {
    "restored": "kept_old",
    "slot_taken": "slot_taken",
    "calendar_unavailable": "restore_failed",
    "already_decided": "already_decided",
    "not_applicable": "already_decided",
}


def _outcome_base(adjustment: DepositAdjustment) -> dict:
    """The plain values of a tapped change, read BEFORE anything commits."""
    return {
        "adjustment_id": adjustment.id,
        "appointment_id": adjustment.appointment_id,
        "diff_cents": adjustment.diff_cents,
        "old_service": adjustment.old_service,
        "new_service": adjustment.new_service,
    }


async def _send_choice_reply(client, reply: _ReplyContext, outcome: ChoiceOutcome, tenant) -> None:
    body, buttons = _choice_reply(outcome, tenant, None)
    if buttons:
        await client.send_buttons(reply.patient_ref, body, buttons)
    else:
        await client.send_text_message(to=reply.patient_ref, body=body)


async def _handle_keep(reply: _ReplyContext, adjustment_id: UUID) -> None:
    """"Manter a antiga" (D-R11, mapa-u §1.2/§7.3): `old_slot.restore_old_slot(trigger="tap")`
    (COMMITS; called with a clean session). Restored -> "Sua consulta antiga voltou" +
    `followup_kept_old` (its reminders are planned again); the slot was taken meanwhile ->
    "slot_taken" with "Outro horário" / "Reenviar o Pix" / "Cancelar ambas", nothing changed; the
    agenda did not answer -> "restore_failed" with the same three choices, nothing changed."""
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        tenant_id = tenant.id
        client = _reply_sender(reply, tenant, await get_waba_token(session, tenant_id))
        if client is None:
            logger.error("service_diff_choice_no_sender", tenant_id=str(tenant_id))
            return
        adjustment = await _owned_adjustment(session, tenant_id, conversation, adjustment_id)
        if adjustment is None:
            outcome = ChoiceOutcome(kind="not_found")
        else:
            base = _outcome_base(adjustment)
            await session.commit()  # end the reads: the restore needs a clean session
            restored = await old_slot.restore_old_slot(
                session, tenant_id=tenant_id, adjustment_id=adjustment.id, now=now, trigger="tap"
            )
            outcome = ChoiceOutcome(kind=_KEEP_KIND.get(restored, "already_decided"), **base)
        await _send_choice_reply(client, reply, outcome, tenant)
    logger.info(
        "service_diff_choice_tap",
        tenant_id=str(tenant_id),
        adjustment_id=str(adjustment_id),
        outcome=outcome.kind,
    )
    if outcome.kind == "kept_old" and outcome.appointment_id is not None:
        await service_change.followup_kept_old(tenant_id, outcome.appointment_id)


async def _rebook_entry(session, tenant: Tenant, patient_id: UUID, appointment_id: UUID):
    """What the reschedule flow needs to open on the old consultation (the `apptresched` shape):
    the consultation as the manage flow lists it - `load_upcoming_appointments(rebooking_id=)`
    returns it although it is suspended, marked `rebook_adjustment_id` - the router's roster
    (catalog and units resolved, so the day picker offers THAT unit's hours) and its OWN agenda.
    None when the consultation is not rebookable any more."""
    appointments = await load_upcoming_appointments(
        session, tenant.id, patient_id, rebooking_id=appointment_id
    )
    target = next((appt for appt in appointments if appt.get("id") == str(appointment_id)), None)
    if target is None or not target.get("rebook_adjustment_id"):
        return None
    professional_rows = await list_active_professionals(session, tenant.id)
    # TASK-090 Adendo 1 (I1): the patient roster - a hidden doctor's EXISTING consultation
    # keeps his units (incomplete ones too) and so his unit hours and blocked days.
    professionals = await build_patient_roster(
        session, tenant, professional_rows, await load_service_catalog(session, tenant.id)
    )
    calendar = await _appointment_calendar(
        session, tenant, _appointment_calendar_target(target, professional_rows)
    )
    return appointments, professionals, calendar


async def _handle_other(reply: _ReplyContext, adjustment_id: UUID, redis=None) -> None:
    """"Outro horário" (D-R11, mapa-u §1.5): `choose_after_expiry(choice="other")` validates the
    change (REBOOKABLE_STATUSES) and releases the abandoned new window -> commit -> the patient's
    reschedule flow on the OLD consultation (`enter_manage_action("reschedule", ...,
    preselected_id=)`, its own agenda): same service, doctor, unit and convênio, only the day and
    time change. The reschedule limit is neither checked nor used (mapa-u §7.6). The answer
    ("Certo! Escolha o novo dia...") goes on top of the day picker, in the same reply."""
    now = datetime.now(UTC)
    entry = None
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
                choice=getattr(service_change, "CHOICE_OTHER", "other"),
                now=now,
            )
            await session.commit()
            if outcome.kind == "rebook" and outcome.appointment_id is not None:
                entry = await _rebook_entry(
                    session, tenant, conversation.patient_id, outcome.appointment_id
                )
        if entry is None:
            if outcome.kind == "rebook":
                # Decided elsewhere between the tap and the read (a payment, the clinic).
                outcome = ChoiceOutcome(kind="already_decided", adjustment_id=adjustment_id)
            await _send_choice_reply(client, reply, outcome, tenant)
    logger.info(
        "service_diff_choice_tap",
        tenant_id=str(tenant_id),
        adjustment_id=str(adjustment_id),
        outcome=outcome.kind,
    )
    if entry is None:
        return
    appointments, professionals, calendar = entry
    result = await enter_manage_action(
        "reschedule",
        tenant,
        appointments,
        professionals,
        preselected_id=outcome.appointment_id,
        calendar=calendar,
    )
    if result.action == "reply":
        result.bubbles = [
            TextBubble(body=service_change.choice_answer_text(outcome)),
            *result.bubbles,
        ]
    # Lazy: flow_runner imports this module (the edit's reply).
    from secretaria.workers.shared.flow_runner import _apply_flow_result

    await _apply_flow_result(
        reply, result, reply.patient_ref, redis=redis, tenant=tenant, waba_token=waba_token
    )
