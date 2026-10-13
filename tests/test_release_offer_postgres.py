"""Races SQLite cannot prove for the released-slot offer (TASK-052), on a REAL Postgres.

Opt-in: `PAYMENTS_TEST_PG_URL=postgresql+asyncpg://user@host:port/postgres` (a throwaway server;
each test creates and drops its own database). Proven under concurrent connections:

* N "Cancelar" taps + N runs of the B sweep on ONE offer -> exactly one tap cancels, the refund
  falls due once: ONE attention item, ONE cancel notice, whatever the sweep did;
* N booking commits racing the sweep on an offer in `rescheduling` -> the deposit moves to exactly
  one new appointment and no refund item is left open (a refund B made due is undone);
* N dispatchers on ONE due e-mail -> exactly one SMTP send.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.config import get_settings
from secretaria.core.database import Base
from secretaria.models import PaymentAttentionItem, PixDeposit
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
from secretaria.services import email, release_offer, release_offer_dispatch
from tests.release_offer_support import make_released, new_appointment

PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to a throwaway server")

WORKERS = 8


@pytest_asyncio.fixture
async def maker():
    name = f"t052_conc_{uuid4().hex[:10]}"
    admin = create_async_engine(PG_URL, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'CREATE DATABASE "{name}"')
    url = PG_URL.rpartition("/")[0] + f"/{name}"
    engine = create_async_engine(url, pool_size=WORKERS * 2 + 2)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()
    async with admin.connect() as conn:
        await conn.exec_driver_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    await admin.dispose()


async def _offer(maker, *, released_hours_ago: float):
    async with maker() as session:
        released = await make_released(session, business_hours={})
        offer = await release_offer.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=datetime.now(UTC) - timedelta(hours=released_hours_ago),
        )
        await session.commit()
    return released, offer


async def test_cancel_taps_and_sweeps_open_exactly_one_item(maker):
    # Released 5 h 59 min 58.5 s ago: B is ~1.5 s ahead, so taps (before B) and sweeps (after B)
    # genuinely overlap around the deadline.
    _released, offer = await _offer(maker, released_hours_ago=6 - 1.5 / 3600)

    async def tap():
        async with maker() as session:
            row = await session.get(DepositReleaseOffer, offer.id)
            outcome = await release_offer.choose_cancel(session, offer=row)
            await session.commit()
            return outcome == release_offer.CANCEL_NOTIFIED

    async def sweep():
        await asyncio.sleep(1.5)
        async with maker() as session:
            return await release_offer.expire_offers(session)

    results = await asyncio.gather(
        *[tap() for _ in range(WORKERS)], *[sweep() for _ in range(WORKERS)]
    )

    taps = results[:WORKERS]
    assert sum(1 for won in taps if won) == 1  # one "Cancelar" wins; the sweep may or may not
    async with maker() as session:
        items = await session.scalar(select(func.count()).select_from(PaymentAttentionItem))
        row = await session.get(DepositReleaseOffer, offer.id)
        cancel_rows = await session.scalar(
            select(func.count())
            .select_from(DepositReleaseEmail)
            .where(DepositReleaseEmail.kind == "clinic_refund_cancel")
        )
    assert items == 1 and cancel_rows == 1
    assert (row.decision, row.refund_status) == ("cancelled", "due")


async def test_bookings_racing_the_sweep_carry_exactly_once_and_leave_no_refund_open(maker):
    released, offer = await _offer(maker, released_hours_ago=6 - 1.5 / 3600)
    async with maker() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert await release_offer.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id
        )
        # The provider said "paid, no refund" at the confirmation (Task 10): every racing booking
        # (two confirm clicks, the Portal code and the flow) may try to carry - ONE wins.
        await release_offer.record_carry_check(
            session, tenant_id=released.tenant_id, offer_id=offer.id, outcome="reusable"
        )
        await session.commit()

    async def book(day: int):
        async with maker() as session:
            fresh = await new_appointment(
                session, released, start=datetime(2030, 4, day, 14, tzinfo=UTC)
            )
            carried = await release_offer.claim_carry_over(
                session,
                tenant_id=released.tenant_id,
                conversation_id=released.conversation.id,
                new_appointment=fresh,
            )
            await session.commit()
            return carried is not None

    async def sweep():
        await asyncio.sleep(1.5)
        async with maker() as session:
            return await release_offer.expire_offers(session)

    results = await asyncio.gather(*[book(day) for day in range(1, WORKERS + 1)], sweep())

    # Owner rule 2026-10-12: "Remarcar" carries the deposit even after B (the refund that B made
    # due is undone) - so exactly ONE booking carries, whatever the sweep did, and no refund item
    # is left open for money that is in use.
    assert sum(1 for won in results[:WORKERS] if won) == 1
    async with maker() as session:
        open_items = await session.scalar(
            select(func.count())
            .select_from(PaymentAttentionItem)
            .where(PaymentAttentionItem.status == "open")
        )
        deposit = await session.get(PixDeposit, released.deposit.id)
        row = await session.get(DepositReleaseOffer, offer.id)
    assert (row.decision, row.refund_status) == ("rebooked", "not_due") and open_items == 0
    assert deposit.appointment_id == row.new_appointment_id


async def test_concurrent_dispatchers_send_one_email(maker, monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(email, "_send_sync", lambda to, subject, body: sent.append(subject))
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    _released, offer = await _offer(maker, released_hours_ago=1)
    async with maker() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        assert (
            await release_offer.choose_cancel(session, offer=row) == release_offer.CANCEL_NOTIFIED
        )
        await session.commit()

    async def dispatch():
        async with maker() as session:
            return (await release_offer_dispatch.dispatch_due(session)).sent

    results = await asyncio.gather(*[dispatch() for _ in range(WORKERS)])

    assert sum(results) == 1 and len(sent) == 1  # only the cancel notice was due
