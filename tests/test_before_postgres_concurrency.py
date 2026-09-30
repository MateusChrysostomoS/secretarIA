"""Concurrency proofs that SQLite cannot give (spec sections 8.5, 9, 16) for the `before` flow.

Opt-in: `PAYMENTS_TEST_PG_URL=postgresql+asyncpg://user@host:port/postgres` (a throwaway server;
each test creates and drops its own database). What is proven under REAL concurrent connections:

* N workers promoting the SAME paid charge (the job, the sweep, a replay) create exactly ONE
  appointment, ONE patient notice and fire the post-booking hooks ONCE;
* the Pix window ending and a paid event racing each other can never lose the payment;
* N patients confirming the SAME slot at the same instant end with exactly ONE reservation.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.core.database import Base
from secretaria.models import (
    Appointment,
    BookingHold,
    Conversation,
    Patient,
    PaymentWebhookEvent,
    PixDeposit,
)
from secretaria.models.pix_deposit import FULFILLMENT_PROMOTING, PixDepositStatus
from secretaria.services import booking_hold as holds, booking_promotion
from secretaria.services.payments import (
    booking_charge,
    event_processor,
    fulfillment,
    jobs,
    notify,
    registry,
)
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from secretaria.services.payments.webhook_payload import event_to_dict
from tests.before_support import make_world, new_charge
from tests.payments_support import FakeProvider, make_event, recording_enqueue

PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to a throwaway server")

WORKERS = 8
QUOTE = DepositQuote(
    "required", "required", amount_cents=6000, provider="asaas", ttl_minutes=30,
    service_label="Consulta",
)
SLOT = datetime(2030, 3, 4, 13, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def maker(monkeypatch):
    name = f"t017_conc_{uuid4().hex[:10]}"
    admin = create_async_engine(PG_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = PG_URL.rpartition("/")[0] + f"/{name}"
    engine = create_async_engine(url, pool_size=WORKERS * 2 + 2)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for module in (holds, booking_charge, notify):
        monkeypatch.setattr(module, "async_session_factory", session_maker)
    yield session_maker
    await engine.dispose()
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.dispose()


class SlowCalendar:
    def __init__(self) -> None:
        self.created: list[str | None] = []

    async def check_availability(self, start, end, max_results=50):
        return []

    async def create_event(self, start, end, summary, description="", *, event_id=None):
        await asyncio.sleep(0.05)  # keep every worker inside the promotion at the same time
        self.created.append(event_id)
        return {"id": event_id, "htmlLink": "https://cal/x"}


async def _paid_charge(maker, world):
    started = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=SLOT,
        end_at=SLOT + timedelta(minutes=30),
    )
    async with maker() as session:
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
    return started.deposit_id


async def test_many_workers_promoting_one_paid_charge_create_one_consultation(maker, monkeypatch):
    provider = FakeProvider()
    provider.created_charge = new_charge()
    monkeypatch.setitem(registry._PROVIDERS, "asaas", provider)
    calendar = SlowCalendar()
    notices: list[str] = []
    hooks: list[str] = []

    async def calendar_for_booking(session, tenant, professional_id):
        return calendar

    async def deliver(session, tenant, patient, messages, **kwargs):
        await asyncio.sleep(0.02)
        notices.extend(m.body for m in messages)

    async def enqueue_hooks(redis, tenant_id, appointment_id, source):
        hooks.append(str(appointment_id))

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", calendar_for_booking)
    monkeypatch.setattr(notify, "deliver", deliver)
    monkeypatch.setattr("secretaria.plugins.post_booking.enqueue_post_booking_hooks", enqueue_hooks)
    async with maker() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(maker, world)

    async def worker():
        async with maker() as session:
            return await fulfillment.fulfill_paid_charge(
                session, deposit_id=deposit_id, redis=object()
            )

    outcomes = await asyncio.gather(*(worker() for _ in range(WORKERS)), return_exceptions=True)

    assert not [o for o in outcomes if isinstance(o, Exception)], outcomes
    async with maker() as session:
        appointments = list(await session.scalars(select(Appointment)))
        deposit = await session.get(PixDeposit, deposit_id)
    assert len(appointments) == 1
    assert deposit.appointment_id == appointments[0].id
    assert deposit.fulfillment_status == "confirmed"
    assert set(calendar.created) == {deposit_id.hex}  # every attempt used the SAME event id
    assert len(notices) == 1  # ONE "consulta confirmada"
    assert hooks == [str(appointments[0].id)]  # the hooks fired ONCE


async def test_the_pix_window_ending_and_a_paid_event_racing_never_lose_the_payment(
    maker, monkeypatch
):
    provider = FakeProvider()
    provider.created_charge = new_charge()
    monkeypatch.setitem(registry._PROVIDERS, "asaas", provider)
    async with maker() as session:
        world = await make_world(session)
    started = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=SLOT,
        end_at=SLOT + timedelta(minutes=30),
    )
    async with maker() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == started.deposit_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        deposit = await session.get(PixDeposit, started.deposit_id)
        row = PaymentWebhookEvent(
            tenant_id=world.tenant_id,
            provider="asaas",
            event_id="evt_paid",
            provider_charge_id=deposit.provider_charge_id,
            payload={
                "normalized": event_to_dict(
                    make_event(
                        event_id="evt_paid",
                        charge_id=deposit.provider_charge_id,
                        reference=str(started.deposit_id),
                    )
                )
            },
        )
        session.add(row)
        await session.commit()
        row_id = row.id
    # The provider says PAID: the expiry job must NOT expire it, the event core must pay it once.
    provider.state = ChargeState(
        PaymentStatus.PAID, paid_amount_cents=6000, external_reference=str(started.deposit_id)
    )
    sent, enqueue = recording_enqueue()

    async def expire():
        async with maker() as session:
            return await jobs.expire_due_charges(session, now=datetime.now(UTC), enqueue=enqueue)

    async def pay():
        async with maker() as session:
            return await event_processor.process_payment_event_row(session, row_id)

    await asyncio.gather(*(f() for _ in range(3) for f in (expire, pay)))

    async with maker() as session:
        deposit = await session.get(PixDeposit, started.deposit_id)
        paid_events = await session.scalar(
            select(func.count()).select_from(PaymentWebhookEvent).where(
                PaymentWebhookEvent.status == "processed"
            )
        )
    assert deposit.status == PixDepositStatus.PAID  # never expired, never lost
    assert deposit.fulfillment_status == FULFILLMENT_PROMOTING  # one hand-off to the promoter
    assert deposit.paid_amount_cents == 6000
    assert paid_events == 1


async def test_place_hold_waits_for_the_agenda_lock_and_other_agendas_do_not(maker):
    """Deterministic proof of the serialisation (the concurrent test below cannot force the
    interleaving): while another transaction holds the agenda's advisory lock, `place_hold` on
    that agenda waits; on a different professional's agenda it does not."""
    from sqlalchemy import text

    from secretaria.models import Professional

    async with maker() as session:
        world = await make_world(session)
        professional = Professional(id=uuid4(), tenant_id=world.tenant_id, name="Dr B")
        rival = Patient(id=uuid4(), tenant_id=world.tenant_id, wa_id="5511777000000")
        session.add_all([professional, rival])
        await session.flush()
        rival_conversation = Conversation(
            id=uuid4(), tenant_id=world.tenant_id, patient_id=rival.id
        )
        session.add(rival_conversation)
        await session.commit()

    async def hold(conversation_id, patient_id, professional_id):
        return await holds.place_hold(
            tenant_id=world.tenant_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            professional_id=professional_id,
            appointment_type="Consulta",
            insurance=None,
            start_at=SLOT,
            end_at=SLOT + timedelta(minutes=30),
        )

    lock = text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))")
    async with maker() as blocker:
        async with blocker.begin():
            await blocker.execute(lock, {"key": holds.hold_lock_key(world.tenant_id, None)})
            waiting = asyncio.create_task(hold(world.conversation.id, world.patient.id, None))
            await asyncio.sleep(0.4)
            assert not waiting.done()  # the SAME agenda waits for the lock holder
            other = await asyncio.wait_for(
                hold(rival_conversation.id, rival.id, professional.id), 5
            )
            assert other is not None  # a DIFFERENT agenda never waits
    assert (await asyncio.wait_for(waiting, 5)) is not None  # ...and proceeds once it commits


