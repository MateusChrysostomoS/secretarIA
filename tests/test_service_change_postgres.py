"""Concurrency proofs of the service-change difference that SQLite cannot give (TASK-067 s07).

* apply-once: many webhook workers for the SAME paid difference write the pending edit once;
* lock order: the webhook of a difference and a cancellation of its consultation take the
  appointment row first, then the adjustment - the opposite order deadlocked (Postgres aborts
  one of the two transactions).

Opt-in: `PAYMENTS_TEST_PG_URL=postgresql+asyncpg://user@host:port/postgres` (a throwaway
server; each test creates and drops its own database). Skipped otherwise.
"""

import asyncio
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.core.database import Base
from secretaria.models import Appointment, AppointmentStatus, PaymentAttentionItem, Tenant
from secretaria.models.deposit_adjustment import (
    STATUS_APPLIED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import REASON_ORPHAN_PAYMENT
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from tests import test_service_change as base, test_service_change_webhook as hook

PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to a throwaway server")

WORKERS = 8
provider = base.provider
no_default_hooks = base.no_default_hooks


@pytest_asyncio.fixture
async def maker():
    name = f"t067_svc_{uuid4().hex[:10]}"
    admin = create_async_engine(PG_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = PG_URL.rpartition("/")[0] + f"/{name}"
    engine = create_async_engine(url, pool_size=WORKERS + 2)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.dispose()


async def _seed_pg(session):
    """`base._seed`'s rows and values, with the tenant flushed on its own first: Postgres
    enforces the patient -> tenant FK that SQLite does not (one flush may order them wrong)."""
    flushed = session.add_all

    def add_all(rows):
        rows = list(rows)
        tenants = [r for r in rows if isinstance(r, Tenant)]
        if tenants:
            flushed(tenants)
            pending.append([r for r in rows if not isinstance(r, Tenant)])
        else:
            flushed(rows)

    pending: list[list] = []
    original_flush = session.flush

    async def flush(*args, **kwargs):
        await original_flush()
        while pending:
            flushed(pending.pop())
            await original_flush()

    session.add_all, session.flush = add_all, flush
    try:
        return await base._seed(session)
    finally:
        del session.add_all, session.flush


async def _awaiting(maker, provider):
    async with maker() as session:
        seeded = await _seed_pg(session)
    created = await base._request(maker, seeded, "Avaliação", minutes=60)
    assert created.kind == "awaiting_payment"
    hook._paid(provider, created)
    return seeded, created


async def test_many_workers_apply_one_paid_difference_exactly_once(maker, provider):
    seeded, created = await _awaiting(maker, provider)

    results = await asyncio.gather(
        *(hook._process(maker, seeded[0], event_id=f"evt_diff_{n}") for n in range(WORKERS))
    )

    outcomes = sorted(r.outcome for r in results)
    assert outcomes == ["adjustment_already_applied"] * (WORKERS - 1) + ["adjustment_applied"]
    assert [r.applied_adjustment_id for r in results if r.applied_adjustment_id] == [
        created.adjustment_id
    ]  # the patient's notice / agenda hooks run once
    async with maker() as session:
        row = await session.get(Appointment, seeded[2].id)
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
        notices = await session.scalar(select(func.count()).select_from(ProfessionalEditNotice))
        items = await session.scalar(select(func.count()).select_from(PaymentAttentionItem))
    assert (row.appointment_type, adjustment.status) == ("Avaliação", STATUS_APPLIED)
    assert (notices, items) == (1, 0)


async def test_a_difference_paid_while_its_consultation_is_cancelled_never_deadlocks(
    maker, provider
):
    """Cancel transaction: the appointment row first (the guarded UPDATE of every cancel
    surface), then `settle_open_adjustments` moves the unpaid difference to `cancelled`. The
    webhook of that difference must queue behind the appointment instead of holding the
    adjustment row and then waiting for the appointment (a lock cycle = Postgres aborts one)."""
    seeded, created = await _awaiting(maker, provider)
    appointment_id = seeded[2].id

    async with maker() as cancel:
        await cancel.execute(
            update(Appointment)
            .where(Appointment.id == appointment_id)
            .values(status=AppointmentStatus.CANCELLED)
        )
        webhook = asyncio.create_task(hook._process(maker, seeded[0]))
        await asyncio.sleep(1.0)  # the webhook verified the charge and reached the locks
        await cancel.execute(
            update(DepositAdjustment)
            .where(
                DepositAdjustment.id == created.adjustment_id,
                DepositAdjustment.status == STATUS_AWAITING_PAYMENT,
            )
            .values(status=STATUS_CANCELLED)
        )
        await cancel.commit()
    result = await asyncio.wait_for(webhook, timeout=15)

    assert result.outcome == "adjustment_paid_late"
    async with maker() as session:
        row = await session.get(Appointment, appointment_id)
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
        reasons = list(await session.scalars(select(PaymentAttentionItem.reason)))
    assert (row.appointment_type, row.status) == ("Consulta", AppointmentStatus.CANCELLED)
    assert adjustment.status == STATUS_CANCELLED
    assert reasons == [REASON_ORPHAN_PAYMENT]  # the clinic handles the money; nothing changed
