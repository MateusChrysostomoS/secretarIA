"""release_keep - the CLINIC accepts "Manter horário" on a released PAID slot (TASK-052).

Owner decision 2026-10-12 (3rd round): the patient's "Manter" only ASKS; the clinic decides in
the TASK-054 list. Accepting (only on an open `keep_requested`, before S, refund not marked)
re-creates: the released row is CANCELLED and its Google event deleted, and Google never lets a
deleted event id be used again (services/calendar.py::CalendarService.create_event), so a NEW
consultation on the same slot gets its own deterministic event id (`keep_event_id`) and the PAID
deposit is re-pointed (`release_offer.claim_keep`) in ONE transaction with the offer's UPDATE.

TAKEN (nothing changes; the clinic may refuse or try later) when, on the OWNING calendar, Google
has someone else's event over the slot, another conversation holds it, or the clinic has another
live consultation of the same professional (or of the clinic, without a professional) over it.
`CalendarService.is_slot_free` is NOT used: it hard-codes an 8-18 window.

Order: checks -> create the event -> INSERT + claim. Losing the claim rolls back and deletes the
event. The re-created row counts as confirmed (`confirmation_count = 1`). Never logs PII.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.core.logging import get_logger
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.models.appointment import LIVE_APPOINTMENT_STATUSES
from secretaria.models.deposit_release_offer import (
    DECISION_KEEP_REQUESTED,
    DECISION_KEPT,
    REFUND_CARRYABLE,
    DepositReleaseOffer,
)
from secretaria.services import booking_hold, booking_promotion, release_offer

logger = get_logger(__name__)

ACCEPT_KEPT = "kept"
ACCEPT_ALREADY = "already"
ACCEPT_TAKEN = "slot_taken"
ACCEPT_PAST = "slot_past"
ACCEPT_UNAVAILABLE = "calendar_unavailable"
ACCEPT_NOT_REQUESTED = "not_requested"
ACCEPT_REFUNDED = "refund_already_marked"


@dataclass(frozen=True)
class KeepResult:
    outcome: str
    appointment: Appointment | None = None


def keep_event_id(offer_id: UUID) -> str:
    """Deterministic Google event id: base32hex alphabet (a-v, 0-9), 5..1024 characters."""
    return f"relkeep{offer_id.hex}"


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


async def _consultation_overlaps(
    session: AsyncSession, released: Appointment, start: datetime, end: datetime
) -> bool:
    same_professional = (
        Appointment.professional_id == released.professional_id
        if released.professional_id is not None
        else Appointment.professional_id.is_(None)
    )
    found = await session.scalar(
        select(Appointment.id)
        .where(
            Appointment.tenant_id == released.tenant_id,
            Appointment.id != released.id,
            Appointment.status.in_(LIVE_APPOINTMENT_STATUSES),
            Appointment.start_at < end,
            Appointment.end_at > start,
            same_professional,
        )
        .limit(1)
    )
    return found is not None


async def _kept(session: AsyncSession, offer_id: UUID) -> KeepResult:
    offer = await session.get(DepositReleaseOffer, offer_id, populate_existing=True)
    if offer is not None and offer.decision == DECISION_KEPT and offer.new_appointment_id:
        return KeepResult(ACCEPT_ALREADY, await session.get(Appointment, offer.new_appointment_id))
    return KeepResult(ACCEPT_NOT_REQUESTED)


async def accept_keep(
    session: AsyncSession,
    *,
    tenant: Tenant,
    offer: DepositReleaseOffer,
    resolved_by: str | None,
    now: datetime | None = None,
) -> KeepResult:
    now = now or datetime.now(UTC)
    offer_id = offer.id
    if offer.tenant_id != tenant.id:
        return KeepResult(ACCEPT_NOT_REQUESTED)
    if offer.decision != DECISION_KEEP_REQUESTED:
        return await _kept(session, offer_id)
    if offer.refund_status not in REFUND_CARRYABLE:
        return KeepResult(ACCEPT_REFUNDED)
    released = await session.get(Appointment, offer.released_appointment_id)
    if released is None or released.tenant_id != tenant.id or released.start_at is None:
        return KeepResult(ACCEPT_NOT_REQUESTED)
    start, end = _utc(released.start_at), _utc(released.end_at or released.start_at)
    if start <= now:
        return KeepResult(ACCEPT_PAST)

    calendar = await booking_promotion.calendar_for_booking(
        session, tenant, released.professional_id
    )
    if calendar is None:
        return KeepResult(ACCEPT_UNAVAILABLE)
    own_event = keep_event_id(offer_id)
    try:
        events = await calendar.check_availability(start, end)
    except Exception as exc:  # noqa: BLE001 - Google down: nothing changes, the clinic retries
        logger.warning(
            "release_keep_calendar_failed", offer_id=str(offer_id), error_type=type(exc).__name__
        )
        return KeepResult(ACCEPT_UNAVAILABLE)
    if any(str(event.get("id")) != own_event for event in events):
        return KeepResult(ACCEPT_TAKEN)
    if await booking_hold.live_hold_overlaps(
        session, tenant.id, start, end, professional_id=released.professional_id
    ):
        return KeepResult(ACCEPT_TAKEN)
    if await _consultation_overlaps(session, released, start, end):
        return KeepResult(ACCEPT_TAKEN)

    patient = await session.get(Patient, released.patient_id) if released.patient_id else None
    display = released.attendee_name or (patient.name if patient is not None else None)
    service = released.appointment_type or "Consulta"
    try:
        event = await calendar.create_event(
            start=start,
            end=end,
            summary=f"{service} - {display}" if display else service,
            event_id=own_event,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "release_keep_create_failed", offer_id=str(offer_id), error_type=type(exc).__name__
        )
        return KeepResult(ACCEPT_UNAVAILABLE)

    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=released.patient_id,
        conversation_id=released.conversation_id,
        google_event_id=str(event.get("id") or own_event),
        google_calendar_source=released.google_calendar_source,
        google_event_link=event.get("htmlLink"),
        appointment_type=released.appointment_type,
        start_at=released.start_at,
        end_at=released.end_at,
        phone=released.phone,
        professional_id=released.professional_id,
        unit_id=released.unit_id,
        insurance=released.insurance,
        insurance_plan_id=released.insurance_plan_id,
        insurance_professional_plan_id=released.insurance_professional_plan_id,
        attendee_name=released.attendee_name,
        status=AppointmentStatus.SCHEDULED,
        confirmation_count=1,
        first_confirmed_at=now,
        last_confirmed_at=now,
    )
    session.add(appointment)
    await session.flush()
    if not await release_offer.claim_keep(
        session, offer=offer, new_appointment=appointment, resolved_by=resolved_by, now=now
    ):
        await session.rollback()
        try:
            await calendar.cancel_event(own_event)
        except Exception as exc:  # noqa: BLE001 - logged; the event is ours and empty
            logger.error(
                "release_keep_orphan_event", offer_id=str(offer_id), error_type=type(exc).__name__
            )
        return await _kept(session, offer_id)
    await session.commit()
    logger.info("release_keep_accepted", offer_id=str(offer_id), appointment_id=str(appointment.id))
    return KeepResult(ACCEPT_KEPT, appointment)
