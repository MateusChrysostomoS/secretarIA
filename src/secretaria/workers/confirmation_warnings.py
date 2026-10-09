"""Clinic warnings for unconfirmed appointments (TASK-032 R4, spec 4.4).

R2's reminder engine leaves, on every reminder row it sent (or gave up on), a
`warn_due_at` and a `warn_kind`. This module is the other half: when that
deadline passes and the patient still has not confirmed, it warns the clinic.

The row's `warned_at` is the single marker for BOTH things the spec asks for:
it makes the warning fire once (the claim below is an atomic conditional
UPDATE, so two workers or two overlapping ticks cannot both win), and it is
what R1's `display_state` reads to turn the appointment red in the agenda. That
is why the claim happens BEFORE the e-mail and is never rolled back: a clinic
with no alert address, or a transport hiccup, must still see the red state; the
e-mail is best-effort on top (see `_notify_clinic`).

Nothing here sends anything to the patient and nothing releases a slot.
"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.appointment_reminder import (
    REMINDER_KIND_CHAT,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_SENT,
    REMINDER_WARN_DELIVERY_FAILED,
    REMINDER_WARN_UNCONFIRMED,
)

# Spec 4.4: at most three warnings per appointment (custom, day, hour).
MAX_WARNINGS_PER_APPOINTMENT = 3
# Rows examined per one-minute tick, so a tick never outlives the next one.
BATCH_SIZE = 200

# R2 contract: only a row that was really sent (or definitively failed) is
# warned about; a `pending` row keeps R1's `warn_due_at` but never went out.
_WARNABLE_ROW_STATUSES = (REMINDER_STATUS_SENT, REMINDER_STATUS_FAILED)
_WARN_KINDS = (REMINDER_WARN_UNCONFIRMED, REMINDER_WARN_DELIVERY_FAILED)


@dataclass(frozen=True)
class WarningCandidate:
    """One reminder row whose warning is due, with what the claim must re-check."""

    reminder_id: UUID
    appointment_id: UUID
    tenant_id: UUID
    kind: str
    warn_kind: str
    # The appointment start this row was planned for (R1's version); the cap is
    # counted per version so a reschedule starts a fresh cycle.
    start_version: datetime


def _appointment_conditions(now: datetime) -> list:
    """The appointment must still need the warning: live, unconfirmed, upcoming."""
    return [
        Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
        Appointment.confirmation_count == 0,
        Appointment.start_at.is_not(None),
        Appointment.start_at > now,
    ]


async def due_candidates(
    session: AsyncSession, now: datetime, *, limit: int = BATCH_SIZE
) -> list[WarningCandidate]:
    """Rows whose warning is due. Read-only; the claim re-checks everything."""
    rows = await session.execute(
        select(
            AppointmentReminder.id,
            AppointmentReminder.appointment_id,
            AppointmentReminder.tenant_id,
            AppointmentReminder.kind,
            AppointmentReminder.warn_kind,
            AppointmentReminder.appointment_start_at,
        )
        .join(Appointment, Appointment.id == AppointmentReminder.appointment_id)
        .join(Tenant, Tenant.id == Appointment.tenant_id)
        .where(
            # A reminder row of another clinic pointing at this appointment is
            # corrupt data; never let it reach a clinic's inbox.
            AppointmentReminder.tenant_id == Appointment.tenant_id,
            AppointmentReminder.status.in_(_WARNABLE_ROW_STATUSES),
            AppointmentReminder.kind != REMINDER_KIND_CHAT,
            AppointmentReminder.warn_kind.in_(_WARN_KINDS),
            AppointmentReminder.warn_due_at.is_not(None),
            AppointmentReminder.warn_due_at <= now,
            AppointmentReminder.warned_at.is_(None),
            # Retired rows (reschedule / R6 edit of day or time) never warn.
            AppointmentReminder.invalidated_at.is_(None),
            # Only the row of the appointment's CURRENT start: a reschedule
            # cancelled the older ones.
            AppointmentReminder.appointment_start_at == Appointment.start_at,
            Tenant.reminders_v2_enabled.is_(True),
            *_appointment_conditions(now),
        )
        .order_by(AppointmentReminder.warn_due_at, AppointmentReminder.id)
        .limit(limit)
    )
    return [
        WarningCandidate(
            reminder_id=row.id,
            appointment_id=row.appointment_id,
            tenant_id=row.tenant_id,
            kind=row.kind,
            warn_kind=row.warn_kind,
            start_version=row.appointment_start_at,
        )
        for row in rows.all()
    ]


async def claim_warning(session: AsyncSession, candidate: WarningCandidate, now: datetime) -> bool:
    """Set `warned_at` iff nobody did and the appointment still needs the warning.

    One conditional UPDATE: the row must still be unwarned and sent/failed, and
    the appointment must still be live, unconfirmed and upcoming - so a patient
    who confirms (or a clinic that cancels/releases) between the selection and
    this call defeats the claim. Returns True only for the call that won. The
    caller owns the transaction.
    """
    already = await session.scalar(
        select(func.count())
        .select_from(AppointmentReminder)
        .where(
            AppointmentReminder.appointment_id == candidate.appointment_id,
            AppointmentReminder.tenant_id == candidate.tenant_id,
            AppointmentReminder.appointment_start_at == candidate.start_version,
            AppointmentReminder.invalidated_at.is_(None),
            AppointmentReminder.warned_at.is_not(None),
        )
    )
    if (already or 0) >= MAX_WARNINGS_PER_APPOINTMENT:
        return False
    result = await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.id == candidate.reminder_id,
            AppointmentReminder.tenant_id == candidate.tenant_id,
            AppointmentReminder.invalidated_at.is_(None),
            AppointmentReminder.warned_at.is_(None),
            AppointmentReminder.status.in_(_WARNABLE_ROW_STATUSES),
            AppointmentReminder.appointment_id.in_(
                select(Appointment.id).where(
                    Appointment.id == candidate.appointment_id,
                    Appointment.tenant_id == candidate.tenant_id,
                    *_appointment_conditions(now),
                )
            ),
        )
        .values(warned_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1
