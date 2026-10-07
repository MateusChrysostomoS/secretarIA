"""Keep an appointment's reminder rows in step with the appointment (TASK-032 R2).

Every path that books, moves or closes an appointment calls one of the three
`after_*` hooks AFTER its own commit, and each hook runs in its own short
transaction. Deliberately not inside the caller's transaction: the agent and
the flow commit the appointment after the Google event already exists, so a
reminder failure that rolled the booking back would orphan the event and
lose the booking - a reminder is never worth that. The hooks never raise.

Call sites check `enabled_for(tenant)` first so a clinic with the switch OFF
does no extra I/O. The one exception is a reschedule of an appointment that
had confirmations: R1's `reschedule_reminders` zeroes the counter even with
the switch OFF (a moved booking is unconfirmed again), so those sites also
call the hook when `confirmation_count > 0`.

A crash between the caller's commit and the hook - or a clinic switched ON
with appointments already booked - leaves live future appointments without
rows of their current version. `reconcile_missing_reminders` (cron, every 10
minutes, workers/reminder_engine.py) plans exactly those: it is the backfill.
R1's `schedule_reminders` is idempotent and skips already-due kinds, so a
repeated or late call can never duplicate or send a stale reminder.

Uses `core.database.async_session_factory` through the module, looked up at
call time, so tests and the worker share one binding.
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.core.logging import get_logger
from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentReminder,
    Tenant,
)
from secretaria.models.appointment_reminder import REMINDER_KIND_CHAT
from secretaria.services import reminder_schedule

logger = get_logger(__name__)

RECONCILE_LIMIT = 500


def enabled_for(tenant) -> bool:
    """The clinic's switch, read defensively (snapshots, None, older rows)."""
    return getattr(tenant, "reminders_v2_enabled", False) is True


async def _load(session, appointment_id: UUID) -> tuple[Appointment | None, Tenant | None]:
    appointment = await session.get(Appointment, appointment_id)
    if appointment is None:
        return None, None
    return appointment, await session.get(Tenant, appointment.tenant_id)


async def after_appointment_booked(appointment_id: UUID, *, now: datetime | None = None) -> int:
    """Plan the reminders of a new appointment. Returns how many rows were created."""
    try:
        async with core_database.async_session_factory() as session:
            async with session.begin():
                appointment, tenant = await _load(session, appointment_id)
                if appointment is None or tenant is None:
                    return 0
                rows = await reminder_schedule.schedule_reminders(
                    session, appointment, tenant, now=now or datetime.now(UTC)
                )
                return len(rows)
    except Exception as exc:
        logger.warning(
            "reminder_hook_failed",
            hook="booked",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return 0


async def after_appointment_rescheduled(
    appointment_id: UUID, *, now: datetime | None = None
) -> int:
    """The appointment moved: retire the old rows, zero the counter, plan anew."""
    try:
        async with core_database.async_session_factory() as session:
            async with session.begin():
                appointment, tenant = await _load(session, appointment_id)
                if appointment is None or tenant is None:
                    return 0
                rows = await reminder_schedule.reschedule_reminders(
                    session, appointment, tenant, now=now or datetime.now(UTC)
                )
                return len(rows or [])
    except Exception as exc:
        logger.warning(
            "reminder_hook_failed",
            hook="rescheduled",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return 0


async def after_appointment_closed(appointment_id: UUID, *, reason: str) -> int:
    """Cancelled / attended / no-show / released: cancel the rows still pending."""
    try:
        async with core_database.async_session_factory() as session:
            async with session.begin():
                return await reminder_schedule.cancel_reminders(
                    session, appointment_id, reason=reason
                )
    except Exception as exc:
        logger.warning(
            "reminder_hook_failed",
            hook="closed",
            appointment_id=str(appointment_id),
            error_type=type(exc).__name__,
        )
        return 0


async def reconcile_missing_reminders(*, now: datetime, limit: int = RECONCILE_LIMIT) -> int:
    """Plan every live future appointment of a switched-ON clinic that has no
    row for its CURRENT start. Returns how many rows were created. Never raises."""
    try:
        current_version = (
            select(AppointmentReminder.id)
            .where(
                AppointmentReminder.appointment_id == Appointment.id,
                AppointmentReminder.appointment_start_at == Appointment.start_at,
                # TASK-032 R3: a `chat` row (the opening card) is not a plan -
                # an appointment that only has one still needs its cron rows.
                AppointmentReminder.kind != REMINDER_KIND_CHAT,
            )
            .exists()
        )
        async with core_database.async_session_factory() as session:
            candidates = list(
                await session.scalars(
                    select(Appointment.id)
                    .join(Tenant, Tenant.id == Appointment.tenant_id)
                    .where(
                        Tenant.reminders_v2_enabled.is_(True),
                        Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
                        Appointment.patient_id.is_not(None),
                        Appointment.start_at > now,
                        ~current_version,
                    )
                    .order_by(Appointment.start_at)
                    .limit(limit)
                )
            )
    except Exception as exc:
        logger.warning("reminder_reconcile_failed", error_type=type(exc).__name__)
        return 0
    created = 0
    for appointment_id in candidates:
        created += await after_appointment_booked(appointment_id, now=now)
    logger.info("reminders_reconciled", candidates=len(candidates), created=created)
    return created