async def test_many_patients_confirming_the_same_slot_end_with_exactly_one_reservation(
    maker, monkeypatch
):
    async with maker() as session:
        base = await make_world(session)
        rivals = []
        for n in range(WORKERS):
            patient = Patient(
                id=uuid4(), tenant_id=base.tenant_id, wa_id=f"55119990000{n:02d}", name=f"P{n}"
            )
            session.add(patient)
            await session.flush()
            conversation = Conversation(
                id=uuid4(), tenant_id=base.tenant_id, patient_id=patient.id
            )
            session.add(conversation)
            rivals.append((patient.id, conversation.id))
        await session.commit()

    async def confirm(patient_id, conversation_id):
        return await holds.place_hold(
            tenant_id=base.tenant_id,
            conversation_id=conversation_id,
            patient_id=patient_id,
            professional_id=None,
            appointment_type="Consulta",
            insurance=None,
            start_at=SLOT,
            end_at=SLOT + timedelta(minutes=30),
            phase="payment",
            charge_id=uuid4(),
            ttl_minutes=30,
        )

    results = await asyncio.gather(*(confirm(p, c) for p, c in rivals))

    winners = [r for r in results if r is not None]
    async with maker() as session:
        rows = list(await session.scalars(select(BookingHold)))
    assert len(winners) == 1 and len(rows) == 1
