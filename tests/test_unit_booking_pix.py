# ruff: noqa: F811
"""A `before`-timing Pix booking carries its unit through the payment (TASK-083 spec §8).

The real charge start (`booking_charge.begin_payment`) and the real paid promotion
(`fulfillment.fulfill_paid_charge`), on the fixtures of tests/test_before_fulfillment.py: the hold
records the unit, the deposit's booking snapshot carries it, and the appointment, the event
(written through the unit's agenda: its location on Google) and the paid confirmation name it.
Without a unit nothing changes (the existing fulfillment tests).
"""

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from sqlalchemy import select, update

from secretaria.models import BookingHold, Conversation, FlowState, PixDeposit, Unit
from secretaria.models.pix_deposit import FULFILLMENT_PROMOTING, PixDepositStatus
from secretaria.services import booking_hold as holds
from secretaria.services.payments import booking_charge
from tests.before_support import SLOT_END, SLOT_START, make_world
from tests.test_before_fulfillment import (  # noqa: F401 - fixtures
    QUOTE,
    _fulfill,
    _paid_charge,
    _state,
    db,
    env,
)


async def _unit(db, world) -> Unit:
    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=world.tenant_id, name="Centro", address="Rua A, 10")
        session.add(unit)
        await session.commit()
        return unit


def _scoping(env):
    """The fake agenda grows the `with_unit` seam; each scoped copy records its unit."""
    calendar = env.calendar
    scoped: list[tuple] = []

    def with_unit(unit_id, location, *, business_hours=None):
        scoped.append((unit_id, location, business_hours))
        return calendar

    calendar.with_unit = with_unit
    original = calendar.create_event
    calendar.descriptions = []

    async def create_event(start, end, summary, description="", *, event_id=None):
        calendar.descriptions.append(description)
        return await original(start, end, summary, description, event_id=event_id)

    calendar.create_event = create_event
    return scoped


def _link_details(text: str) -> str:
    [link] = [line for line in text.splitlines() if line.startswith("https://calendar.google")]
    return parse_qs(urlparse(link).query)["details"][0]


async def test_the_unit_rides_the_pix_from_the_hold_to_the_appointment(db, env):
    async with db() as session:
        world = await make_world(session)
    unit = await _unit(db, world)
    scoped = _scoping(env)

    started = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
        unit_id=unit.id,
    )
    async with db() as session:
        [hold] = list(await session.scalars(select(BookingHold)))
        deposit = await session.get(PixDeposit, started.deposit_id)
    assert hold.unit_id == unit.id and hold.phase == "payment"
    assert deposit.booking_snapshot["unit_id"] == str(unit.id)

    # Paid: the same steps `_paid_charge` runs, on THIS charge.
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.AWAITING_DEPOSIT
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == started.deposit_id)
            .values(
                status=PixDepositStatus.PAID,
                paid_at=datetime.now(UTC),
                paid_amount_cents=6000,
                fulfillment_status=FULFILLMENT_PROMOTING,
            )
        )
        await session.commit()

    assert await _fulfill(db, started.deposit_id) == "promoted"
    _deposit, [appointment], _items, _holds, _conversation = await _state(db, started.deposit_id)
    assert appointment.unit_id == unit.id
    # The event was written through the unit's agenda (location; the window keeps its hours).
    assert scoped == [(unit.id, "Centro — Rua A, 10", None)]
    (_channel, text) = env.notices[0]
    # Follow-up (owner 2026-11-13): the unit and its address on their own lines, in the event's
    # description (a paid booking at a unit now gets one) and in the patient's link details.
    assert "Unidade: Centro\nEndereço: Rua A, 10\n" in text
    assert env.calendar.descriptions == [
        "Serviço: Consulta\nUnidade: Centro\nEndereço: Rua A, 10\nConvênio: não informado"
    ]
    details = _link_details(text)
    assert "Unidade: Centro\nEndereço: Rua A, 10" in details
    assert "Data e horário: segunda-feira, 04/03/2030 às 10:00" in details


async def test_a_pix_without_a_unit_keeps_todays_snapshot_and_text(db, env):
    async with db() as session:
        world = await make_world(session)
    scoped = _scoping(env)
    deposit_id = await _paid_charge(db, world)
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
    assert "unit_id" not in deposit.booking_snapshot
    assert await _fulfill(db, deposit_id) == "promoted"
    _deposit, [appointment], *_ = await _state(db, deposit_id)
    assert appointment.unit_id is None
    assert scoped == []
    assert "Local:" not in env.notices[0][1]
    assert "Unidade" not in env.notices[0][1]
    # No unit: the paid event's description stays exactly what it was (none).
    assert env.calendar.descriptions == [""]


async def test_a_code_hold_moved_to_the_payment_phase_keeps_its_unit(db, env):
    async with db() as session:
        world = await make_world(session)
    unit = await _unit(db, world)
    held = await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
        unit_id=unit.id,
    )
    assert held.unit_id == unit.id
    started = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
        existing_hold=held,
    )
    async with db() as session:
        deposit = await session.get(PixDeposit, started.deposit_id)
    assert deposit.booking_snapshot["unit_id"] == str(unit.id)
