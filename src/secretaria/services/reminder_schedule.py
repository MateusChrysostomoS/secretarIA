"""The reminder schedule and the confirmation counter - TASK-032, spec 4.1/4.2.

The ONLY writer of `appointment_reminders` rows' lifecycle (schedule / cancel /
reschedule) and of `Appointment.confirmation_count`. Nothing here sends a
message and nothing commits: callers own the transaction, these functions
`flush`. Wiring into the creation/reschedule/cancel paths and the cron is R2.

Datetimes: every comparison goes through `_as_utc`, because SQLite (the test
engine) returns naive datetimes for `DateTime(timezone=True)` columns while
Postgres returns aware ones.

LGPD: logs carry ids, kinds and counts only.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import inspect, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.extra_reminder import custom_due_at
from secretaria.core.logging import get_logger
from secretaria.models import (
    LIVE_APPOINTMENT_STATUSES,
    Appointment,
    AppointmentStatus,
    Tenant,
    is_live_status,
)
from secretaria.models.appointment_reminder import (
    REMINDER_ANSWER_CONFIRM,
    REMINDER_CHANNEL_WHATSAPP,
    REMINDER_KIND_CUSTOM,
    REMINDER_KIND_DAY,
    REMINDER_KIND_HOUR,
    REMINDER_KINDS_STAFF,
    REMINDER_STATUS_CANCELLED,
    REMINDER_STATUS_PENDING,
    REMINDER_STATUS_SENDING,
    REMINDER_STATUS_SENT,
    AppointmentReminder,
)
from secretaria.services.appointment_status import (
    SOURCE_BUTTON,
    SOURCE_FLOW,
    SOURCE_HUB,
    log_status_transition,
)

logger = get_logger(__name__)

MAX_CONFIRMATIONS = 2

CONFIRMATION_SOURCE_REMINDER_BUTTON = "reminder_button"
CONFIRMATION_SOURCE_CHAT_PROMPT = "chat_prompt"
CONFIRMATION_SOURCE_STAFF = "staff"
_CONFIRMATION_SOURCES = {
    CONFIRMATION_SOURCE_REMINDER_BUTTON: SOURCE_BUTTON,
    CONFIRMATION_SOURCE_CHAT_PROMPT: SOURCE_FLOW,
    CONFIRMATION_SOURCE_STAFF: SOURCE_HUB,
}

DISPLAY_UNCONFIRMED = "unconfirmed"
DISPLAY_CONFIRMED = "confirmed"
DISPLAY_CONFIRMED_TWICE = "confirmed_twice"
DISPLAY_ATTENTION = "attention"

_DAY_LEAD = timedelta(hours=24)
_HOUR_LEAD = timedelta(hours=1)
# How long after a reminder is due the clinic is warned if still unconfirmed.
_WARN_AFTER = {
    REMINDER_KIND_CUSTOM: timedelta(hours=2),
    REMINDER_KIND_DAY: timedelta(hours=2),
    REMINDER_KIND_HOUR: timedelta(minutes=20),
}


class ReminderMismatchError(ValueError):
    """The reminder id does not belong to this appointment/clinic."""


def _as_utc(dt: datetime) -> datetime:
    """Naive timestamps (SQLite) are UTC; aware ones are converted to UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def _lock_appointment(session: AsyncSession, appointment: Appointment) -> Appointment:
    """Serialize lifecycle changes and reload clean fields, including stale ORM reads.

    Keep the caller's explicit edits (the new start/status on a reschedule or
    the requested PATCH status), but read the counter and other unchanged fields
    from the current row. Always lock the appointment before its reminders.
    SQLite ignores FOR UPDATE; Postgres holds it until the caller ends the transaction.
    """
    state = inspect(appointment)
    if state.pending or state.transient:
        await session.flush()
    changes = {attr.key: attr.value for attr in state.attrs if attr.history.has_changes()}
    with session.no_autoflush:
        current = await session.scalar(
            select(Appointment)
            .where(
                Appointment.id == appointment.id,
                Appointment.tenant_id == appointment.tenant_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    if current is None:
        raise ValueError("appointment does not belong to this tenant")
    for key, value in changes.items():
        setattr(current, key, value)
    return current


def _planned_dues(tenant: Tenant, start: datetime) -> list[tuple[str, datetime]]:
    """(kind, due) of the reminders an appointment starting at `start` gets, in send order.

    The extra ("custom") one is the clinic's "N dias antes, às HH:MM" on its local calendar
    (core/extra_reminder.py, spec 2026-10-09 §6.2); day/hour are fixed leads.
    """
    plan: list[tuple[str, datetime]] = []
    custom = custom_due_at(tenant, start)
    if custom is not None:
        plan.append((REMINDER_KIND_CUSTOM, custom))
    plan.append((REMINDER_KIND_DAY, start - _DAY_LEAD))
    plan.append((REMINDER_KIND_HOUR, start - _HOUR_LEAD))
    return plan


def _arm(row: AppointmentReminder, *, due: datetime, with_prompt: bool) -> None:
    """Initialise a fresh pending row's schedule fields."""
    row.status = REMINDER_STATUS_PENDING
    row.due_at = due
    row.with_prompt = with_prompt
    row.attempts = 0
    row.last_error_code = None
    row.sent_at = None
    row.answered_at = None
    row.answer = None
    row.warn_due_at = due + _WARN_AFTER[row.kind]
    row.warned_at = None
    row.warn_kind = None


async def schedule_reminders(
    session: AsyncSession,
    appointment: Appointment,
    tenant: Tenant,
    *,
    now: datetime,
) -> list[AppointmentReminder]:
    """Create the custom/day/hour rows of the appointment's CURRENT start.

    Returns only the rows created (or revived) by this call. Skips, silently:
    a clinic with the switch off, a block slot (no patient), a missing start, a
    terminal status, and every reminder whose due time is not in the future (a
    booking made too late never gets an already-late reminder). Idempotent: a
    active row that already exists for (appointment, kind, start) is left alone.
    A reschedule retires the old identities and creates fresh rows, even when
    the start is unchanged; old sent history remains intact.
    """
    if appointment.tenant_id != tenant.id:
        raise ValueError("appointment does not belong to this tenant")
    appointment = await _lock_appointment(session, appointment)
    if not tenant.reminders_v2_enabled:
        return []
    if appointment.patient_id is None or appointment.start_at is None:
        return []
    if not is_live_status(appointment.status):
        return []

    start = _as_utc(appointment.start_at)
    now_utc = _as_utc(now)
    with_prompt = appointment.confirmation_count < MAX_CONFIRMATIONS

    existing = {
        row.kind: row
        for row in await session.scalars(
            select(AppointmentReminder).where(
                AppointmentReminder.tenant_id == tenant.id,
                AppointmentReminder.appointment_id == appointment.id,
                AppointmentReminder.appointment_start_at == start,
                AppointmentReminder.invalidated_at.is_(None),
            )
        )
    }

    created: list[AppointmentReminder] = []
    for kind, due in _planned_dues(tenant, start):
        if due <= now_utc:
            continue
        row = existing.get(kind)
        if row is not None:
            continue
        row = AppointmentReminder(
            tenant_id=tenant.id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            kind=kind,
            appointment_start_at=start,
            channel=REMINDER_CHANNEL_WHATSAPP,
        )
        _arm(row, due=due, with_prompt=with_prompt)
        session.add(row)
        created.append(row)

    await session.flush()
    logger.info(
        "reminders_scheduled",
        appointment_id=str(appointment.id),
        tenant_id=str(tenant.id),
        kinds=[r.kind for r in created],
    )
    return created


async def cancel_reminders(session: AsyncSession, appointment_id: UUID, *, reason: str) -> int:
    """Cancel every not-yet-sent row of the appointment; returns how many.

    Rows that already went out (`sent`/`failed`) stay as history, but their
    pending clinic warning is cleared so nobody is warned about an appointment
    that no longer needs it. All identities are invalidated so old buttons and
    warnings never return when the same start is booked again. Takes no tenant:
    callers loaded the appointment tenant-scoped; its tenant also scopes each UPDATE. A row a
    worker is sending right now (`sending`) is cancelled too; R2's sender must
    re-check the status after the send and not resurrect it.
    """
    tenant_id = await session.scalar(
        select(Appointment.tenant_id).where(Appointment.id == appointment_id).with_for_update()
    )
    if tenant_id is None:
        return 0
    invalidated_at = datetime.now(UTC)
    result = await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.appointment_id == appointment_id,
            AppointmentReminder.tenant_id == tenant_id,
            AppointmentReminder.invalidated_at.is_(None),
            AppointmentReminder.status.in_((REMINDER_STATUS_PENDING, REMINDER_STATUS_SENDING)),
        )
        .values(status=REMINDER_STATUS_CANCELLED, warn_due_at=None, invalidated_at=invalidated_at)
        .execution_options(synchronize_session="fetch")
    )
    cancelled = result.rowcount or 0
    await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.appointment_id == appointment_id,
            AppointmentReminder.tenant_id == tenant_id,
            AppointmentReminder.invalidated_at.is_(None),
        )
        .values(warn_due_at=None, invalidated_at=invalidated_at)
        .execution_options(synchronize_session="fetch")
    )
    logger.info(
        "reminders_cancelled",
        appointment_id=str(appointment_id),
        reason=reason,
        cancelled=cancelled,
    )
    return cancelled


