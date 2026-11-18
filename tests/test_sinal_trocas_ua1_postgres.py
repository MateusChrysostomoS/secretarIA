"""TASK-085 R / UA1 - the old slot's races on Postgres (what SQLite cannot show).

On the secretarIA's own agenda (exclusive writes: the agenda's advisory lock), with real
`calendar_events` rows:

* the old consultation coming back ("Manter a antiga" / the silence sweep) racing ANOTHER booking
  of the same slot: exactly one of them takes it - never two consultations on one slot;
* the difference being PAID while the old event is being taken off the agenda: whatever the order,
  the consultation ends with exactly ONE live event, on the new window, and the row points at it.

Opt-in like tests/test_service_change_postgres.py: `PAYMENTS_TEST_PG_URL` (a throwaway server;
each test creates and drops its own database).
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update

from secretaria.models import Appointment, Tenant
from secretaria.models.calendar_event import CalendarEvent
from secretaria.models.deposit_adjustment import (
    OLD_SLOT_RELEASED,
    OLD_SLOT_RELEASING,
    OLD_SLOT_REPLACED,
    STATUS_APPLIED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_KEPT_OLD,
    DepositAdjustment,
)
from secretaria.services import booking_hold
from secretaria.services.calendar import CalendarSlotTakenError
from secretaria.services.local_calendar import create_event_exclusive
from secretaria.services.payments import old_slot, service_change
from tests import test_service_change as base, test_service_change_postgres as pg
from tests._local_agenda import agenda as local_agenda, wire_local_agenda
from tests.test_sinal_trocas_s1_postgres import _suspended

pytestmark = pg.pytestmark
NOW = base.NOW
ROUNDS = 8
provider = base.provider
no_default_hooks = base.no_default_hooks
maker = pg.maker


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(booking_hold, "_now", lambda: NOW)
    monkeypatch.setattr(old_slot, "_now", lambda: NOW)


async def _world(maker, provider, monkeypatch):
    """A suspended change of a consultation on the clinic's own agenda; its old event is a real
    local event the row points at."""
    seeded, created = await _suspended(maker, provider)
    wire_local_agenda(monkeypatch, maker)
    monkeypatch.setattr(old_slot, "async_session_factory", maker)
    local = local_agenda(seeded[0].id)
    start = seeded[2].start_at
    end = start + timedelta(minutes=30)
    old = await local.create_event(start, end, "Consulta - Maria")
    async with maker() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == seeded[2].id)
            .values(google_event_id=old["id"])
        )
        await session.commit()

    async def resolve(session, tenant, appointment):
        return local

    monkeypatch.setattr(service_change, "calendar_for_appointment", resolve)
    adjustment = await _adjustment(maker, created.adjustment_id)
    new_start = service_change._pending_window(seeded[2], adjustment.pending_edit)[0]
    return seeded, created.adjustment_id, local, old["id"], (start, end), new_start


async def _adjustment(maker, adjustment_id) -> DepositAdjustment:
    async with maker() as session:
        return await session.get(DepositAdjustment, adjustment_id)


async def _confirmed(maker, tenant_id) -> list[CalendarEvent]:
    async with maker() as session:
        return list(
            (
                await session.scalars(
                    select(CalendarEvent).where(
                        CalendarEvent.tenant_id == tenant_id, CalendarEvent.status == "confirmed"
                    )
                )
            ).all()
        )


async def test_the_old_visit_coming_back_and_another_booking_never_share_the_slot(
    maker, provider, monkeypatch
):
    seeded, adjustment_id, local, old_id, (start, end), _new = await _world(
        maker, provider, monkeypatch
    )
    tenant_id, appointment_id = seeded[0].id, seeded[2].id
    assert await old_slot.vacate_old_slot(tenant_id, adjustment_id) == "released"
    restored_id = old_slot.restore_event_id(adjustment_id, calendar=local)

    for round_ in range(ROUNDS):
        async with maker() as session:
            await session.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == adjustment_id)
                .values(status=STATUS_AWAITING_CHOICE, old_slot=OLD_SLOT_RELEASED,
                        expires_at=NOW + timedelta(hours=1), resolved_at=None)
            )
            await session.execute(
                update(Appointment)
                .where(Appointment.id == appointment_id)
                .values(google_event_id=old_id)
            )
            await session.commit()

        async def restore():
            async with maker() as session:
                return await old_slot.restore_old_slot(
                    session, tenant_id=tenant_id, adjustment_id=adjustment_id,
                    now=NOW + timedelta(hours=2), trigger="silence",
                )

        async def book():
            try:
                event = await create_event_exclusive(
                    local, start=start, end=end, summary="Outro paciente"
                )
            except CalendarSlotTakenError:
                return None
            return event["id"]

        if round_ % 2:
            other, outcome = await asyncio.gather(book(), restore())
        else:
            outcome, other = await asyncio.gather(restore(), book())

        busy = await local.check_availability(start, end)
        assert len(busy) == 1, (round_, outcome, other)
        assert (outcome == "restored") != (other is not None), (round_, outcome, other)
        adjustment = await _adjustment(maker, adjustment_id)
        async with maker() as session:
            row = await session.get(Appointment, appointment_id)
        if outcome == "restored":
            assert adjustment.status == STATUS_KEPT_OLD and row.google_event_id == restored_id
            assert busy[0]["id"] == restored_id
        else:
            assert outcome == "slot_taken"
            assert adjustment.status == STATUS_AWAITING_CHOICE and row.google_event_id == old_id
            assert busy[0]["id"] == other
        # Next round: nothing on the slot again (the deterministic id may be re-created).
        async with maker() as session:
            await session.execute(delete(CalendarEvent).where(CalendarEvent.id != old_id))
            await session.commit()


async def test_paid_while_the_old_event_leaves_ends_with_one_event_on_the_new_window(
    maker, provider, monkeypatch
):
    seeded, adjustment_id, local, old_id, (start, end), new_start = await _world(
        maker, provider, monkeypatch
    )
    tenant_id, appointment_id = seeded[0].id, seeded[2].id
    new_end = new_start + timedelta(minutes=60)

    async def pay():
        """The webhook (row first, then the change), then the agenda follows after the commit:
        the new event when the old one left (`place_new_event_after_payment`), else today's
        move of the old event."""
        async with maker() as session:
            row = await session.scalar(
                select(Appointment).where(Appointment.id == appointment_id).with_for_update()
                .execution_options(populate_existing=True)
            )
            won = await session.execute(
                update(DepositAdjustment)
                .where(
                    DepositAdjustment.id == adjustment_id,
                    DepositAdjustment.status == STATUS_AWAITING_PAYMENT,
                )
                .values(status=STATUS_APPLIED, resolved_at=NOW)
                .returning(DepositAdjustment.id)
            )
            assert won.scalar_one_or_none() is not None
            row.appointment_type = "Avaliação"
            row.start_at, row.end_at = new_start, new_end
            await session.commit()
            adjustment = await session.get(
                DepositAdjustment, adjustment_id, populate_existing=True
            )
            if adjustment.old_slot in (OLD_SLOT_RELEASING, OLD_SLOT_RELEASED):
                tenant = await session.get(Tenant, tenant_id)
                await old_slot.place_new_event_after_payment(
                    session, tenant=tenant, appointment=row, adjustment=adjustment,
                    patient_name="Maria",
                )
                return "placed"
            await local.update_event(old_id, new_start, new_end, exclusive=True)
            return "moved"

    async def vacate():
        return await old_slot.vacate_old_slot(tenant_id, adjustment_id)

    seen = set()
    for round_ in range(ROUNDS):
        async with maker() as session:
            await session.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == adjustment_id)
                .values(status=STATUS_AWAITING_PAYMENT, old_slot=None, old_slot_at=None,
                        resolved_at=None)
            )
            await session.execute(
                update(Appointment)
                .where(Appointment.id == appointment_id)
                .values(google_event_id=old_id, appointment_type="Consulta", start_at=start,
                        end_at=end, google_calendar_source=None)
            )
            await session.execute(delete(CalendarEvent).where(CalendarEvent.id != old_id))
            await session.execute(
                update(CalendarEvent)
                .where(CalendarEvent.id == old_id)
                .values(status="confirmed", start_at=start, end_at=end)
            )
            await session.commit()

        if round_ == 0:  # one after the other: the old event already left
            vacated = await vacate()
            paid = await pay()
        elif round_ == 1:  # paid first: the old event simply follows the row
            paid = await pay()
            vacated = await vacate()
        elif round_ % 2:
            vacated, paid = await asyncio.gather(vacate(), pay())
        else:
            paid, vacated = await asyncio.gather(pay(), vacate())
        seen.add(paid)

        live = await _confirmed(maker, tenant_id)
        async with maker() as session:
            row = await session.get(Appointment, appointment_id)
        adjustment = await _adjustment(maker, adjustment_id)
        assert [e.id for e in live] == [row.google_event_id], (round_, paid, vacated)
        assert live[0].start_at == new_start, (round_, paid, vacated)
        assert adjustment.status == STATUS_APPLIED
        if paid == "placed":
            assert adjustment.old_slot == OLD_SLOT_REPLACED
            assert row.google_event_id == old_slot.paid_event_id(adjustment_id, calendar=local)
        else:
            assert adjustment.old_slot is None and vacated == "not_applicable"
            assert row.google_event_id == old_id
    assert seen == {"placed", "moved"}
