"""Send the reminder card that opens a returning patient's chat (TASK-032 R3, spec §4.3).

The decision was taken inside the inbound transaction
(services/reminder_opening.py::decide_reminder_opening); this runs from
`_send_bot_reply_inner`, after the entitlement gate, so an unentitled clinic
creates no row and sends nothing. Everything is re-checked here because time
passed since the decision: the appointment must still be this conversation's
patient's, live and in the future. Automatic quiet returns stop at two
confirmations; an explicit context/first-contact card keeps management actions
available while the shared confirmation writer still enforces the cap.

The card is the SAME text and buttons as a cron reminder (R2's builders), on
the `chat` row of this appointment version - created now or re-shown. It goes
out through `_send_buttons_reply`, which picks the channel (WhatsApp or Portal)
and records the card (the Portal's tap check reads that record,
workers/portal/inbound.py).
"""

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.models import Appointment, Conversation, Tenant, is_live_status
from secretaria.services import reminder_schedule
from secretaria.services.reminder_opening import ensure_chat_reminder
from secretaria.services.reminder_text import (
    build_reminder_body,
    load_reminder_content,
    reminder_buttons,
)
from secretaria.workers.shared.context import _ReplyContext
from secretaria.workers.shared.dispatch import _send_buttons_reply
from secretaria.workers.shared.text import _as_utc

logger = get_logger(__name__)


async def _prepare_opening(
    reply: _ReplyContext, tenant: Tenant, now: datetime
) -> tuple[str, list[tuple[str, str]]] | None:
    async with async_session_factory() as session:
        async with session.begin():
            conversation = await session.get(Conversation, reply.conversation_id)
            appointment = await session.scalar(
                select(Appointment).where(
                    Appointment.id == reply.reminder_opening_appointment_id,
                    Appointment.tenant_id == tenant.id,
                )
            )
            if (
                conversation is None
                or appointment is None
                or appointment.patient_id is None
                or appointment.patient_id != conversation.patient_id
                or not is_live_status(appointment.status)
                or appointment.start_at is None
                or _as_utc(appointment.start_at) <= now
                or (
                    appointment.confirmation_count >= reminder_schedule.MAX_CONFIRMATIONS
                    and not reply.reminder_opening_first_contact
                )
            ):
                return None
            row = await ensure_chat_reminder(session, appointment, now=now)
            content = await load_reminder_content(session, tenant, appointment)
            return build_reminder_body(content), reminder_buttons(row.id)


async def _send_reminder_opening(
    reply: _ReplyContext, *, tenant: Tenant, waba_token: str | None,
    still_current: Callable[[], Awaitable[bool]] | None = None,
) -> bool | None:
    """Send the opening card. Returns False (logged) when it no longer applies."""
    if reply.reminder_opening_appointment_id is None or reply.conversation_id is None:
        return False
    now = datetime.now(UTC)
    try:
        prepared = await _prepare_opening(reply, tenant, now)
    except IntegrityError:
        # A twin turn created this version's chat row a moment ago: re-use it.
        prepared = await _prepare_opening(reply, tenant, now)
    if prepared is None:
        logger.info(
            "reminder_opening_dropped",
            conversation_id=str(reply.conversation_id),
            tenant_id=str(tenant.id),
        )
        return False
    if still_current is not None and not await still_current():
        # Preparing the row/body awaits another transaction. A patient action
        # that arrived meanwhile supersedes this unsolicited entry opening.
        logger.info("reminder_opening_superseded", conversation_id=str(reply.conversation_id),
                    tenant_id=str(tenant.id))
        return None
    body, buttons = prepared
    await _send_buttons_reply(
        reply,
        tenant=tenant,
        waba_token=waba_token,
        body=body,
        buttons=buttons,
        event="reminder_opening_sent",
    )
    return True
