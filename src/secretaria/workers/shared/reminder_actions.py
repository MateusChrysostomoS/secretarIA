"""A tap on a reminder button: remconfirm| / remcancel| / remother| (TASK-032 R2).

The id after the bar is an `appointment_reminders` ROW id. The older
`apptconfirm|<appointment_id>` family can only be checked against the TENANT;
the row names the patient too, so a tap is honoured only when the row's tenant
AND patient are the tapping conversation's. Anything else gets the same polite
miss, whichever check failed - never reveal whose row it is.

Channel-neutral: replies go through `_reply_sender`, so this serves a WhatsApp
tap today and a Portal tap once R3 decodes Portal ids
(schemas/webhook.py::decode_action_id). Runs before the handover/flow/LLM
gates, like every action button (workers/turn_router.py), and never changes
`flow_state` itself.
"""

from datetime import UTC, datetime
from uuid import UUID

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    Conversation,
    Tenant,
    is_live_status,
)
from secretaria.models.appointment_reminder import (
    REMINDER_ANSWER_CANCEL,
    REMINDER_ANSWER_CONFIRM,
    REMINDER_ANSWER_OTHER,
)
from secretaria.services import reminder_schedule
from secretaria.services.reminder_text import ACTION_CANCEL, ACTION_CONFIRM, REMINDER_ACTIONS
from secretaria.services.tenant_config import get_waba_token
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.greeting import _format_appointment_when
from secretaria.workers.shared.sender import _reply_sender
from secretaria.workers.shared.text import _as_utc

logger = get_logger(__name__)

NOT_FOUND_TEXT = "Não encontrei essa consulta."
NOT_ACTIVE_TEXT = "Essa consulta não está mais ativa."
CONFIRMED_TEXT = "Presença confirmada! ✅ Até {when}."
MOVED_TEXT = "Essa mensagem era sobre um horário antigo. Sua consulta agora é em {when}."
CANCEL_PATH_TEXT = "O que você prefere?"
OTHER_TEXT = "Claro! Me conta como posso te ajudar com a sua consulta."
# <= 20 characters each (WhatsApp reply-button cap).
LABEL_RESCHEDULE = "Remarcar"
LABEL_GIVE_UP = "Não vou mais"


def cancel_path_buttons(appointment_id) -> list[tuple[str, str]]:
    """Interim "Cancelar" card. R3 replaces it with Remarcar / Marcar outra / Não vou mais.

    Both ids are the existing appointment-scoped ones, so the Pix rules keep
    applying unchanged: `apptresched|` checks the reschedule limit and
    `apptcancel|` shows the retention warning inside the refund window
    (workers/shared/actions.py::_handle_action_button).
    """
    return [
        (f"apptresched|{appointment_id}", LABEL_RESCHEDULE),
        (f"apptcancel|{appointment_id}", LABEL_GIVE_UP),
    ]


async def handle_reminder_button(
    reply: _ReplyContext, action: str, reminder_id: str, redis=None
) -> None:
    """Answer one reminder-button tap. Never raises on a foreign or stale id."""
    if reply.conversation_id is None or action not in REMINDER_ACTIONS:
        return
    try:
        row_id = UUID(reminder_id)
    except ValueError:
        return
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        conversation = await session.get(Conversation, reply.conversation_id)
        tenant = await session.get(Tenant, conversation.tenant_id) if conversation else None
        if tenant is None:
            return
        waba_token = await get_waba_token(session, tenant.id)
        client = _reply_sender(reply, tenant, waba_token)
        if client is None:
            logger.error("reminder_button_no_sender", tenant_id=str(tenant.id))
            return

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
            return

        if (
            not is_live_status(appointment.status)
            or appointment.start_at is None
            or _as_utc(appointment.start_at) <= now
        ):
            await client.send_text_message(to=reply.patient_ref, body=NOT_ACTIVE_TEXT)
            return
        when = _format_appointment_when(appointment.start_at, tenant.timezone)
        if _as_utc(reminder.appointment_start_at) != _as_utc(appointment.start_at):
            await client.send_text_message(to=reply.patient_ref, body=MOVED_TEXT.format(when=when))
            return

        if action == ACTION_CONFIRM:
            try:
                await reminder_schedule.register_confirmation(
                    session,
                    appointment=appointment,
                    reminder_id=reminder.id,
                    source=reminder_schedule.CONFIRMATION_SOURCE_REMINDER_BUTTON,
                    now=now,
                )
            except reminder_schedule.ReminderMismatchError:
                # Checked above; kept so a race can never surface as an error.
                await client.send_text_message(to=reply.patient_ref, body=NOT_FOUND_TEXT)
                return
            await session.commit()
            await client.send_text_message(
                to=reply.patient_ref, body=CONFIRMED_TEXT.format(when=when)
            )
            return

        if reminder.answer != REMINDER_ANSWER_CONFIRM:
            # A confirmation already counted stays recorded: overwriting it would
            # let the same message count twice (R1 dedupes on answer == confirm).
            reminder.answer = (
                REMINDER_ANSWER_CANCEL if action == ACTION_CANCEL else REMINDER_ANSWER_OTHER
            )
            reminder.answered_at = now
        await session.commit()
        if action == ACTION_CANCEL:
            await client.send_buttons(
                reply.patient_ref, CANCEL_PATH_TEXT, cancel_path_buttons(appointment.id)
            )
        else:
            await client.send_text_message(to=reply.patient_ref, body=OTHER_TEXT)