def reset_confirmation(appointment: Appointment) -> None:
    """Zero the confirmation counter (a moved booking is unconfirmed again)."""
    appointment.confirmation_count = 0
    appointment.first_confirmed_at = None
    appointment.last_confirmed_at = None


async def reschedule_reminders(
    session: AsyncSession,
    appointment: Appointment,
    tenant: Tenant,
    *,
    now: datetime,
) -> list[AppointmentReminder]:
    """The appointment moved: retire the old rows, zero the counter, recreate.

    Call AFTER `appointment.start_at` holds the new start. Does not touch
    `appointment.status` (the caller sets RESCHEDULED, as today). Works even
    when the clinic's switch is off (the counter is still zeroed; no rows).
    """
    if appointment.tenant_id != tenant.id:
        raise ValueError("appointment does not belong to this tenant")
    appointment = await _lock_appointment(session, appointment)
    await cancel_reminders(session, appointment.id, reason="rescheduled")
    reset_confirmation(appointment)
    return await schedule_reminders(session, appointment, tenant, now=now)


async def register_confirmation(
    session: AsyncSession,
    *,
    appointment: Appointment,
    reminder_id: UUID | None,
    source: str,
    now: datetime,
) -> int:
    """Count one confirmation; returns the appointment's new `confirmation_count`.

    * `source` is one of CONFIRMATION_SOURCE_* (anything else -> ValueError).
    * A live appointment only: a terminal one (cancelled/attended/no_show) is
      returned untouched, so a late tap never resurrects it.
    * With a `reminder_id` the row must belong to this clinic AND this
      appointment, else `ReminderMismatchError` (the caller answers generically,
      it must not leak whose row it is). A cancelled row or one of an older
      start (the booking moved since) is a stale tap: ignored, count unchanged.
      The same row counts once; a different row counts again until the cap of 2.
    * With `reminder_id=None` (staff, or a prompt that has no row) it counts only
      when the counter is 0: re-clicking "mark as confirmed" never reaches 2.
    * On the way the status becomes CONFIRMED (a logged transition) unless it
      already is. The caller commits.
    """
    log_source = _CONFIRMATION_SOURCES.get(source)
    if log_source is None:
        raise ValueError(f"unknown confirmation source: {source!r}")
    appointment = await _lock_appointment(session, appointment)
    count = appointment.confirmation_count
    if not is_live_status(appointment.status):
        return count
    now_utc = _as_utc(now)

    if reminder_id is not None:
        reminder = await session.scalar(
            select(AppointmentReminder)
            .where(
                AppointmentReminder.id == reminder_id,
                AppointmentReminder.tenant_id == appointment.tenant_id,
                AppointmentReminder.appointment_id == appointment.id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if reminder is None:
            raise ReminderMismatchError("reminder does not belong to this appointment")
        stale = (
            reminder.invalidated_at is not None
            or reminder.status == REMINDER_STATUS_CANCELLED
            or (
                appointment.start_at is not None
                and _as_utc(reminder.appointment_start_at) != _as_utc(appointment.start_at)
            )
        )
        if stale or reminder.answer == REMINDER_ANSWER_CONFIRM:
            return count
        reminder.answer = REMINDER_ANSWER_CONFIRM
        reminder.answered_at = now_utc
        counts = True
    else:
        counts = count == 0

    if counts and count < MAX_CONFIRMATIONS:
        count += 1
        appointment.confirmation_count = count
        if appointment.first_confirmed_at is None:
            appointment.first_confirmed_at = now_utc
        appointment.last_confirmed_at = now_utc

    previous = appointment.status
    if previous != AppointmentStatus.CONFIRMED:
        appointment.status = AppointmentStatus.CONFIRMED
        log_status_transition(
            appointment_id=appointment.id,
            tenant_id=appointment.tenant_id,
            old_status=previous,
            new_status=AppointmentStatus.CONFIRMED,
            source=log_source,
            idempotency_key=f"confirm:{appointment.id}:{count}",
        )
    appointment.updated_at = now_utc
    await session.flush()
    logger.info(
        "confirmation_registered",
        appointment_id=str(appointment.id),
        source=source,
        confirmation_count=count,
    )
    return count


def display_state(appointment, reminders: Iterable) -> str:
    """The state the agenda shows: unconfirmed / confirmed / confirmed_twice / attention.

    `attention` = the clinic was already warned for the CURRENT start and the
    patient has not confirmed. Terminal appointments (cancelled/attended/no_show)
    return `unconfirmed`: their colour comes from `status`, which the API sends
    alongside, and a dead appointment must never look red or green.
    """
    if not is_live_status(appointment.status):
        return DISPLAY_UNCONFIRMED
    count = appointment.confirmation_count
    if count >= MAX_CONFIRMATIONS:
        return DISPLAY_CONFIRMED_TWICE
    if count >= 1:
        return DISPLAY_CONFIRMED
    start = appointment.start_at
    for reminder in reminders:
        if getattr(reminder, "invalidated_at", None) is not None:
            continue
        if reminder.warned_at is None:
            continue
        if start is not None and _as_utc(reminder.appointment_start_at) != _as_utc(start):
            continue
        return DISPLAY_ATTENTION
    return DISPLAY_UNCONFIRMED


# --- TASK-032 R7: the rows behind the clinic's confirm / edit cards -----------------


def _check_staff_kind(kind: str) -> None:
    if kind not in REMINDER_KINDS_STAFF:
        raise ValueError(f"not a staff notice kind: {kind!r}")


async def current_staff_notice_row(
    session: AsyncSession, appointment: Appointment, *, kind: str
) -> AppointmentReminder | None:
    """The active `kind` row of the appointment's CURRENT start, or None."""
    _check_staff_kind(kind)
    if appointment.start_at is None:
        return None
    start = _as_utc(appointment.start_at)
    rows = await session.scalars(
        select(AppointmentReminder).where(
            AppointmentReminder.tenant_id == appointment.tenant_id,
            AppointmentReminder.appointment_id == appointment.id,
            AppointmentReminder.kind == kind,
            AppointmentReminder.invalidated_at.is_(None),
        )
    )
    return next((row for row in rows if _as_utc(row.appointment_start_at) == start), None)


async def ensure_staff_notice_row(
    session: AsyncSession,
    appointment: Appointment,
    *,
    kind: str,
    channel: str,
    with_prompt: bool,
    now: datetime,
) -> tuple[AppointmentReminder, bool]:
    """The staff card row of the current start: re-shown (False) or created (True).

    Born `sent` with NO clinic warning (`warn_due_at` NULL) - the R4 cron must never
    warn about a card the clinic itself sent. A re-shown row keeps its `answer`, so
    tapping Confirmar on the same card twice still counts once (R1 dedupes on the
    row). One active row per (appointment, kind, start) - R1's unique index. Flushes;
    the caller commits.
    """
    if appointment.start_at is None:
        raise ValueError("an appointment without a start has no card")
    row = await current_staff_notice_row(session, appointment, kind=kind)
    if row is not None:
        row.sent_at = now
        row.with_prompt = with_prompt
        await session.flush()
        return row, False
    row = AppointmentReminder(
        tenant_id=appointment.tenant_id,
        appointment_id=appointment.id,
        patient_id=appointment.patient_id,
        kind=kind,
        appointment_start_at=appointment.start_at,
        due_at=now,
        status=REMINDER_STATUS_SENT,
        channel=channel,
        with_prompt=with_prompt,
        sent_at=now,
        warn_due_at=None,
    )
    try:
        # The unique active-row index is the backstop when a competing insert
        # arrives after our lookup (and on SQLite, where FOR UPDATE is ignored).
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        winner = await current_staff_notice_row(session, appointment, kind=kind)
        if winner is None:
            raise
        return winner, False
    logger.info(
        "staff_notice_row_created",
        appointment_id=str(appointment.id),
        kind=kind,
    )
    return row, True


async def retire_staff_notice_row(
    session: AsyncSession, reminder_id: UUID, *, tenant_id: UUID
) -> None:
    """Retire a staff row whose card never reached the patient (a retry gets a fresh one)."""
    await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.id == reminder_id,
            AppointmentReminder.tenant_id == tenant_id,
        )
        .values(invalidated_at=datetime.now(UTC))
        .execution_options(synchronize_session=False)
    )


