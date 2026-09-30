"""Schema contract of the payment provider core (TASK-016, spec sections 6.1-6.5).

SQLite-level checks: the constraints that make replay, per-tenant isolation and the
attention-item dedupe safe are declared on the models themselves.
"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.core.database import Base
from secretaria.models import (
    BookingHold,
    ClinicPaymentAccount,
    Patient,
    PatientPaymentCustomer,
    PaymentAttentionItem,
    PaymentWebhookEvent,
    PixDeposit,
    Tenant,
)
from secretaria.models.pix_deposit import (
    DEPOSIT_TIMING_AFTER,
    FULFILLMENT_NONE,
    PixDepositStatus,
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _tenant(session: AsyncSession) -> Tenant:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    session.add(tenant)
    await session.flush()
    return tenant


async def test_tenant_payment_defaults_keep_today_behaviour(db):
    async with db() as session:
        tenant = await _tenant(session)
        await session.commit()
        await session.refresh(tenant)
    assert tenant.payment_provider == "asaas"
    assert tenant.deposit_timing == "after"
    assert tenant.deposit_hold_ttl_minutes == 30


async def test_booking_hold_phase_defaults_to_code(db):
    assert BookingHold.__table__.c.phase.server_default.arg == "code"
    assert BookingHold.__table__.c.charge_id.nullable is True


async def test_account_is_unique_per_tenant_and_provider_and_slug_is_unique(db):
    async with db() as session:
        tenant = await _tenant(session)
        session.add(ClinicPaymentAccount(tenant_id=tenant.id, provider="asaas", webhook_slug="s-1"))
        await session.commit()
    async with db() as session:
        session.add(ClinicPaymentAccount(tenant_id=tenant.id, provider="asaas", webhook_slug="s-2"))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with db() as session:
        other = await _tenant(session)
        session.add(ClinicPaymentAccount(tenant_id=other.id, provider="asaas", webhook_slug="s-1"))
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_account_defaults(db):
    async with db() as session:
        tenant = await _tenant(session)
        session.add(ClinicPaymentAccount(tenant_id=tenant.id, provider="asaas", webhook_slug="s-9"))
        await session.commit()
        row = await session.scalar(select(ClinicPaymentAccount))
    assert row.status == "pending"
    assert row.capabilities == {}
    assert row.excess_groups == []
    assert row.probe_inconclusive_days == 0
    assert row.api_key_encrypted is None


async def test_webhook_event_unique_per_tenant_provider_event_id(db):
    async with db() as session:
        a = await _tenant(session)
        b = await _tenant(session)
        session.add(PaymentWebhookEvent(tenant_id=a.id, provider="asaas", event_id="evt_1"))
        await session.commit()
    async with db() as session:
        session.add(PaymentWebhookEvent(tenant_id=a.id, provider="asaas", event_id="evt_1"))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with db() as session:
        # A forged event_id of clinic A never blocks the same id of clinic B.
        b_id = (await session.scalars(select(Tenant.id).where(Tenant.id != a.id))).first()
        session.add(PaymentWebhookEvent(tenant_id=b_id, provider="asaas", event_id="evt_1"))
        await session.commit()
        assert b_id == b.id


async def test_webhook_event_defaults(db):
    async with db() as session:
        tenant = await _tenant(session)
        session.add(PaymentWebhookEvent(tenant_id=tenant.id, provider="asaas", event_id="e"))
        await session.commit()
        row = await session.scalar(select(PaymentWebhookEvent))
    assert row.status == "received"
    assert row.attempts == 0
    assert row.payload is None
    assert row.received_at is not None


async def test_pix_deposit_can_exist_before_its_appointment(db):
    async with db() as session:
        tenant = await _tenant(session)
        deposit = PixDeposit(
            tenant_id=tenant.id, appointment_id=None, amount_cents=6000, percent_applied=30
        )
        session.add(deposit)
        await session.commit()
        await session.refresh(deposit)
    assert deposit.appointment_id is None
    assert deposit.provider == "asaas"
    assert deposit.deposit_timing == DEPOSIT_TIMING_AFTER
    assert deposit.fulfillment_status == FULFILLMENT_NONE
    assert deposit.status == PixDepositStatus.AWAITING


async def test_pix_deposit_provider_charge_id_is_unique_but_nulls_repeat(db):
    async with db() as session:
        tenant = await _tenant(session)
        for _ in range(2):  # NULL provider_charge_id may repeat
            session.add(
                PixDeposit(tenant_id=tenant.id, amount_cents=100, percent_applied=30)
            )
        session.add(
            PixDeposit(
                tenant_id=tenant.id,
                amount_cents=100,
                percent_applied=30,
                provider="asaas",
                provider_charge_id="pay_1",
            )
        )
        await session.commit()
    async with db() as session:
        tenant_id = (await session.scalars(select(Tenant.id))).first()
        session.add(
            PixDeposit(
                tenant_id=tenant_id,
                amount_cents=100,
                percent_applied=30,
                provider="asaas",
                provider_charge_id="pay_1",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_patient_payment_customer_unique_per_tenant_patient_provider(db):
    async with db() as session:
        tenant = await _tenant(session)
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="M")
        session.add(patient)
        await session.flush()
        session.add(
            PatientPaymentCustomer(
                tenant_id=tenant.id,
                patient_id=patient.id,
                provider="asaas",
                provider_customer_id="cus_1",
            )
        )
        await session.commit()
        session.add(
            PatientPaymentCustomer(
                tenant_id=tenant.id,
                patient_id=patient.id,
                provider="asaas",
                provider_customer_id="cus_2",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_attention_item_dedupe_key_is_unique_per_tenant(db):
    async with db() as session:
        a = await _tenant(session)
        b = await _tenant(session)
        session.add(
            PaymentAttentionItem(
                tenant_id=a.id, provider="asaas", reason="amount_under", dedupe_key="k"
            )
        )
        await session.commit()
        session.add(
            PaymentAttentionItem(
                tenant_id=b.id, provider="asaas", reason="amount_under", dedupe_key="k"
            )
        )
        await session.commit()  # another tenant may reuse the key
        session.add(
            PaymentAttentionItem(
                tenant_id=a.id, provider="asaas", reason="amount_under", dedupe_key="k"
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()


async def test_attention_item_defaults(db):
    async with db() as session:
        tenant = await _tenant(session)
        session.add(
            PaymentAttentionItem(
                tenant_id=tenant.id,
                provider="asaas",
                reason="late_slot_taken",
                dedupe_key="asaas:pay_1:late_slot_taken",
                paid_at=datetime(2026, 9, 29, tzinfo=UTC),
            )
        )
        await session.commit()
        row = await session.scalar(select(PaymentAttentionItem))
    assert row.status == "open"
    assert row.resolved_at is None
    assert row.detected_at is not None
