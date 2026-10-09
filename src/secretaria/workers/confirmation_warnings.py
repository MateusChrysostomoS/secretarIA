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

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.config import get_settings
from secretaria.core import database as core_database
from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentReminder,
    Patient,
    Professional,
    Tenant,
)
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.appointment_reminder import (
    REMINDER_KIND_CHAT,
    REMINDER_STATUS_FAILED,
    REMINDER_STATUS_SENT,
    REMINDER_WARN_DELIVERY_FAILED,
    REMINDER_WARN_UNCONFIRMED,
)
from secretaria.services.email import send_confirmation_warning_alert

logger = get_logger(__name__)

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


# Which reminder the clinic is told about, in the clinic's words.
REMINDER_LABELS = {
    "custom": "lembrete antecipado",
    "day": "lembrete de 1 dia antes",
    "hour": "lembrete de 1 hora antes",
}
_NO_PROFESSIONAL = "o profissional da clínica"
_DEFAULT_TIMEZONE = "America/Sao_Paulo"


@dataclass
class WarningTickReport:
    candidates: int = 0
    claimed: int = 0
    emailed: int = 0
    no_address: int = 0
    email_failed: int = 0
    errors: int = 0


def _when_text(start_at: datetime | None, timezone: str | None) -> str:
    """`DD/MM/AAAA às HH:MM` in the CLINIC's zone (SQLite hands back naive UTC)."""
    if start_at is None:
        return "horário a confirmar"
    try:
        tz = ZoneInfo(timezone or _DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo(_DEFAULT_TIMEZONE)
    aware = start_at if start_at.tzinfo is not None else start_at.replace(tzinfo=UTC)
    return aware.astimezone(tz).strftime("%d/%m/%Y às %H:%M")


def _clinic_day(start_at: datetime | None, timezone: str | None) -> str | None:
    """`YYYY-MM-DD` of the start in the CLINIC's zone (the agenda's `?data=`)."""
    if start_at is None:
        return None
    try:
        tz = ZoneInfo(timezone or _DEFAULT_TIMEZONE)
    except Exception:
        tz = ZoneInfo(_DEFAULT_TIMEZONE)
    aware = start_at if start_at.tzinfo is not None else start_at.replace(tzinfo=UTC)
    return aware.astimezone(tz).strftime("%Y-%m-%d")


def _agenda_link(appointment_id, day: str | None = None) -> str | None:
    """The agenda URL the deployment configured + `consulta=<id>[&data=<day>]`; None when unset.

    `data` makes R5's agenda open the week that contains the appointment (the
    screen only selects an appointment inside the range it loaded).

    `DOCTOR_AGENDA_URL` is a full URL on purpose (the two frontends serve the
    agenda at different paths); a mail with no link is fine, a broken one is not.
    """
    base = (get_settings().DOCTOR_AGENDA_URL or "").strip()
    if not base:
        return None
    separator = "&" if "?" in base else "?"
    link = f"{base}{separator}consulta={appointment_id}"
    return f"{link}&data={day}" if day else link


def _patient_label(appointment: Appointment, patient: Patient | None) -> str:
    holder = ((patient.name if patient else None) or "").strip()
    attendee = (appointment.attendee_name or "").strip()
    if attendee and holder:
        return f"{attendee} (consulta marcada por {holder})"
    return attendee or holder or "O paciente"


async def _notify_clinic(candidate: WarningCandidate, report: WarningTickReport) -> None:
    """E-mail the clinic once for an appointment whose rows were just claimed.

    Best-effort by design: `warned_at` is already set (the agenda is red), so a
    missing address or a transport failure is logged and counted, never retried
    - a retry loop on a dead SMTP would mail the clinic a burst when it recovers.
    """
    async with core_database.async_session_factory() as session:
        tenant = await session.get(Tenant, candidate.tenant_id)
        appointment = await session.scalar(
            select(Appointment).where(
                Appointment.id == candidate.appointment_id,
                Appointment.tenant_id == candidate.tenant_id,
            )
        )
        if tenant is None or appointment is None:
            return
        patient = None
        if appointment.patient_id is not None:
            patient = await session.scalar(
                select(Patient).where(
                    Patient.id == appointment.patient_id, Patient.tenant_id == tenant.id
                )
            )
        professional_name = None
        if appointment.professional_id is not None:
            professional_name = await session.scalar(
                select(Professional.name).where(
                    Professional.id == appointment.professional_id,
                    Professional.tenant_id == tenant.id,
                )
            )

    to_email = (tenant.contact_email or "").strip()
    if not to_email:
        report.no_address += 1
        logger.info(
            "confirmation_warning_no_address",
            tenant_id=str(tenant.id),
            appointment_id=str(appointment.id),
        )
        return

    sent = await send_confirmation_warning_alert(
        to_email,
        clinic_name=tenant.clinic_name,
        warn_kind=candidate.warn_kind,
        patient_label=_patient_label(appointment, patient),
        professional_name=professional_name or _NO_PROFESSIONAL,
        service_name=appointment.appointment_type or "Consulta",
        when_text=_when_text(appointment.start_at, tenant.timezone),
        reminder_label=REMINDER_LABELS.get(candidate.kind, "lembrete"),
        agenda_link=_agenda_link(
            appointment.id, _clinic_day(appointment.start_at, tenant.timezone)
        ),
    )
    if sent:
        report.emailed += 1
        return
    report.email_failed += 1
    logger.error(
        "confirmation_warning_undelivered",
        alarm="confirmation_warning_undelivered",
        tenant_id=str(tenant.id),
        appointment_id=str(appointment.id),
    )


async def run_warning_tick(*, now: datetime) -> WarningTickReport:
    """Warn the clinic about every appointment whose deadline just passed.

    Claims are grouped per appointment inside ONE transaction, then ONE e-mail
    is sent for the group (the latest reminder), so a cron outage that leaves
    custom + day + hour all due does not mail the clinic three times. An error on
    one appointment is logged and counted and never stops the others.
    """
    report = WarningTickReport()
    async with core_database.async_session_factory() as session:
        candidates = await due_candidates(session, now)
    report.candidates = len(candidates)

    groups: dict[UUID, list[WarningCandidate]] = {}
    for candidate in candidates:
        groups.setdefault(candidate.appointment_id, []).append(candidate)

    for appointment_id, group in groups.items():
        try:
            claimed: list[WarningCandidate] = []
            async with core_database.async_session_factory() as session:
                async with session.begin():
                    for candidate in group:
                        if await claim_warning(session, candidate, now):
                            claimed.append(candidate)
            report.claimed += len(claimed)
            if claimed:
                await _notify_clinic(claimed[-1], report)
        except Exception as exc:
            report.errors += 1
            logger.warning(
                "confirmation_warning_item_failed",
                appointment_id=str(appointment_id),
                error_type=type(exc).__name__,
            )
    logger.info("confirmation_warning_tick", **asdict(report))
    return report


async def process_confirmation_warnings(ctx: dict) -> None:
    """arq cron (every minute, workers/arq_worker.py): warn the clinics."""
    await run_warning_tick(now=datetime.now(UTC))