# --- TASK-044 R7 (spec 2026-10-09 §5.B): the clinic changed its extra-reminder setting ----

_PLANNED_KINDS = (REMINDER_KIND_CUSTOM, REMINDER_KIND_DAY, REMINDER_KIND_HOUR)


@dataclass(frozen=True)
class CustomReplan:
    """What `replan_custom_reminders` did (counts only - logged, never shown)."""

    moved: int = 0
    cancelled: int = 0
    created: int = 0


async def _move_pending_custom(
    session: AsyncSession, reminder_id: UUID, tenant_id: UUID, due: datetime
) -> int:
    """Re-arm one still-pending row at `due`; 0 when the engine claimed it meanwhile."""
    result = await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.id == reminder_id,
            AppointmentReminder.tenant_id == tenant_id,
            AppointmentReminder.status == REMINDER_STATUS_PENDING,
            AppointmentReminder.invalidated_at.is_(None),
        )
        .values(
            due_at=due,
            warn_due_at=due + _WARN_AFTER[REMINDER_KIND_CUSTOM],
            warned_at=None,
            warn_kind=None,
        )
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


async def _retire_pending_custom(
    session: AsyncSession, reminder_id: UUID, tenant_id: UUID, now_utc: datetime
) -> int:
    """Cancel + invalidate one still-pending row; 0 when the engine claimed it meanwhile."""
    result = await session.execute(
        update(AppointmentReminder)
        .where(
            AppointmentReminder.id == reminder_id,
            AppointmentReminder.tenant_id == tenant_id,
            AppointmentReminder.status == REMINDER_STATUS_PENDING,
            AppointmentReminder.invalidated_at.is_(None),
        )
        .values(status=REMINDER_STATUS_CANCELLED, warn_due_at=None, invalidated_at=now_utc)
        .execution_options(synchronize_session="fetch")
    )
    return result.rowcount or 0


