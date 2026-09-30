"""The arq jobs of the payment core (workers/payments_tasks.py) and the fulfillment seam
(services/payments/fulfillment.py)."""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    PaymentAttentionItem,
    PaymentWebhookEvent,
    PixDeposit,
    Tenant,
)
from secretaria.models.pix_deposit import (
    FULFILLMENT_CONFIRMED,
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_NONE,
    FULFILLMENT_PROMOTING,
    PixDepositStatus,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, alerts, deposit_lifecycle, fulfillment, registry
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.services.payments.webhook_payload import event_to_dict
from secretaria.workers import payments_tasks
from tests.payments_support import (
    FakeArqPool,
    FakeProvider,
    FakeRedis,
    make_event as _event,
    make_sessionmaker,
)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(payments_tasks, "async_session_factory", maker)
    yield maker
    await engine.dispose()


async def _seed(db, *, timing="after", status=PixDepositStatus.AWAITING, snapshot=None,
                appointment_status=AppointmentStatus.SCHEDULED, contact_email="c@example.test"):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            contact_email=contact_email,
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add_all([tenant, patient])
        await session.flush()
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="token")
        appointment = None
        if timing == "after":
            appointment = Appointment(
                id=uuid4(),
                tenant_id=tenant.id,
                patient_id=patient.id,
                google_event_id="e",
                appointment_type="Consulta",
                status=appointment_status,
            )
            session.add(appointment)
            await session.flush()
        deposit = PixDeposit(
            tenant_id=tenant.id,
            appointment_id=appointment.id if appointment else None,
            patient_id=patient.id,
            amount_cents=6000,
            percent_applied=30,
            status=status,
            provider="asaas",
            provider_charge_id="pay_1",
            external_reference="dep-ref-1",
            deposit_timing=timing,
            booking_snapshot=snapshot,
        )
        session.add(deposit)
        event = _event()
        stored = PaymentWebhookEvent(
            tenant_id=tenant.id,
            provider="asaas",
            event_id=event.event_id,
            provider_charge_id="pay_1",
            event_type=event.event_type,
            payload={"normalized": event_to_dict(event)},
        )
        session.add(stored)
        await session.commit()
        return tenant, deposit, stored


def _provider(monkeypatch, **kw) -> FakeProvider:
    fake = FakeProvider(**kw)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


def _paid() -> ChargeState:
    return ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=6000, external_reference="dep-ref-1"
    )


@pytest.fixture
def notices(monkeypatch):
    seen = {"paid": [], "expired": []}

    async def paid(session, tenant, deposit):
        seen["paid"].append(deposit.id)

    async def expired(session, tenant, appointment):
        seen["expired"].append(appointment.id)

    monkeypatch.setattr(deposit_lifecycle, "_notify_paid", paid)
    monkeypatch.setattr(deposit_lifecycle, "_notify_expired", expired)
    return seen


# --------------------------------------------------------------------------
# process_payment_event
# --------------------------------------------------------------------------


async def test_job_pays_then_notifies_after_commit(db, monkeypatch, notices):
    _provider(monkeypatch, state=_paid())
    tenant, deposit, stored = await _seed(db)
    pool = FakeArqPool()
    await payments_tasks.process_payment_event({"redis": pool}, str(stored.id))

    async with db() as session:
        assert (await session.get(PixDeposit, deposit.id)).status == PixDepositStatus.PAID
    assert notices["paid"] == [deposit.id]
    assert pool.jobs == []


async def test_a_failing_notice_never_fails_the_job_nor_undoes_the_payment(db, monkeypatch):
    _provider(monkeypatch, state=_paid())

    async def boom(session, tenant, deposit):
        raise RuntimeError("whatsapp down")

    monkeypatch.setattr(deposit_lifecycle, "_notify_paid", boom)
    tenant, deposit, stored = await _seed(db)
    await payments_tasks.process_payment_event({"redis": FakeArqPool()}, str(stored.id))
    async with db() as session:
        assert (await session.get(PixDeposit, deposit.id)).status == PixDepositStatus.PAID


async def test_a_provider_outage_is_counted_then_re_raised_for_the_arq_retry(db, monkeypatch):
    _provider(
        monkeypatch,
        get_error=ProviderError("get_charge_failed", status_code=503, retryable=True),
    )
    tenant, deposit, stored = await _seed(db)

    with pytest.raises(ProviderError):
        await payments_tasks.process_payment_event({"redis": FakeArqPool()}, str(stored.id))

    async with db() as session:
        row = await session.get(PaymentWebhookEvent, stored.id)
        assert (row.attempts, row.status, row.error_code) == (1, "received", "get_charge_failed")
        assert (await session.get(PixDeposit, deposit.id)).status == PixDepositStatus.AWAITING


