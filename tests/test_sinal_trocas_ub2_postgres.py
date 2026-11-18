"""TASK-085 R / UB2 on Postgres (opt-in: a throwaway database next to `PAYMENTS_TEST_PG_URL`).

What SQLite cannot prove: two openings of the SAME price link running at the same time (a double
click, two tabs, a reload racing the first entry) create ONE Pix of the difference. The
consultation row lock (`FOR UPDATE`) serializes them; the guarded claim of the notice
(`answered_at IS NULL`) makes the second one answer "pending".
"""

import asyncio
import os
import uuid

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.core.database import Base
from secretaria.models import Tenant
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.services.payments import price_change, registry
from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
from tests.payments_support import FakeProvider
from tests.test_sinal_trocas_ub1_sweep import NOW
from tests.test_sinal_trocas_ub2_opening import add_notice, reprice, seed

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
    name = f"t085ub2_{uuid.uuid4().hex[:10]}"
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


class _SlowProvider(FakeProvider):
    async def create_pix_charge(self, account, request):
        await asyncio.sleep(0.2)  # the other opening runs meanwhile
        return await super().create_pix_charge(account, request)


async def test_two_openings_at_once_charge_once(pg, monkeypatch):
    provider = _SlowProvider(name="asaas", state=ChargeState(PaymentStatus.PENDING))
    provider.created_charge = CreatedCharge(provider_charge_id="pay_diff", copy_paste="COPY")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", provider)
    tenant, patient, appointment, _deposit = await seed(pg)
    rid = await add_notice(pg, appointment)
    await reprice(pg, tenant.id, "R$ 300,00")

    async def _open():
        async with pg() as session:
            current = await session.get(Tenant, tenant.id)
            return await price_change.open_price_notice(
                session, tenant=current, reminder_id=rid, patient_id=patient.id, now=NOW
            )

    kinds = sorted(opening.kind for opening in await asyncio.gather(_open(), _open()))

    assert kinds == ["pending", "pix"]
    assert provider.called("create_pix_charge") == 1
    async with pg() as session:
        assert await session.scalar(select(func.count()).select_from(DepositAdjustment)) == 1
