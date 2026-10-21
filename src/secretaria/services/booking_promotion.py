"""Turning a paid reservation into a consultation (spec sections 4.2 item 2, 9 and 10).

In `before` timing the appointment does not exist until the deposit is paid. This module is the
promotion core, free of any inbound message: the caller is a payment (`fulfillment.py`, driven
by a webhook, the reconciliation job or a sweep), never a turn of the conversation.

The promotion is IDEMPOTENT by construction, which is the whole point:

* the Google event id is DETERMINISTIC (the deposit id in hex), so a retry - a replayed
  webhook, the sweep after a crash - finds the event it already created instead of duplicating
  it (`CalendarService.create_event(event_id=...)` treats Google's 409 as success);
* the `Appointment` row is written in the same transaction as the atomic claim
  `UPDATE pix_deposits SET appointment_id = :id WHERE appointment_id IS NULL`: two workers can
  both insert a row, exactly ONE claim wins and the loser rolls its row back;
* before anything is created the slot is REVALIDATED against Google and against the live holds
  of other reservations (a doctor may have typed an event straight into Google, or a late
  payment may find the slot gone): a taken slot is never double-booked, it becomes an attention
  item for the clinic (`fulfillment.py`).

Nothing here sends a message or enqueues a hook: `promote` returns what happened and the
caller does the rest, each step behind its own idempotency guard.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    PixDeposit,
    Professional,
    Tenant,
)
from secretaria.services import booking_hold
from secretaria.services.calendar import CalendarService, build_patient_calendar_link
from secretaria.services.insurance_catalog import resolve_booking_plan_ids
from secretaria.services.local_calendar import LocalCalendarService, create_event_exclusive
from secretaria.services.tenant_config import load_tenant_config, resolve_professional_calendar

logger = get_logger(__name__)


@dataclass(frozen=True)
class BookingSnapshot:
    """What the promotion needs, read from `pix_deposits.booking_snapshot` (the hold may
    already have been swept by another conversation). Never carries the patient's name."""

    tenant_id: UUID
    conversation_id: UUID | None
    patient_id: UUID | None
    professional_id: UUID | None
    appointment_type: str
    insurance: str | None
    attendee_name: str | None
    start_at: datetime
    end_at: datetime

    @classmethod
    def from_deposit(cls, deposit: PixDeposit) -> "BookingSnapshot | None":
        """None when the snapshot is missing or unreadable (an orphan payment, spec 10)."""
        raw = deposit.booking_snapshot
        if not isinstance(raw, dict):
            return None
        try:
            start_at = datetime.fromisoformat(raw["start_at"])
            end_at = datetime.fromisoformat(raw["end_at"])
            professional_id = raw.get("professional_id")
            conversation_id = raw.get("conversation_id")
            appointment_type = str(raw.get("appointment_type") or "Consulta")
        except (KeyError, TypeError, ValueError):
            return None
        return cls(
            tenant_id=deposit.tenant_id,
            conversation_id=UUID(conversation_id) if conversation_id else None,
            patient_id=deposit.patient_id,
            professional_id=UUID(professional_id) if professional_id else None,
            appointment_type=appointment_type,
            insurance=raw.get("insurance"),
            attendee_name=raw.get("attendee_name"),
            start_at=start_at,
            end_at=end_at,
        )


def event_id_for(deposit_id: UUID, *, calendar: CalendarService | None = None) -> str:
    """The deterministic Google event id of a deposit: 32 lowercase hex characters, inside
    Google's base32hex alphabet (a-v, 0-9) and its 5..1024 length rule."""
    if isinstance(calendar, LocalCalendarService):
        return f"loc_{deposit_id.hex}"
    return deposit_id.hex


async def calendar_for_booking(
    session: AsyncSession, tenant: Tenant, professional_id: UUID | None
) -> CalendarService | None:
    """The calendar the booking belongs to (the professional's own, else the clinic's), or None
    when it cannot be built. The same construction `workers/tasks.py::_appointment_calendar`
    uses, without the worker context."""
    try:
        config = await load_tenant_config(session, tenant)
        if professional_id is not None:
            professional = await session.get(Professional, professional_id)
            if professional is None or professional.tenant_id != tenant.id:
                return None
            return await resolve_professional_calendar(
                session, tenant, professional, tenant_config=config
            )
        if not config:
            return None
        return CalendarService.from_tenant_config(config)
    except Exception as exc:  # noqa: BLE001
        logger.warning("promotion_calendar_failed", error_type=type(exc).__name__)
        return None


async def slot_conflict(
    session: AsyncSession,
    calendar: CalendarService,
    snapshot: BookingSnapshot,
    *,
    deposit: PixDeposit,
) -> str | None:
    """Why the slot is no longer bookable, or None when it is free.

    `"past"`: the slot has already started. `"calendar"`: an event of somebody else overlaps
    it on Google (OUR deterministic event does
    not count: it is the retry of a promotion that already got that far). `"hold"`: another
    reservation holds an overlapping window. Raises `CalendarUnavailableError` untouched: the
    caller retries, it does not guess.
    """
    start = snapshot.start_at
    started = start if start.tzinfo else start.replace(tzinfo=UTC)
    if started <= datetime.now(UTC):
        # A payment that arrives after the slot began (a Pix that outlived it, or a late payment
        # of a voided charge) must never create a consultation in the past: the clinic decides.
        return "past"
    own_event = event_id_for(deposit.id, calendar=calendar)
    events = await calendar.check_availability(snapshot.start_at, snapshot.end_at)
    if any(event.get("id") != own_event for event in events):
        return "calendar"
    if await booking_hold.live_hold_overlaps(
        session,
        snapshot.tenant_id,
        snapshot.start_at,
        snapshot.end_at,
        professional_id=snapshot.professional_id,
        exclude_charge_id=deposit.id,
    ):
        return "hold"
    return None


