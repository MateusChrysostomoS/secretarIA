"""After a "Marcar outra" booking committed: finish the original (TASK-032 R3).

`services/appointment_replacement.py::cancel_replaced_appointment` cancels the
original inside the new booking's transaction. What cannot live in that
transaction runs here, AFTER the commit, best-effort - a failure is logged and
never undoes the booking the patient just confirmed:

  * the original's Google Calendar event is deleted on the calendar that OWNS
    it (its professional's, or the clinic's) - never a guessed agenda; with an
    owner that no longer resolves nothing is deleted and that is logged, the
    same "don't guess, degrade" rule as `actions.py::_calendar_for_appointment`;
  * the original's pending reminder rows are cancelled through R2's hook.
"""

from secretaria.core.database import async_session_factory
from secretaria.core.logging import get_logger
from secretaria.services import reminder_hooks
from secretaria.services.appointment_replacement import REPLACEMENT_REASON, ReplacedAppointment
from secretaria.services.tenant_config import list_active_professionals
from secretaria.workers.shared.greeting import _format_appointment_when
from secretaria.workers.shared.llm_context import (
    _appointment_calendar,
    _appointment_calendar_target,
)

logger = get_logger(__name__)

REPLACED_TEXT = "Sua consulta anterior, de {when}, foi cancelada."


def _replaced_notice(tenant, replaced: ReplacedAppointment) -> str:
    """The line appended to the new booking's confirmation (plus the Pix sentence)."""
    text = REPLACED_TEXT.format(when=_format_appointment_when(replaced.start_at, tenant.timezone))
    return f"{text} {replaced.money_note}" if replaced.money_note else text


async def _finish_replacement(tenant, replaced: ReplacedAppointment) -> None:
    """Delete the original's Google event and close its reminder rows. Never raises."""
    if replaced.google_event_id:
        try:
            async with async_session_factory() as session:
                professional_rows = await list_active_professionals(session, tenant.id)
                target = _appointment_calendar_target(
                    {"professional_id": replaced.professional_id}, professional_rows
                )
                calendar = await _appointment_calendar(session, tenant, target)
            if calendar is None:
                logger.warning(
                    "appointment_replacement_calendar_missing",
                    tenant_id=str(tenant.id),
                    appointment_id=str(replaced.appointment_id),
                )
            else:
                await calendar.cancel_event(replaced.google_event_id)
        except Exception as exc:
            logger.warning(
                "appointment_replacement_calendar_cancel_failed",
                tenant_id=str(tenant.id),
                appointment_id=str(replaced.appointment_id),
                error_type=type(exc).__name__,
            )
    if reminder_hooks.enabled_for(tenant):
        await reminder_hooks.after_appointment_closed(
            replaced.appointment_id, reason=REPLACEMENT_REASON
        )
