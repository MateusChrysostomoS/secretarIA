"""Cancel the appointment a "Marcar outra consulta" booking replaces (TASK-032 R3).

Spec §4.3 and owner decision 7: the original is cancelled ONLY when the new
booking is confirmed, in the same transaction as the new `Appointment` row -
so a booking that fails to persist never cancels anything, and a patient who
gives up half-way keeps the appointment they had.

Called from the two places an appointment is confirmed from the flow:
`workers/shared/flow_runner.py::_apply_flow_result` (WhatsApp, and a verified
Portal patient) and `workers/shared/booking_hold.py::_promote_booking_hold`
(the Portal code gate). The caller owns the transaction; nothing here commits.
What lives outside the database - the Google event of the original and its
reminder rows - is finished after the commit by
`workers/shared/replacement.py::_finish_replacement`.

The marker is never trusted on its own: the original must belong to the same
clinic AND the same patient as the conversation, still be live and still be in
the future. Anything else is a stale marker and is ignored (logged), never an
error that could cost the patient the booking they just made.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Tenant, is_live_status
from secretaria.services.appointment_status import SOURCE_FLOW, log_status_transition
from secretaria.services.patient_context import as_utc
from secretaria.services.payments import deposit_lifecycle

logger = get_logger(__name__)

# The reason the reminder rows of the original are closed with
# (services/reminder_hooks.py::after_appointment_closed).
REPLACEMENT_REASON = "replaced"


@dataclass(frozen=True)
class ReplacedAppointment:
    """What the after-commit step and the patient's confirmation need to know."""

    appointment_id: UUID
    google_event_id: str | None
    professional_id: UUID | None
    start_at: datetime
    # The Pix sentence (deposit_lifecycle.cancellation_notice), or None.
    money_note: str | None


async def cancel_replaced_appointment(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient_id: UUID | None,
    replaced_id: UUID | None,
    new_appointment_id: UUID | None,
    waba_token: str | None = None,
    now: datetime | None = None,
) -> ReplacedAppointment | None:
    """Cancel the replaced appointment inside the caller's transaction.

    Returns None (and changes nothing) when there is no marker, when it points
    at the new booking itself, or when the original is not this patient's live
    future appointment in this clinic. The money hook runs exactly as on a
    normal cancel; if it raises, the original is still cancelled (the patient
    asked for it and the new booking exists) and the failure is logged.
    """
    if replaced_id is None or patient_id is None or replaced_id == new_appointment_id:
        return None
    now = now or datetime.now(UTC)
    original = await session.scalar(
        select(Appointment).where(
            Appointment.id == replaced_id,
            Appointment.tenant_id == tenant.id,
            Appointment.patient_id == patient_id,
        )
    )
    if (
        original is None
        or not is_live_status(original.status)
        or original.start_at is None
        or as_utc(original.start_at) <= now
    ):
        logger.info(
            "appointment_replacement_skipped",
            tenant_id=str(tenant.id),
            replaced_id=str(replaced_id),
            found=original is not None,
        )
        return None

    previous = original.status
    original.status = AppointmentStatus.CANCELLED
    log_status_transition(
        appointment_id=original.id,
        tenant_id=tenant.id,
        old_status=previous,
        new_status=AppointmentStatus.CANCELLED,
        source=SOURCE_FLOW,
        idempotency_key=f"replace:{original.id}",
    )
    money_note: str | None = None
    try:
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=original, waba_token=waba_token, now=now
        )
        if outcome is not None:
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, original.id)
            if deposit is not None:
                money_note = deposit_lifecycle.cancellation_notice(outcome, tenant, deposit)
    except Exception as exc:
        logger.warning(
            "appointment_replacement_money_hook_failed",
            tenant_id=str(tenant.id),
            appointment_id=str(original.id),
            error_type=type(exc).__name__,
        )
    logger.info(
        "appointment_replaced",
        tenant_id=str(tenant.id),
        appointment_id=str(original.id),
        new_appointment_id=str(new_appointment_id),
    )
    return ReplacedAppointment(
        appointment_id=original.id,
        google_event_id=original.google_event_id or None,
        professional_id=original.professional_id,
        start_at=original.start_at,
        money_note=money_note,
    )
