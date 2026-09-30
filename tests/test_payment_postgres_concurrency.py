"""Concurrency proofs that SQLite cannot give (spec section 16): the "paid once" transition, the
attention-item dedupe and the persist-first webhook, under REAL concurrent connections.

Opt-in: `PAYMENTS_TEST_PG_URL=postgresql+asyncpg://user@host:port/postgres` (a throwaway
server; each test creates and drops its own database). Skipped otherwise.
"""

import asyncio
import json
import os
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.core.database import Base, get_session
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    PaymentAttentionItem,
    PaymentWebhookEvent,
    PixDeposit,
    Tenant,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, attention, event_processor, registry
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from secretaria.services.payments.webhook_payload import event_to_dict
from tests.payments_support import FakeArqPool, FakeProvider, make_event

PG_URL = os.environ.get("PAYMENTS_TEST_PG_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="set PAYMENTS_TEST_PG_URL to a throwaway server")

WORKERS = 8
TOKEN = "webhook-token-secret-0123456789"


@pytest_asyncio.fixture
async def maker():
    name = f"t016_conc_{uuid4().hex[:10]}"
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


async def _seed(maker):
    async with maker() as session:
        tenant = Tenant(id=uuid4(), clinic_name="C", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="M")
        session.add(patient)
        await session.flush()
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token=TOKEN)
        row.status = "connected"
        appointment = Appointment(
            id=uuid4(), tenant_id=tenant.id, patient_id=patient.id, google_event_id="e",
            appointment_type="Consulta", status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.flush()
        deposit = PixDeposit(
            tenant_id=tenant.id, appointment_id=appointment.id, patient_id=patient.id,
            amount_cents=6000, percent_applied=30, provider="asaas", provider_charge_id="pay_1",
            external_reference="dep-ref-1",
        )
        session.add(deposit)
        await session.commit()
        return tenant, deposit


async def test_many_events_for_one_payment_mark_it_paid_exactly_once(maker, monkeypatch):
    paid = ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=6000, external_reference="dep-ref-1"
    )
    monkeypatch.setitem(registry._PROVIDERS, "asaas", FakeProvider(state=paid))
    tenant, deposit = await _seed(maker)
    row_ids = []
    async with maker() as session:
        for n in range(WORKERS):  # RECEIVED / CONFIRMED / redeliveries: all distinct event ids
            row = PaymentWebhookEvent(
                tenant_id=tenant.id, provider="asaas", event_id=f"evt_{n}",
                provider_charge_id="pay_1",
                payload={"normalized": event_to_dict(make_event(event_id=f"evt_{n}"))},
            )
            session.add(row)
            await session.flush()
            row_ids.append(row.id)
        await session.commit()

    async def worker(row_id):
        async with maker() as session:
            return await event_processor.process_payment_event_row(session, row_id)

    results = await asyncio.gather(*(worker(r) for r in row_ids))
    outcomes = sorted(result.outcome for result in results)
    notified = [r.notify_paid_deposit_id for r in results if r.notify_paid_deposit_id]

    assert outcomes == ["already_paid"] * (WORKERS - 1) + ["paid"]
    assert notified == [deposit.id]  # exactly one "sinal recebido" for the patient
    async with maker() as session:
        after = await session.get(PixDeposit, deposit.id)
        items = await session.scalar(select(func.count()).select_from(PaymentAttentionItem))
    assert after.status == PixDepositStatus.PAID and items == 0


async def test_the_same_attention_situation_from_many_workers_opens_one_item(maker):
    tenant, deposit = await _seed(maker)

    async def worker():
        async with maker() as session:
            live = await session.get(PixDeposit, deposit.id)
            outcome = await attention.open_attention_item(
                session,
                tenant_id=tenant.id,
                reason="amount_under",
                dedupe_key=attention.attention_dedupe_key("asaas", "pay_1", "amount_under"),
                deposit=live,
                paid_amount_cents=1000,
            )
            await session.commit()
            return outcome.created

    created = await asyncio.gather(*(worker() for _ in range(WORKERS)))
    assert sorted(created) == [False] * (WORKERS - 1) + [True]
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentAttentionItem)) == 1


async def test_a_flood_of_identical_webhooks_persists_and_enqueues_once(
    client: AsyncClient, maker, monkeypatch
):
    from secretaria.main import app

    tenant, _ = await _seed(maker)
    async with maker() as session:
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        slug = row.webhook_slug

    async def _session():
        async with maker() as session:
            yield session

    pool = FakeArqPool()
    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    body = json.dumps(
        {"id": "evt_flood", "event": "PAYMENT_RECEIVED",
         "payment": {"id": "pay_1", "value": 60.0, "externalReference": "dep-ref-1"}}
    )
    headers = {"asaas-access-token": TOKEN, "content-type": "application/json"}
    try:
        responses = await asyncio.gather(
            *(client.post(f"/webhooks/asaas/{slug}", content=body, headers=headers)
              for _ in range(WORKERS))
        )
    finally:
        app.dependency_overrides.pop(get_session, None)

    statuses = sorted(r.json()["status"] for r in responses)
    assert statuses == ["duplicate"] * (WORKERS - 1) + ["ok"]
    assert all(r.status_code == 200 for r in responses)
    assert len(pool.jobs) == 1
    async with maker() as session:
        assert await session.scalar(select(func.count()).select_from(PaymentWebhookEvent)) == 1
