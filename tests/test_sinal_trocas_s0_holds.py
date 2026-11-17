"""TASK-085 R S0 - the hold of the NEW window of a change waiting for the Pix of the difference.

A "before" clinic keeps the old slot (the appointment row stays live) and guards the new one with
a payment-phase hold that belongs to no conversation: it belongs to the `DepositAdjustment`
(`booking_holds.adjustment_id`). These tests pin the five pieces every later slice builds on:
`held_windows` stays NULL-safe, `place_difference_hold_in` / `release_difference_hold_in` /
`extend_difference_hold_in` / `difference_hold_in`, and `live_hold_overlaps(exclude_adjustment_id)`.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select

from secretaria.models import Appointment, AppointmentStatus, BookingHold, PixDeposit
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    MODE_SUSPEND,
    STATUS_AWAITING_PAYMENT,
    DepositAdjustment,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import booking_hold as holds
from tests.before_support import make_world
from tests.payments_support import make_sessionmaker

START = datetime.now(UTC).replace(microsecond=0) + timedelta(days=3)
END = START + timedelta(minutes=60)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(holds, "async_session_factory", maker)
    yield maker
    await engine.dispose()


async def _adjustment(session, world, *, minutes_ahead: int = 0) -> DepositAdjustment:
    """A suspended dearer change of a paid sinal (the row a difference hold belongs to)."""
    appointment = Appointment(
        id=uuid4(),
        tenant_id=world.tenant_id,
        patient_id=world.patient.id,
        google_event_id=f"evt-{uuid4().hex[:8]}",
        phone=world.patient.wa_id,
        appointment_type="Consulta",
        start_at=START - timedelta(days=1),
        end_at=START - timedelta(days=1) + timedelta(minutes=30),
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(appointment)
    await session.flush()
    deposit = PixDeposit(
        tenant_id=world.tenant_id,
        appointment_id=appointment.id,
        patient_id=world.patient.id,
        amount_cents=6000,
        percent_applied=30,
        status=PixDepositStatus.PAID,
        provider="asaas",
        provider_charge_id=f"pay_{uuid4().hex[:8]}",
        deposit_timing="before",
    )
    session.add(deposit)
    await session.flush()
    adjustment = DepositAdjustment(
        id=uuid4(),
        tenant_id=world.tenant_id,
        deposit_id=deposit.id,
        appointment_id=appointment.id,
        kind=KIND_INCREASE,
        mode=MODE_SUSPEND,
        status=STATUS_AWAITING_PAYMENT,
        old_service="Consulta",
        new_service="Avaliação",
        diff_cents=3000,
        provider_charge_id=f"pay_diff_{uuid4().hex[:8]}",
    )
    session.add(adjustment)
    await session.flush()
    return adjustment


async def _place(session, world, adjustment, **overrides):
    args = {
        "tenant_id": world.tenant_id,
        "adjustment_id": adjustment.id,
        "patient_id": world.patient.id,
        "professional_id": None,
        "unit_id": None,
        "appointment_type": "Avaliação",
        "insurance": None,
        "attendee_name": None,
        "start_at": START,
        "end_at": END,
        "expires_at": datetime.now(UTC) + timedelta(minutes=45),
    }
    args.update(overrides)
    return await holds.place_difference_hold_in(session, **args)


async def _setup(db):
    async with db() as session:
        world = await make_world(session)
    async with db() as session:
        adjustment = await _adjustment(session, world)
        await session.commit()
    return world, adjustment


async def test_a_difference_hold_belongs_to_no_conversation_and_blocks_its_window(db):
    world, adjustment = await _setup(db)
    async with db() as session:
        held = await _place(session, world, adjustment)
        await session.commit()

    assert held is not None
    assert held.conversation_id is None
    assert held.adjustment_id == adjustment.id
    assert held.phase == holds.BLOCKING_PHASE and held.charge_id is None
    assert held.patient_id == world.patient.id and held.appointment_type == "Avaliação"
    assert await holds.held_windows(world.tenant_id, None) == [(START, END)]


async def test_held_windows_excluding_a_conversation_still_sees_a_hold_without_one(db):
    """The bug S0 fixes: `conversation_id != X` is NULL (not TRUE) for a NULL conversation, so
    the patient's own exclusion used to make the difference hold vanish from every slot list."""
    world, adjustment = await _setup(db)
    async with db() as session:
        await _place(session, world, adjustment)
        await session.commit()

    windows = await holds.held_windows(
        world.tenant_id, None, exclude_conversation=world.conversation.id
    )
    assert windows == [(START, END)]


async def test_the_conversation_exclusion_still_hides_the_conversation_own_payment_hold(db):
    world, adjustment = await _setup(db)
    own_start = START + timedelta(hours=3)
    await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=own_start,
        end_at=own_start + timedelta(minutes=30),
        phase="payment",
        charge_id=uuid4(),
    )
    async with db() as session:
        await _place(session, world, adjustment)
        await session.commit()

    windows = await holds.held_windows(
        world.tenant_id, None, exclude_conversation=world.conversation.id
    )
    assert windows == [(START, END)]
    assert len(await holds.held_windows(world.tenant_id, None)) == 2


async def test_another_live_payment_hold_on_the_window_refuses_and_writes_nothing(db):
    world, adjustment = await _setup(db)
    await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=START + timedelta(minutes=30),
        end_at=END + timedelta(minutes=30),
        phase="payment",
        charge_id=uuid4(),
    )
    async with db() as session:
        held = await _place(session, world, adjustment)
        await session.commit()
        rows = list(
            await session.scalars(
                select(BookingHold).where(BookingHold.adjustment_id == adjustment.id)
            )
        )

    assert held is None
    assert rows == []