@dataclass(frozen=True)
class Promotion:
    appointment: Appointment
    created: bool  # False: a previous attempt already made it (retry)


async def promote(
    session: AsyncSession,
    tenant: Tenant,
    deposit: PixDeposit,
    snapshot: BookingSnapshot,
    calendar: CalendarService,
) -> Promotion:
    """Create the event (deterministic id) and the appointment, and claim them on the deposit.

    Commits. If the deposit already points at an appointment the call is a no-op returning it.
    A `CalendarUnavailableError` from Google propagates before anything is written.
    """
    if deposit.appointment_id is not None:
        existing = await session.get(Appointment, deposit.appointment_id)
        if existing is not None:
            return Promotion(existing, created=False)

    patient = await session.get(Patient, snapshot.patient_id) if snapshot.patient_id else None
    display = snapshot.attendee_name or (patient.name if patient is not None else None)
    summary = (
        f"{snapshot.appointment_type} - {display}" if display else snapshot.appointment_type
    )
    event = await create_event_exclusive(
        calendar,
        start=snapshot.start_at,
        end=snapshot.end_at,
        summary=summary,
        event_id=event_id_for(deposit.id, calendar=calendar),
    )
    google_event_id = event.get("id") or event_id_for(deposit.id, calendar=calendar)

    plan_id, professional_plan_id = await resolve_booking_plan_ids(
        session, tenant.id, snapshot.insurance, snapshot.professional_id
    )
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=snapshot.patient_id,
        conversation_id=snapshot.conversation_id,
        google_event_id=google_event_id,
        google_event_link=event.get("htmlLink"),
        appointment_type=snapshot.appointment_type[:120],
        start_at=snapshot.start_at,
        end_at=snapshot.end_at,
        # A Portal patient has no phone (`appointments.phone` is VARCHAR(32): never a UUID).
        phone=patient.wa_id if patient is not None else None,
        professional_id=snapshot.professional_id,
        insurance=snapshot.insurance,
        insurance_plan_id=plan_id,
        insurance_professional_plan_id=professional_plan_id,
        attendee_name=snapshot.attendee_name,
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(appointment)
    await session.flush()
    claimed = await session.execute(
        update(PixDeposit)
        .where(
            PixDeposit.id == deposit.id,
            PixDeposit.tenant_id == tenant.id,
            PixDeposit.appointment_id.is_(None),
        )
        .values(appointment_id=appointment.id, calendar_event_id=google_event_id)
        .returning(PixDeposit.id)
        .execution_options(synchronize_session=False)
    )
    if claimed.scalar_one_or_none() is None:
        # Another worker got there first: its appointment stands, ours is discarded.
        await session.rollback()
        await session.refresh(deposit)  # a rollback expires every loaded row
        existing = (
            await session.get(Appointment, deposit.appointment_id)
            if deposit.appointment_id
            else None
        )
        if existing is None:  # pragma: no cover - the winner's commit is atomic with its claim
            raise RuntimeError("promotion claim lost but no appointment found")
        return Promotion(existing, created=False)
    await session.commit()
    await session.refresh(deposit)
    return Promotion(appointment, created=True)


def confirmation_text(
    tenant: Tenant, snapshot: BookingSnapshot, patient_name: str | None, *, late_reopened: bool
) -> str:
    """The "consulta confirmada" bubble, sent only once the payment was identified."""
    from zoneinfo import ZoneInfo

    try:
        tz = ZoneInfo(tenant.timezone or "America/Sao_Paulo")
    except Exception:  # noqa: BLE001
        tz = ZoneInfo("America/Sao_Paulo")
    local_start = snapshot.start_at.astimezone(tz)
    local_end = snapshot.end_at.astimezone(tz)
    display = snapshot.attendee_name or patient_name
    summary = f"{snapshot.appointment_type} - {display}" if display else snapshot.appointment_type
    lines = []
    if late_reopened:
        from secretaria.services.payments.deposit_lifecycle import before_late_reopened_text

        lines.append(before_late_reopened_text())
    lines.append(
        "Pagamento recebido! Seu agendamento está confirmado. ✅\n\n"
        f"{snapshot.appointment_type}\n"
        + (f"Paciente: {snapshot.attendee_name}\n" if snapshot.attendee_name else "")
        + f"{local_start.strftime('%d/%m/%Y às %H:%M')}\n\n"
        "Adicionar à sua agenda:\n"
        f"{build_patient_calendar_link(local_start, local_end, summary, tz=tz)}"
    )
    return "\n\n".join(lines)


__all__ = [
    "BookingSnapshot",
    "Promotion",
    "calendar_for_booking",
    "confirmation_text",
    "event_id_for",
    "promote",
    "slot_conflict",
]
