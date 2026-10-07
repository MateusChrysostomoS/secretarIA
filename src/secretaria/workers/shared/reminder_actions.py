"""A tap on a reminder button (TASK-032 R2 + R3).

The id after the bar is an `appointment_reminders` ROW id. The older
`apptconfirm|<appointment_id>` family can only be checked against the TENANT;
the row names the patient too, so a tap is honoured only when the row's tenant
AND patient are the tapping conversation's. Anything else gets the same polite
miss, whichever check failed - never reveal whose row it is.

R2 (the reminder itself): Confirmar / Cancelar / Outro.
R3 (spec §4.3, the "Cancelar" path), every step on the same row id:
    Cancelar           -> "O que você prefere?": Remarcar Consulta / Agendar Outra /
                          Cancelar Consulta
    Remarcar Consulta  -> the existing reschedule of THIS appointment (Pix limits apply)
    Agendar Outra      -> a new booking that replaces this one only once confirmed
    Cancelar Consulta  -> "Tem certeza?" (Pix retention line inside the refund window)
                          -> Sim, cancelar -> cancel + "por quê?" / Manter consulta
    Outro              -> the conversation goes to the AI (TASK-030's hand-backs bring
                          it back to the flow)

Three of those steps need the appointment-scoped machinery in
`workers/shared/actions.py` (reschedule entry with the Pix limit, the cancel,
the booking entry). This module validates the row and returns a CONTINUATION
`(action, appointment_id)`; `_handle_action_button` then runs its own branch for
it. The continuation names are not button prefixes, so nobody can send one.

Channel-neutral: replies go through `_reply_sender`, so a Portal tap
(workers/portal/inbound.py) is answered in the Portal. Runs before the
handover/flow/LLM gates, like every action button (workers/turn_router.py).
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentReminder,
    Conversation,
    FlowState,
    PixDepositStatus,
    Tenant,
    is_live_status,
)
from secretaria.models.appointment_reminder import (
    REMINDER_ANSWER_CANCEL,
    REMINDER_ANSWER_CONFIRM,
    REMINDER_ANSWER_OTHER,
    REMINDER_KIND_CHAT,
    REMINDER_STATUS_CANCELLED,
)
from secretaria.services import reminder_schedule
from secretaria.services.flow_router import FlowRouterResult
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.reminder_text import (
    ACTION_BOOK_ANOTHER,
    ACTION_CANCEL,
    ACTION_CONFIRM,
    ACTION_GIVE_UP,
    ACTION_GIVE_UP_CONFIRM,
    ACTION_KEEP,
    ACTION_OTHER,
    ACTION_RESCHEDULE_THIS,
    REMINDER_ROW_ACTIONS,
    cancel_path_buttons,
    give_up_confirm_buttons,
)
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.deposit import _hours_until_start, _pix_retention_warning_line
from secretaria.workers.shared.flow_runner import _apply_flow_result
from secretaria.workers.shared.greeting import _format_appointment_when
from secretaria.workers.shared.sender import _reply_sender
from secretaria.workers.shared.text import _as_utc

logger = get_logger(__name__)

NOT_FOUND_TEXT = "Não encontrei essa consulta."
NOT_ACTIVE_TEXT = "Essa consulta não está mais ativa."
CONFIRMED_TEXT = "Presença confirmada! ✅ Até {when}."
MOVED_TEXT = "Essa mensagem era sobre um horário antigo. Sua consulta agora é em {when}."
# The buttons are 20 characters at most (WhatsApp), so "Agendar Outra" is the short
# form of the owner's "Agendar Outra Consulta"; the body says it in full.
CANCEL_PATH_TEXT = (
    "O que você prefere?\n\n"
    "• *Remarcar Consulta*: mudar o dia ou o horário desta consulta.\n"
    "• *Agendar Outra*: agendar outra consulta diferente. Esta só é cancelada "
    "quando a nova for confirmada.\n"
    "• *Cancelar Consulta*: cancelar esta consulta."
)
OTHER_TEXT = "Claro! Me conta como posso te ajudar com a sua consulta."
GIVE_UP_CONFIRM_TEXT = "Tem certeza que quer cancelar a consulta de {when}?"
KEPT_TEXT = "Combinado! Sua consulta continua marcada para {when}."
ALSO_SCHEDULED_TEXT = "Você também tem consulta em {whens}."
BOOK_ANOTHER_INTRO = (
    "Combinado! Vamos agendar a outra consulta. A de {when} só será cancelada "
    "quando você confirmar a nova."
)

# Continuations run by workers/shared/actions.py::_handle_action_button after
# this module checked the row. "apptresched" IS that module's existing branch
# (reschedule entry + the Pix reschedule limit); the other two are R3's.
CONTINUE_RESCHEDULE = "apptresched"
CONTINUE_CANCEL_AND_ASK_WHY = "reminder_cancel_and_ask_why"
CONTINUE_BOOK_ANOTHER = "reminder_book_another"

# How many other appointments the "você também tem consulta" line names.
_ALSO_SCHEDULED_LIMIT = 3


async def _retention_warning(session, tenant: Tenant, appointment: Appointment, now) -> str | None:
    """The Pix retention sentence when a cancel now would keep the patient's money.

    Same rule `_handle_action_button`'s `apptcancel` branch applies: a PAID
    deposit and a start inside `tenant.pix_refund_window_hours`.
    """
    deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
    hours_until = _hours_until_start(appointment, now)
    if (
        deposit is not None
        and deposit.status == PixDepositStatus.PAID
        and hours_until is not None
        and hours_until <= tenant.pix_refund_window_hours
    ):
        return _pix_retention_warning_line(tenant, deposit)
    return None


async def _also_scheduled(session, tenant: Tenant, appointment: Appointment, now) -> str:
    """ "\\n\\nVocê também tem consulta em …" for the patient's OTHER live appointments, or ""."""
    starts = list(
        await session.scalars(
            select(Appointment.start_at)
            .where(
                Appointment.tenant_id == tenant.id,
                Appointment.patient_id == appointment.patient_id,
                Appointment.id != appointment.id,
                Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
                Appointment.start_at > now,
            )
            .order_by(Appointment.start_at)
            .limit(_ALSO_SCHEDULED_LIMIT)
        )
    )
    if not starts:
        return ""
    whens = [_format_appointment_when(start, tenant.timezone) for start in starts]
    joined = whens[0] if len(whens) == 1 else f"{', '.join(whens[:-1])} e {whens[-1]}"
    return f"\n\n{ALSO_SCHEDULED_TEXT.format(whens=joined)}"