async def test_a_code_phase_hold_or_another_agenda_does_not_conflict(db):
    world, adjustment = await _setup(db)
    # TASK-072: a code-phase row reserves nothing.
    await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=START,
        end_at=END,
    )
    async with db() as session:
        # A payment hold on ANOTHER agenda (professional) never blocks this one.
        other_adjustment = await _adjustment(session, world)
        await session.commit()
    async with db() as session:
        from secretaria.models import Professional

        doctor = Professional(id=uuid4(), tenant_id=world.tenant_id, name="Dra. Ana")
        session.add(doctor)
        await session.flush()
        assert await _place(session, world, other_adjustment, professional_id=doctor.id)
        held = await _place(session, world, adjustment)
        await session.commit()

    assert held is not None


async def test_placing_again_for_the_same_adjustment_replaces_its_own_hold(db):
    world, adjustment = await _setup(db)
    async with db() as session:
        first = await _place(session, world, adjustment)
        await session.commit()
    async with db() as session:
        # Its own hold never conflicts with itself; the old row is replaced, never doubled.
        second = await _place(
            session, world, adjustment, start_at=START + timedelta(minutes=15),
            end_at=END + timedelta(minutes=15),
        )
        await session.commit()
        rows = list(
            await session.scalars(
                select(BookingHold).where(BookingHold.adjustment_id == adjustment.id)
            )
        )

    assert first is not None and second is not None and first.id != second.id
    assert [row.id for row in rows] == [second.id]


async def test_release_drops_only_that_adjustment_hold(db):
    world, adjustment = await _setup(db)
    async with db() as session:
        other = await _adjustment(session, world)
        await session.commit()
    async with db() as session:
        await _place(session, world, adjustment)
        await _place(
            session, world, other, start_at=START + timedelta(hours=4),
            end_at=END + timedelta(hours=4),
        )
        await session.commit()
    async with db() as session:
        dropped = await holds.release_difference_hold_in(session, adjustment.id)
        await session.commit()
    async with db() as session:
        again = await holds.release_difference_hold_in(session, adjustment.id)
        await session.commit()
        left = list(await session.scalars(select(BookingHold.adjustment_id)))

    assert (dropped, again) == (1, 0)
    assert left == [other.id]


async def test_extend_moves_the_deadline_and_can_repoint_to_a_new_adjustment(db):
    world, adjustment = await _setup(db)
    async with db() as session:
        successor = await _adjustment(session, world)
        await session.commit()
    async with db() as session:
        placed = await _place(session, world, adjustment)
        await session.commit()
    later = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=20)
    async with db() as session:
        extended = await holds.extend_difference_hold_in(session, adjustment.id, expires_at=later)
        await session.commit()
    async with db() as session:
        moved = await holds.extend_difference_hold_in(
            session, adjustment.id, expires_at=later + timedelta(hours=1),
            to_adjustment_id=successor.id,
        )
        await session.commit()
        gone = await holds.difference_hold_in(session, adjustment.id)
        found = await holds.difference_hold_in(session, successor.id)

    assert extended is not None and extended.id == placed.id and extended.expires_at == later
    assert moved is not None and moved.id == placed.id and moved.adjustment_id == successor.id
    assert gone is None
    assert found is not None and found.id == placed.id


async def test_an_expired_difference_hold_is_never_revived(db):
    """Once its clock ran out, the slot may have been booked by somebody else: extending it
    would hide a slot that is no longer ours to hold."""
    world, adjustment = await _setup(db)
    async with db() as session:
        past = datetime.now(UTC) - timedelta(minutes=1)
        await _place(session, world, adjustment, expires_at=past)
        await session.commit()
    async with db() as session:
        extended = await holds.extend_difference_hold_in(
            session, adjustment.id, expires_at=datetime.now(UTC) + timedelta(hours=5)
        )
        found = await holds.difference_hold_in(session, adjustment.id)

    assert extended is None and found is None
    assert await holds.held_windows(world.tenant_id, None) == []


async def test_live_hold_overlaps_can_exclude_the_change_own_hold_only(db):
    world, adjustment = await _setup(db)
    async with db() as session:
        other = await _adjustment(session, world)
        await session.commit()
    async with db() as session:
        await _place(session, world, adjustment)
        await session.commit()
    async with db() as session:
        sees_it = await holds.live_hold_overlaps(session, world.tenant_id, START, END)
        own_excluded = await holds.live_hold_overlaps(
            session, world.tenant_id, START, END, professional_id=None,
            exclude_adjustment_id=adjustment.id,
        )
        other_excluded = await holds.live_hold_overlaps(
            session, world.tenant_id, START, END, exclude_adjustment_id=other.id
        )
        # A conversation's hold has no adjustment: excluding an adjustment never hides it.
        by_charge = await holds.live_hold_overlaps(
            session, world.tenant_id, START, END, exclude_charge_id=uuid4()
        )

    assert sees_it is True
    assert own_excluded is False
    assert other_excluded is True
    assert by_charge is True


async def test_a_conversation_placing_its_own_hold_never_deletes_a_difference_hold(db):
    world, adjustment = await _setup(db)
    async with db() as session:
        await _place(session, world, adjustment)
        await session.commit()
    elsewhere = START + timedelta(hours=6)
    await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=elsewhere,
        end_at=elsewhere + timedelta(minutes=30),
    )
    async with db() as session:
        assert await holds.difference_hold_in(session, adjustment.id) is not None


async def test_another_clinic_never_sees_the_hold(db):
    world, adjustment = await _setup(db)
    async with db() as session:
        other_world = await make_world(session)
    async with db() as session:
        await _place(session, world, adjustment)
        await session.commit()
        assert not await holds.live_hold_overlaps(session, other_world.tenant_id, START, END)
    assert await holds.held_windows(other_world.tenant_id, None) == []
