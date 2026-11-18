"""TASK-085 R / UB1 on Postgres (opt-in: a throwaway database next to `PAYMENTS_TEST_PG_URL`).

What SQLite cannot prove: the candidate query (keyset on start/id with a correlated suspension
EXISTS, uuid and timestamptz comparisons) walks every page on the real dialect, and two sweeps of
the same clinic running AT THE SAME TIME (two 5-minute windows overlapping) e-mail each patient
once and leave one `price_change` row per consultation.
"""

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.core.database import Base
from secretaria.models.appointment_reminder import AppointmentReminder
from secretaria.services import reminder_delivery
from secretaria.services.brain_patients import PatientEmailResult
from secretaria.services.email import EmailOutcome
from secretaria.services.payments import price_change
from tests.payments_support import FakeRedis
from tests.test_sinal_trocas_ub1_sweep import (
    CHANGED_AT,
    LINK,
    NOW,
    START,
    reprice,
    seed_tenant,
    seed_visit,
)

PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to run on Postgres")


async def _admin(statement: str) -> None:
    engine = create_async_engine(PG_URL, isolation_level="AUTOCOMMIT")
    async with engine.connect() as conn:
        await conn.exec_driver_sql(statement)
    await engine.dispose()


@pytest.fixture
async def pg(monkeypatch):
    base, _, _ = PG_URL.rpartition("/")
    name = f"t085ub1_{uuid.uuid4().hex[:10]}"
    await _admin(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(f"{base}/{name}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        monkeypatch.setattr(price_change, "async_session_factory", maker)
        yield maker
    finally:
        await engine.dispose()
        await _admin(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture
def mail(monkeypatch) -> list:
    sent: list = []

    async def _send(to, template, variables):
        await asyncio.sleep(0.01)  # let the other sweep run in between
        sent.append((to, variables["link_line"]))
        return EmailOutcome.SENT

    async def _no_brain_api(tenant_id, external_id):
        return PatientEmailResult()

    monkeypatch.setattr(price_change, "send_transactional_email_result", _send)
    monkeypatch.setattr(price_change, "portal_conversation_link", lambda t, r: f"{LINK}{r}")
    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _no_brain_api)
    return sent


async def _rows(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(AppointmentReminder))


async def test_the_sweep_walks_every_page_on_postgres(pg, mail, monkeypatch):
    monkeypatch.setattr(price_change, "_PAGE_SIZE", 2)
    async with pg() as session:
        tenant = await seed_tenant(session)
        for n in range(5):
            # Two consultations share each start: the keyset must break ties on the id.
            await seed_visit(
                session, tenant, email=f"p{n}@example.test", start=START + timedelta(hours=n // 2)
            )
        await session.commit()
    await reprice(pg, tenant.id, "R$ 240,00")

    sent = await price_change.sweep_price_changes(
        tenant.id, changed_at=CHANGED_AT, now=NOW, redis=FakeRedis()
    )

    assert sent == 5
    assert sorted(to for to, _ in mail) == [f"p{n}@example.test" for n in range(5)]
    assert await _rows(pg) == 5


async def test_two_overlapping_sweeps_email_each_patient_once(pg, mail):
    async with pg() as session:
        tenant = await seed_tenant(session)
        for n in range(3):
            await seed_visit(
                session, tenant, email=f"p{n}@example.test", start=START + timedelta(hours=n)
            )
        await session.commit()
    await reprice(pg, tenant.id, "R$ 240,00")
    redis = FakeRedis()  # the one Redis both workers share

    results = await asyncio.gather(
        *(
            price_change.sweep_price_changes(tenant.id, changed_at=CHANGED_AT, now=NOW, redis=redis)
            for _ in range(2)
        )
    )

    assert sum(results) == 3
    assert sorted(to for to, _ in mail) == ["p0@example.test", "p1@example.test", "p2@example.test"]
    assert await _rows(pg) == 3
    # And a later sweep finds every notice pending: nothing more.
    assert (
        await price_change.sweep_price_changes(
            tenant.id, changed_at=CHANGED_AT, now=NOW, redis=redis
        )
        == 0
    )


async def test_a_sweep_that_read_before_the_other_committed_does_not_mail_again(
    pg, mail, monkeypatch
):
    """Review I1 on Postgres: sweep B reads "no notice", then waits on brain-api while sweep A
    mails, commits the row and releases the claim; B takes the free claim, re-reads and stops."""
    async with pg() as session:
        tenant = await seed_tenant(session)
        _p, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(pg, tenant.id, "R$ 240,00")
    redis = FakeRedis()
    b_read, a_done = asyncio.Event(), asyncio.Event()
    real = price_change._patient_email

    async def _email(notice):
        if not b_read.is_set():
            b_read.set()
            await a_done.wait()
        return await real(notice)

    monkeypatch.setattr(price_change, "_patient_email", _email)

    async def sweep_b():
        return await price_change._notify_one(
            tenant.id, appointment.id, changed_at=CHANGED_AT, now=NOW, redis=redis
        )

    async def sweep_a():
        await b_read.wait()
        outcome = await price_change._notify_one(
            tenant.id, appointment.id, changed_at=CHANGED_AT, now=NOW, redis=redis
        )
        a_done.set()
        return outcome

    assert await asyncio.gather(sweep_b(), sweep_a()) == ["pending", "sent"]
    assert len(mail) == 1 and await _rows(pg) == 1