async def replan_custom_reminders(
    session: AsyncSession, tenant: Tenant, *, now: datetime
) -> CustomReplan:
    """Put the clinic's CURRENT extra-reminder setting on the reminders already planned.

    Call AFTER the tenant holds the new `reminder_extra_days_before` /
    `reminder_extra_send_time` / `timezone` (TASK-048 R9, spec §6.2), inside the
    configuration save's transaction (flushes, never commits). The due time is
    core/extra_reminder.py::custom_due_at of each appointment's CURRENT start. For every
    live future appointment of the clinic that has a patient:

    * its `pending` `custom` row of the CURRENT start is re-armed in place at the new
      due time, or cancelled + invalidated when the setting was switched off or the new
      due time is not in the future (never sent late - same rule as schedule_reminders);
    * a row that already left (`sending`/`sent`/`failed`/`skipped`) stays as history;
    * with the setting on and no `custom` row, one is created - only when the appointment
      already has a planned `day`/`hour` row. An appointment with no plan at all is
      left to reminder_hooks.reconcile_missing_reminders, which plans every kind with
      the new lead (creating only `custom` there would make the cron skip it and lose
      its day/hour reminders).

    A clinic with the switch off has nothing planned and is left alone. Appointments
    are locked before their reminders (R1's rule); conditional UPDATEs never move a row
    the engine claimed meanwhile; a row a concurrent booking hook created first wins.
    """
    if not tenant.reminders_v2_enabled:
        return CustomReplan()
    now_utc = _as_utc(now)

    appointments = list(
        await session.scalars(
            select(Appointment)
            .where(
                Appointment.tenant_id == tenant.id,
                Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
                Appointment.patient_id.is_not(None),
                Appointment.start_at > now_utc,
            )
            .with_for_update()
        )
    )
    if not appointments:
        return CustomReplan()
    rows_by_appointment: dict[UUID, list[AppointmentReminder]] = {}
    for row in await session.scalars(
        select(AppointmentReminder).where(
            AppointmentReminder.tenant_id == tenant.id,
            AppointmentReminder.appointment_id.in_([a.id for a in appointments]),
            AppointmentReminder.kind.in_(_PLANNED_KINDS),
            AppointmentReminder.invalidated_at.is_(None),
        )
    ):
        rows_by_appointment.setdefault(row.appointment_id, []).append(row)

    moved = cancelled = created = 0
    for appointment in appointments:
        start = _as_utc(appointment.start_at)
        current = [
            row
            for row in rows_by_appointment.get(appointment.id, [])
            if _as_utc(row.appointment_start_at) == start
        ]
        custom = next((row for row in current if row.kind == REMINDER_KIND_CUSTOM), None)
        due = custom_due_at(tenant, start)
        if due is None or due <= now_utc:
            if custom is not None and custom.status == REMINDER_STATUS_PENDING:
                cancelled += await _retire_pending_custom(session, custom.id, tenant.id, now_utc)
            continue
        if custom is not None:
            if custom.status == REMINDER_STATUS_PENDING and _as_utc(custom.due_at) != due:
                moved += await _move_pending_custom(session, custom.id, tenant.id, due)
            continue
        if not current:
            continue  # never planned: the reconcile cron plans every kind with the new setting
        row = AppointmentReminder(
            tenant_id=tenant.id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            kind=REMINDER_KIND_CUSTOM,
            appointment_start_at=start,
            channel=REMINDER_CHANNEL_WHATSAPP,
        )
        _arm(row, due=due, with_prompt=appointment.confirmation_count < MAX_CONFIRMATIONS)
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            continue  # a concurrent booking hook planned it first
        created += 1

    logger.info(
        "custom_reminders_replanned",
        tenant_id=str(tenant.id),
        moved=moved,
        cancelled=cancelled,
        created=created,
    )
    return CustomReplan(moved=moved, cancelled=cancelled, created=created)
