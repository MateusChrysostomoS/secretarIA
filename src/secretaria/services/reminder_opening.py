"""Whether - and with which appointment - a returning patient's chat opens (TASK-032 R3).

Spec §4.3: a patient with a LIVE future appointment who writes after the same
silence floor the reactivation offer uses (`reactivation_gap_minutes`, default
6 h) sees that appointment's reminder - Confirmar / Cancelar / Outro - as the
first message, instead of the generic greeting/offer. The nearest appointment
is the one shown.

The card is backed by ONE `appointment_reminders` row of kind `chat` per
appointment version (R1's unique key (appointment, kind, start)): showing it
again later re-uses that row, so the same message can never be confirmed twice
(R1 dedupes on the row). The rules below keep the chat and the cron reminders
(R2) from repeating each other:

  * never when the patient already confirmed twice (spec: the requests stop);
  * never when this version's chat card was already confirmed;
  * never twice inside the gap (the chat row's own `sent_at`);
  * never when an UNANSWERED cron reminder that carried the buttons is itself
    the conversation's latest activity - the patient has that very card in
    front of them (R2 records every sent reminder as an outbound message, so
    its `sent_at` and the conversation's last activity coincide).

`decide_reminder_opening` runs inside the inbound transaction
(workers/turn_router.py) and only reads; `ensure_chat_reminder` runs at send
time (workers/shared/reminder_opening.py). The clinic switch is checked first,
without a query: a switched-OFF clinic pays nothing per turn.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.models import LIVE_APPOINTMENT_STATUSES, Appointment, AppointmentReminder, Tenant
from secretaria.models.appointment_reminder import (
    REMINDER_ANSWER_CONFIRM,
    REMINDER_CHANNEL_CHAT,
    REMINDER_KIND_CHAT,
    REMINDER_STATUS_SENT,
    REMINDER_WARN_UNCONFIRMED,
)
from secretaria.services import reminder_hooks, reminder_schedule
from secretaria.services.flow_router import reactivation_gap_minutes
from secretaria.services.patient_context import as_utc

OPEN = "open"
SKIP_SWITCH_OFF = "switch_off"
SKIP_NO_HISTORY = "no_history"
SKIP_RECENT_ACTIVITY = "recent_activity"
SKIP_NO_UPCOMING = "no_upcoming"
SKIP_CONFIRMED_TWICE = "confirmed_twice"
SKIP_CHAT_ALREADY_CONFIRMED = "chat_already_confirmed"
SKIP_DUPLICATE_OPENING = "duplicate_opening"
SKIP_REMINDER_WAITING = "reminder_waiting"

# A reminder sent this close to the conversation's last activity IS that
# activity (send and record happen a moment apart).
REMINDER_WAITING_TOLERANCE = timedelta(minutes=5)


@dataclass(frozen=True)
class OpeningDecision:
    """`reason` is OPEN (then `appointment_id` is set) or one SKIP_* code (logged)."""

    reason: str
    appointment_id: UUID | None = None


async def nearest_live_appointment(
    session: AsyncSession, tenant_id: UUID, patient_id: UUID, *, now: datetime
) -> Appointment | None:
    """The patient's nearest LIVE appointment in this clinic that has not started."""
    return await session.scalar(
        select(Appointment)
        .where(
            Appointment.tenant_id == tenant_id,
            Appointment.patient_id == patient_id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            Appointment.start_at > now,
        )
        .order_by(Appointment.start_at)
        .limit(1)
    )


async def _current_version_rows(
    session: AsyncSession, appointment: Appointment
) -> list[AppointmentReminder]:
    """Every reminder row of the appointment's CURRENT start (compared in UTC:
    SQLite hands datetimes back naive)."""
    rows = await session.scalars(
        select(AppointmentReminder).where(
            AppointmentReminder.tenant_id == appointment.tenant_id,
            AppointmentReminder.appointment_id == appointment.id,
        )
    )
    start = as_utc(appointment.start_at)
    return [row for row in rows if as_utc(row.appointment_start_at) == start]


async def decide_reminder_opening(
    session: AsyncSession,
    *,
    tenant: Tenant,
    patient_id: UUID | None,
    last_activity_at: datetime | None,
    now: datetime,
) -> OpeningDecision:
    """Decide whether this turn opens with the appointment reminder. Read-only."""
    if not reminder_hooks.enabled_for(tenant) or patient_id is None:
        return OpeningDecision(SKIP_SWITCH_OFF)
    if last_activity_at is None:
        return OpeningDecision(SKIP_NO_HISTORY)
    gap = timedelta(minutes=reactivation_gap_minutes(tenant))
    last_activity = as_utc(last_activity_at)
    if now - last_activity < gap:
        return OpeningDecision(SKIP_RECENT_ACTIVITY)
    appointment = await nearest_live_appointment(session, tenant.id, patient_id, now=now)
    if appointment is None:
        return OpeningDecision(SKIP_NO_UPCOMING)
    if appointment.confirmation_count >= reminder_schedule.MAX_CONFIRMATIONS:
        return OpeningDecision(SKIP_CONFIRMED_TWICE, appointment.id)
    for row in await _current_version_rows(session, appointment):
        if row.kind == REMINDER_KIND_CHAT:
            if row.answer == REMINDER_ANSWER_CONFIRM:
                return OpeningDecision(SKIP_CHAT_ALREADY_CONFIRMED, appointment.id)
            if row.sent_at is not None and now - as_utc(row.sent_at) < gap:
                return OpeningDecision(SKIP_DUPLICATE_OPENING, appointment.id)
        elif (
            row.status == REMINDER_STATUS_SENT
            and row.answer is None
            and row.warn_kind == REMINDER_WARN_UNCONFIRMED
            and row.sent_at is not None
            and as_utc(row.sent_at) >= last_activity - REMINDER_WAITING_TOLERANCE
        ):
            return OpeningDecision(SKIP_REMINDER_WAITING, appointment.id)
    return OpeningDecision(OPEN, appointment.id)


async def ensure_chat_reminder(
    session: AsyncSession, appointment: Appointment, *, now: datetime
) -> AppointmentReminder:
    """The `chat` row of the appointment's current version, created or re-shown.

    Born already sent (spec §4.1: "`chat` nasce já enviado"), on the `chat`
    channel, with the buttons and WITHOUT a clinic warning (`warn_due_at`
    stays NULL - R4 warns only from the cron reminders). A row shown before is
    re-used and only its `sent_at` moves; its answer is kept, so confirming the
    same card twice still counts once. Flushes; the caller commits. A twin turn
    racing on the unique key surfaces as IntegrityError at flush/commit - the
    caller retries once and finds the row.
    """
    row = next(
        (
            r
            for r in await _current_version_rows(session, appointment)
            if r.kind == REMINDER_KIND_CHAT
        ),
        None,
    )
    if row is None:
        row = AppointmentReminder(
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            kind=REMINDER_KIND_CHAT,
            appointment_start_at=appointment.start_at,
            due_at=now,
            status=REMINDER_STATUS_SENT,
            channel=REMINDER_CHANNEL_CHAT,
            with_prompt=True,
            sent_at=now,
            warn_due_at=None,
        )
        session.add(row)
    else:
        row.sent_at = now
    await session.flush()
    return row