async def test_a_paid_before_charge_is_handed_to_the_fulfillment_job(db, monkeypatch, notices):
    _provider(monkeypatch, state=_paid())
    tenant, deposit, stored = await _seed(db, timing="before")
    pool = FakeArqPool()
    await payments_tasks.process_payment_event({"redis": pool}, str(stored.id))

    assert pool.jobs == [("fulfill_paid_charge_job", (str(deposit.id),), {})]
    assert notices["paid"] == []  # the promoter tells the patient, not this job


async def test_an_overdue_on_a_confirmed_consultation_alerts_the_clinic(db, monkeypatch, notices):
    _provider(monkeypatch, state=ChargeState(status=PaymentStatus.EXPIRED))
    sent = []

    async def fake_send(to_email, clinic_name, kind):
        sent.append(kind)

    monkeypatch.setattr(alerts.email, "send_payment_alert", fake_send)
    tenant, deposit, stored = await _seed(db, appointment_status=AppointmentStatus.CONFIRMED)
    async with db() as session:
        stored = await session.get(PaymentWebhookEvent, stored.id)
        stored.payload = {
            "normalized": event_to_dict(_event(PaymentStatus.EXPIRED, event_type="PAYMENT_OVERDUE"))
        }
        await session.commit()
    await payments_tasks.process_payment_event({"redis": FakeRedis()}, str(stored.id))
    assert sent == []
    assert notices["expired"] == []


# --------------------------------------------------------------------------
# fulfillment seam
# --------------------------------------------------------------------------


async def test_fulfilling_an_after_charge_marks_it_confirmed(db):
    tenant, deposit, _ = await _seed(db, status=PixDepositStatus.PAID)
    async with db() as session:
        outcome = await fulfillment.fulfill_paid_charge(session, deposit_id=deposit.id)
        await session.commit()
        assert outcome == "confirmed_after"
        after = await session.get(PixDeposit, deposit.id)
        assert after.fulfillment_status == FULFILLMENT_CONFIRMED


async def test_a_before_charge_is_never_swallowed_while_no_promoter_exists(db):
    slot = {
        "start_at": "2026-10-01T13:00:00+00:00",
        "end_at": "2026-10-01T13:30:00+00:00",
        "appointment_type": "Consulta",
    }
    tenant, deposit, _ = await _seed(
        db, timing="before", status=PixDepositStatus.PAID, snapshot=slot
    )
    async with db() as session:
        first = await fulfillment.fulfill_paid_charge(session, deposit_id=deposit.id)
        second = await fulfillment.fulfill_paid_charge(session, deposit_id=deposit.id)
        await session.commit()
        items = (await session.scalars(select(PaymentAttentionItem))).all()
        after = await session.get(PixDeposit, deposit.id)

    assert (first, second) == ("promotion_not_available", "noop")
    assert [i.reason for i in items] == ["promotion_failed"]
    assert items[0].slot_start is not None
    assert items[0].service_label == "Consulta"
    assert after.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION


@pytest.mark.parametrize(
    ("status", "expected"),
    [(PixDepositStatus.AWAITING, "not_paid"), (PixDepositStatus.EXPIRED, "not_paid")],
)
async def test_fulfillment_ignores_a_charge_that_is_not_paid(db, status, expected):
    tenant, deposit, _ = await _seed(db, status=status)
    async with db() as session:
        assert await fulfillment.fulfill_paid_charge(session, deposit_id=deposit.id) == expected
        assert (
            await session.get(PixDeposit, deposit.id)
        ).fulfillment_status == FULFILLMENT_NONE


async def test_fulfillment_of_a_missing_charge_is_a_noop(db):
    async with db() as session:
        assert await fulfillment.fulfill_paid_charge(session, deposit_id=uuid4()) == "missing"


async def test_the_fulfillment_job_commits_its_outcome(db):
    tenant, deposit, _ = await _seed(db, status=PixDepositStatus.PAID)
    await payments_tasks.fulfill_paid_charge_job({}, str(deposit.id))
    async with db() as session:
        after = await session.get(PixDeposit, deposit.id)
    assert after.fulfillment_status == FULFILLMENT_CONFIRMED
    assert FULFILLMENT_PROMOTING != after.fulfillment_status