async def handle_reminder_button(
    reply: _ReplyContext, action: str, reminder_id: str, redis=None
) -> tuple[str, str] | None:
    """Answer one reminder-button tap; never raises on a foreign or stale id.

    Returns a continuation `(action, appointment_id)` for
    `_handle_action_button` when the next step is one of its
    appointment-scoped branches, else None (the tap was fully answered here).
    """
    if reply.conversation_id is None or action not in REMINDER_ROW_ACTIONS:
        return None
    try:
        row_id = UUID(reminder_id)
    except ValueError:
        return None
    now = datetime.now(UTC)
    hand_to_ai_text: str | None = None
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return None
        waba_token = await get_waba_token(session, tenant.id)
        client = _reply_sender(reply, tenant, waba_token)
        if client is None:
            logger.error("reminder_button_no_sender", tenant_id=str(tenant.id))
            return None

        reminder = await session.get(AppointmentReminder, row_id)
        appointment = (
            await session.get(Appointment, reminder.appointment_id)
            if reminder is not None
            else None
        )
        if (
            reminder is None
            or appointment is None
            or reminder.tenant_id != tenant.id
            or appointment.tenant_id != tenant.id
            or reminder.patient_id is None
            or reminder.patient_id != conversation.patient_id
        ):
            logger.info("reminder_button_refused", tenant_id=str(tenant.id), action=action)
            await client.send_text_message(to=reply.patient_ref, body=NOT_FOUND_TEXT)
            return None

        if (
            not is_live_status(appointment.status)
            or appointment.start_at is None
            or _as_utc(appointment.start_at) <= now
        ):
            await client.send_text_message(to=reply.patient_ref, body=NOT_ACTIVE_TEXT)
            return None
        when = _format_appointment_when(appointment.start_at, tenant.timezone)
        if _as_utc(reminder.appointment_start_at) != _as_utc(appointment.start_at):
            await client.send_text_message(to=reply.patient_ref, body=MOVED_TEXT.format(when=when))
            return None

        if reminder.invalidated_at is not None or reminder.status == REMINDER_STATUS_CANCELLED:
            # A retired row (same rules as register_confirmation's `stale`): no
            # write, and never a claim of confirmation.
            await client.send_text_message(to=reply.patient_ref, body=NOT_ACTIVE_TEXT)
            return None
        from_chat = reminder.kind == REMINDER_KIND_CHAT

        if action == ACTION_CONFIRM:
            try:
                await reminder_schedule.register_confirmation(
                    session,
                    appointment=appointment,
                    reminder_id=reminder.id,
                    source=(
                        reminder_schedule.CONFIRMATION_SOURCE_CHAT_PROMPT
                        if from_chat
                        else reminder_schedule.CONFIRMATION_SOURCE_REMINDER_BUTTON
                    ),
                    now=now,
                )
            except reminder_schedule.ReminderMismatchError:
                # Checked above; kept so a race can never surface as an error.
                await client.send_text_message(to=reply.patient_ref, body=NOT_FOUND_TEXT)
                return None
            also = await _also_scheduled(session, tenant, appointment, now) if from_chat else ""
            await session.commit()
            if reminder.answer != REMINDER_ANSWER_CONFIRM:
                # Nothing was recorded (e.g. the row went stale in a race): do not
                # tell the patient they are confirmed.
                await client.send_text_message(to=reply.patient_ref, body=NOT_ACTIVE_TEXT)
                return None
            await client.send_text_message(
                to=reply.patient_ref, body=CONFIRMED_TEXT.format(when=when) + also
            )
            return None

        if action in (ACTION_CANCEL, ACTION_OTHER):
            if reminder.answer != REMINDER_ANSWER_CONFIRM:
                # A confirmation already counted stays recorded: overwriting it
                # would let the same message count twice (R1 dedupes on it).
                reminder.answer = (
                    REMINDER_ANSWER_CANCEL if action == ACTION_CANCEL else REMINDER_ANSWER_OTHER
                )
                reminder.answered_at = now
            also = (
                await _also_scheduled(session, tenant, appointment, now)
                if from_chat and action == ACTION_OTHER
                else ""
            )
            await session.commit()
            if action == ACTION_CANCEL:
                await client.send_buttons(
                    reply.patient_ref, CANCEL_PATH_TEXT, cancel_path_buttons(reminder.id)
                )
                return None
            hand_to_ai_text = OTHER_TEXT + also

        elif action == ACTION_KEEP:
            await client.send_text_message(to=reply.patient_ref, body=KEPT_TEXT.format(when=when))
            return None

        elif action == ACTION_GIVE_UP:
            body = GIVE_UP_CONFIRM_TEXT.format(when=when)
            warning = await _retention_warning(session, tenant, appointment, now)
            if warning:
                body = f"{warning} {body}"
            await client.send_buttons(reply.patient_ref, body, give_up_confirm_buttons(reminder.id))
            return None

        elif action == ACTION_RESCHEDULE_THIS:
            return CONTINUE_RESCHEDULE, str(appointment.id)
        elif action == ACTION_GIVE_UP_CONFIRM:
            return CONTINUE_CANCEL_AND_ASK_WHY, str(appointment.id)
        elif action == ACTION_BOOK_ANOTHER:
            return CONTINUE_BOOK_ANOTHER, str(appointment.id)

    if hand_to_ai_text is not None:
        # "Outro": full LLM mode through the one persistence seam (a result that
        # names nothing else also drops any half-made booking), then the
        # invitation. The patient's next message reaches the agent with the
        # appointment context (orchestrator: flow_state == LLM).
        await _apply_flow_result(
            reply,
            FlowRouterResult(action="delegate_llm", flow_state=FlowState.LLM),
            reply.patient_ref,
            redis=redis,
            tenant=tenant,
            waba_token=waba_token,
        )
        await client.send_text_message(to=reply.patient_ref, body=hand_to_ai_text)
    return None
