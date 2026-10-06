"""The reminder engine: send what `appointment_reminders` planned (TASK-032 R2).

Spec §4.2. R1's services/reminder_schedule.py writes one row per planned
reminder; this cron (`process_appointment_reminders`, every minute) sends the
due ones. Per tick:

1. `due_reminder_ids` - pending, due, not `chat`, of clinics with
   `reminders_v2_enabled`. A clinic with the switch OFF is never even read
   (plugins/reminders.py keeps serving it exactly as before).
2. Entitlement gate per clinic, the old cron's rule: subscription active and
   secretarIA enabled. A gated row is NOT claimed - it waits, and the lateness
   guard retires it if the clinic stays gated for too long.
3. `claim_reminder` - `UPDATE ... SET status='sending', attempts=attempts+1
   WHERE id=:id AND status='pending'`. Only the statement that flips the row
   sees rowcount 1 (Postgres re-checks the WHERE after the row lock), so two
   worker copies - or one holding a stale list - can never both send.
4. `_prepare` - re-reads the row with its appointment and closes it without
   sending when the appointment is gone or terminal, was moved since the row
   was planned (`appointment_start_at` is the version), already started, or
   the reminder is too late to be useful; skips an opted-out patient.
5. services/reminder_delivery.py::deliver_reminder - the channel decision.
6. `_finish` - books the outcome.

Nothing here logs patient content: ids, kinds and codes only.
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select, update

from secretaria.config import get_settings
from secretaria.core import database as core_database
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    Conversation,
    Patient,
    PixDepositStatus,
    Tenant,
    is_live_status,
)
from secretaria.models.appointment_reminder import (
    REMINDER_KIND_CHAT,
    REMINDER_KIND_CUSTOM,
    REMINDER_KIND_DAY,
    REMINDER_KIND_HOUR,
    REMINDER_STATUS_CANCELLED,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_PENDING,
    REMINDER_STATUS_SENDING,
    REMINDER_STATUS_SENT,
    REMINDER_STATUS_SKIPPED,
    REMINDER_WARN_DELIVERY_FAILED,
)
from secretaria.services import cancellation_notice
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.entitlements_client import EntitlementSummary, get_entitlements
from secretaria.services.payments import deposit_lifecycle
from secretaria.services.reminder_delivery import (
    DeliveryOutcome,
    ReminderJob,
    deliver_reminder,
)
from secretaria.services.reminder_schedule import MAX_CONFIRMATIONS
from secretaria.services.reminder_text import load_reminder_content
from secretaria.services.tenant_config import get_waba_token

logger = get_logger(__name__)

# Same budget as the cancellation notice (workers/whatsapp/notifications.py::
# CANCEL_NOTICE_MAX_TRIES): 4 attempts, ~1 minute apart (one per tick).
MAX_ATTEMPTS = 4
# A reminder that would arrive this late is noise, not help. Never later than
# the appointment itself either (`appointment_started`).
MAX_LATENESS: dict[str, timedelta] = {
    REMINDER_KIND_CUSTOM: timedelta(hours=6),
    REMINDER_KIND_DAY: timedelta(hours=3),
    REMINDER_KIND_HOUR: timedelta(minutes=30),
}
DEFAULT_MAX_LATENESS = timedelta(minutes=30)


@dataclass
class TickReport:
    claimed: int = 0
    sent: int = 0
    retried: int = 0
    failed: int = 0
    closed: int = 0
    deferred: int = 0

    def add(self, result: str) -> None:
        setattr(self, result, getattr(self, result) + 1)


def _as_utc(dt: datetime) -> datetime:
    """Naive timestamps (SQLite) are UTC; aware ones are converted to UTC."""
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


async def due_reminder_ids(now: datetime, *, limit: int) -> list[tuple[UUID, UUID]]:
    """(reminder id, tenant id) of the rows this tick may try, oldest first."""
    async with core_database.async_session_factory() as session:
        rows = await session.execute(
            select(AppointmentReminder.id, AppointmentReminder.tenant_id)
            .join(Tenant, Tenant.id == AppointmentReminder.tenant_id)
            .where(
                Tenant.reminders_v2_enabled.is_(True),
                AppointmentReminder.status == REMINDER_STATUS_PENDING,
                AppointmentReminder.kind != REMINDER_KIND_CHAT,
                AppointmentReminder.due_at <= now,
            )
            .order_by(AppointmentReminder.due_at)
            .limit(limit)
        )
        return [(row.id, row.tenant_id) for row in rows]


async def claim_reminder(reminder_id: UUID) -> int | None:
    """Flip one row pending -> sending. The attempt number, or None if another claim won."""
    async with core_database.async_session_factory() as session:
        async with session.begin():
            result = await session.execute(
                update(AppointmentReminder)
                .where(
                    AppointmentReminder.id == reminder_id,
                    AppointmentReminder.status == REMINDER_STATUS_PENDING,
                )
                .values(
                    status=REMINDER_STATUS_SENDING,
                    attempts=func.coalesce(AppointmentReminder.attempts, 0) + 1,
                )
                .execution_options(synchronize_session=False)
            )
            if result.rowcount != 1:
                return None
            return await session.scalar(
                select(AppointmentReminder.attempts).where(AppointmentReminder.id == reminder_id)
            )


def _guard(reminder: AppointmentReminder, appointment: Appointment | None, now: datetime):
    """(status, code) that closes the row without sending, or None to go ahead."""
    if appointment is None or appointment.tenant_id != reminder.tenant_id:
        return REMINDER_STATUS_CANCELLED, "appointment_gone"
    if not is_live_status(appointment.status):
        return REMINDER_STATUS_CANCELLED, "appointment_closed"
    if appointment.start_at is None or _as_utc(appointment.start_at) != _as_utc(
        reminder.appointment_start_at
    ):
        return REMINDER_STATUS_CANCELLED, "stale_version"
    if _as_utc(appointment.start_at) <= now:
        return REMINDER_STATUS_SKIPPED, "appointment_started"
    if now - _as_utc(reminder.due_at) > MAX_LATENESS.get(reminder.kind, DEFAULT_MAX_LATENESS):
        return REMINDER_STATUS_SKIPPED, "too_late"
    return None


def _close(reminder: AppointmentReminder, status: str, code: str) -> None:
    """Retire the row without sending; nothing to warn the clinic about."""
    reminder.status = status
    reminder.last_error_code = code
    reminder.warn_due_at = None
    logger.info("reminder_v2_closed", reminder_id=str(reminder.id), status=status, code=code)


def _fail(reminder: AppointmentReminder, code: str, now: datetime) -> None:
    """Definitive delivery failure: R4 warns the clinic (warn_kind delivery_failed)."""
    reminder.status = REMINDER_STATUS_FAILED
    reminder.last_error_code = code[:64]
    reminder.warn_kind = REMINDER_WARN_DELIVERY_FAILED
    reminder.warn_due_at = now
    logger.error(
        "reminder_v2_undelivered",
        alarm="reminder_undelivered",
        reminder_id=str(reminder.id),
        tenant_id=str(reminder.tenant_id),
        code=reminder.last_error_code,
    )


async def _prepare(reminder_id: UUID, attempt: int, now: datetime) -> ReminderJob | None:
    """Load and guard one claimed row. None = it was closed here; nothing to send."""
    async with core_database.async_session_factory() as session:
        async with session.begin():
            reminder = await session.get(AppointmentReminder, reminder_id)
            if reminder is None:
                return None
            appointment = await session.get(Appointment, reminder.appointment_id)
            closing = _guard(reminder, appointment, now)
            if closing is not None:
                _close(reminder, *closing)
                return None
            tenant = await session.get(Tenant, reminder.tenant_id)
            patient = (
                await session.get(Patient, reminder.patient_id)
                if reminder.patient_id is not None
                else None
            )
            if tenant is None or patient is None or patient.tenant_id != reminder.tenant_id:
                _fail(reminder, "no_patient", now)
                return None
            if patient.reminder_opt_out:
                _close(reminder, REMINDER_STATUS_SKIPPED, "opt_out")
                return None
            content = await load_reminder_content(session, tenant, appointment)
            deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
            conversation_id = await session.scalar(
                select(Conversation.id)
                .where(Conversation.tenant_id == tenant.id, Conversation.patient_id == patient.id)
                .order_by(Conversation.created_at.desc())
                .limit(1)
            )
            portal = patient.channel == CHANNEL_BRAIN_MESSAGE
            last_inbound = (
                None
                if portal
                else await cancellation_notice.last_inbound_at(session, tenant.id, patient.id)
            )
            waba_token = None if portal else await get_waba_token(session, tenant.id)
            return ReminderJob(
                reminder_id=reminder.id,
                kind=reminder.kind,
                attempt=attempt,
                tenant=tenant,
                patient=patient,
                appointment_id=appointment.id,
                content=content,
                # Recomputed now, not trusted from planning time: two
                # confirmations since then silence the prompt (spec §4.2 "Parada").
                with_prompt=bool(reminder.with_prompt)
                and (appointment.confirmation_count or 0) < MAX_CONFIRMATIONS,
                deposit_paid=deposit is not None and deposit.status == PixDepositStatus.PAID,
                conversation_id=conversation_id,
                waba_token=waba_token,
                last_inbound_at=last_inbound,
                now=now,
            )


async def _finish(
    reminder_id: UUID, job: ReminderJob, outcome: DeliveryOutcome, now: datetime
) -> str:
    """Book the outcome. Returns the TickReport field to count it under."""
    async with core_database.async_session_factory() as session:
        async with session.begin():
            reminder = await session.get(AppointmentReminder, reminder_id)
            if reminder is None:
                return "closed"
            if outcome.ok:
                reminder.status = REMINDER_STATUS_SENT
                reminder.sent_at = now
                reminder.channel = outcome.channel
                reminder.with_prompt = job.with_prompt
                reminder.last_error_code = outcome.error_code
                return "sent"
            reminder.status = REMINDER_STATUS_PENDING
            reminder.last_error_code = outcome.error_code
            return "retried"


async def _release_after_crash(reminder_id: UUID, attempt: int, now: datetime) -> None:
    """A bug between claim and finish must not strand the row in 'sending'."""
    try:
        async with core_database.async_session_factory() as session:
            async with session.begin():
                reminder = await session.get(AppointmentReminder, reminder_id)
                if reminder is None or reminder.status != REMINDER_STATUS_SENDING:
                    return
                if attempt >= MAX_ATTEMPTS:
                    _fail(reminder, "engine_error", now)
                else:
                    reminder.status = REMINDER_STATUS_PENDING
                    reminder.last_error_code = "engine_error"
    except Exception as exc:
        logger.warning(
            "reminder_v2_release_failed",
            reminder_id=str(reminder_id),
            error_type=type(exc).__name__,
        )


async def run_reminder_tick(*, now: datetime, redis=None) -> TickReport:
    """One pass over the due rows. One bad row never stops the others."""
    report = TickReport()
    entitlements: dict[UUID, EntitlementSummary | None] = {}
    limit = get_settings().REMINDER_V2_BATCH_SIZE
    for reminder_id, tenant_id in await due_reminder_ids(now, limit=limit):
        attempt: int | None = None
        try:
            if tenant_id not in entitlements:
                entitlements[tenant_id] = await get_entitlements(tenant_id, redis)
            summary = entitlements[tenant_id]
            if summary is None or not summary.active or not summary.secretaria_enabled:
                report.add("deferred")
                continue
            attempt = await claim_reminder(reminder_id)
            if attempt is None:
                continue
            report.add("claimed")
            job = await _prepare(reminder_id, attempt, now)
            if job is None:
                report.add("closed")
                continue
            outcome = await deliver_reminder(job)
            report.add(await _finish(reminder_id, job, outcome, now))
        except Exception as exc:
            logger.warning(
                "reminder_v2_item_failed",
                reminder_id=str(reminder_id),
                error_type=type(exc).__name__,
            )
            if attempt is not None:
                await _release_after_crash(reminder_id, attempt, now)
    logger.info("reminder_v2_tick", **asdict(report))
    return report


async def process_appointment_reminders(ctx: dict) -> None:
    """arq cron (every minute, workers/arq_worker.py): send the due reminders."""
    await run_reminder_tick(now=datetime.now(UTC), redis=ctx.get("redis"))
