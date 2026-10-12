"""Shared helpers for the TASK-052 tests (a released appointment with a deposit). Not a test
module: one clinic, one patient, one conversation, the released (CANCELLED) appointment and its
deposit, on the in-memory SQLite database of tests/payments_support.py.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.models.deposit_release_offer import DepositReleaseEmail
from tests.before_support import make_world

# T: Tuesday 2030-03-05, 14:00 in Sao Paulo. S: the released consultation, Friday 08/03, 10:00.
T = datetime(2030, 3, 5, 17, 0, tzinfo=UTC)
RELEASED_START = datetime(2030, 3, 8, 13, 0, tzinfo=UTC)
# Closes at 22:00 on Tuesdays: F = 21:30, B = min(T + 6 h, F) = 20:00 local = T + 6 h.
OPEN_LATE = {"tuesday": [{"start": "08:00", "end": "22:00"}]}
# Closes at 18:00 on Tuesdays: F = 17:30 local = 20:30 UTC = T + 3 h 30 min.
CLOSES_AT_SIX = {"tuesday": [{"start": "08:00", "end": "18:00"}]}


def utc(value: datetime | None) -> datetime | None:
    """SQLite reads timestamps back naive; every instant here is UTC."""
    if value is None:
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


@dataclass
class Released:
    tenant: Tenant
    patient: Patient
    conversation: Conversation
    appointment: Appointment
    deposit: PixDeposit

    @property
    def tenant_id(self) -> UUID:
        return self.tenant.id


async def make_released(
    session,
    *,
    channel: str = "whatsapp",
    deposit_status: PixDepositStatus = PixDepositStatus.PAID,
    amount_cents: int = 6000,
    timing: str = "after",
    business_hours: dict | None = None,
    professional_hours: dict | None = None,
    start: datetime = RELEASED_START,
    **tenant_kw,
) -> Released:
    """A clinic whose appointment was just RELEASED (status CANCELLED) with a deposit.

    `professional_hours` given: the appointment belongs to a doctor with those hours."""
    tenant_kw.setdefault("contact_email", "clinica@exemplo.com")
    world = await make_world(
        session,
        channel=channel,
        timing=timing,
        business_hours=OPEN_LATE if business_hours is None else business_hours,
        **tenant_kw,
    )
    professional_id = None
    if professional_hours is not None:
        professional = Professional(
            id=uuid4(),
            tenant_id=world.tenant_id,
            name="Dra. Ana",
            business_hours=professional_hours,
        )
        session.add(professional)
        await session.flush()
        professional_id = professional.id
    appointment = Appointment(
        id=uuid4(),
        tenant_id=world.tenant_id,
        patient_id=world.patient.id,
        conversation_id=world.conversation.id,
        google_event_id=f"evt-{uuid4().hex[:8]}",
        appointment_type="Consulta",
        phone=world.patient.wa_id,
        professional_id=professional_id,
        status=AppointmentStatus.CANCELLED,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )
    session.add(appointment)
    await session.flush()
    deposit = PixDeposit(
        id=uuid4(),
        tenant_id=world.tenant_id,
        appointment_id=appointment.id,
        patient_id=world.patient.id,
        asaas_payment_id=f"pay_{uuid4().hex[:10]}",
        provider="asaas",
        provider_charge_id=None,
        amount_cents=amount_cents,
        percent_applied=30,
        status=deposit_status,
        paid_at=datetime(2030, 3, 1, 12, 0, tzinfo=UTC)
        if deposit_status is PixDepositStatus.PAID
        else None,
    )
    deposit.provider_charge_id = deposit.asaas_payment_id
    session.add(deposit)
    await session.commit()
    return Released(world.tenant, world.patient, world.conversation, appointment, deposit)


async def new_appointment(session, released: Released, *, start: datetime) -> Appointment:
    """A fresh SCHEDULED appointment of the same patient (what a rebooking/keep commit creates)."""
    appointment = Appointment(
        id=uuid4(),
        tenant_id=released.tenant_id,
        patient_id=released.patient.id,
        conversation_id=released.conversation.id,
        google_event_id=f"evt-{uuid4().hex[:8]}",
        appointment_type="Consulta",
        phone=released.patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
        start_at=start,
        end_at=start + timedelta(minutes=30),
    )
    session.add(appointment)
    await session.flush()
    return appointment


async def emails(session, offer_id: UUID) -> list[DepositReleaseEmail]:
    return list(
        await session.scalars(
            select(DepositReleaseEmail)
            .where(DepositReleaseEmail.offer_id == offer_id)
            .order_by(DepositReleaseEmail.due_at, DepositReleaseEmail.kind, DepositReleaseEmail.seq)
        )
    )
