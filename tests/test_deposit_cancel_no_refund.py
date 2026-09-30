"""Cancelling a consultation with a deposit NEVER returns money (decision D15, spec 10A.6).

The refund policy fields (`pix_refund_window_hours`, `pix_retention_policy`,
`pix_partial_refund_percent`) only CALCULATE what is due; the clinic returns it in the
provider's own panel and marks the attention item resolved. `FakeAsaasClient.refund_payment`
raises if anything ever calls it.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select

from secretaria.models import Appointment, AppointmentStatus, Patient, PaymentAttentionItem, Tenant
from secretaria.models.pix_deposit import (
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_NONE,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, asaas_provider, deposit_lifecycle
from secretaria.services.payments.asaas import AsaasError
from tests.payments_support import FakeAsaasClient, make_sessionmaker


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


def _install(monkeypatch, **kw) -> FakeAsaasClient:
    kw.setdefault("payment", {"status": "PENDING", "value": 60.0, "externalReference": "r"})
    fake = FakeAsaasClient(**kw)
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    return fake


async def _seed(
    session,
    *,
    status=PixDepositStatus.PAID,
    hours_ahead: float | None = 48,
    window=24,
    policy="total",
    partial=50,
    charge_id="pay_1",
):
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=str(uuid4())[:12],
        pix_refund_window_hours=window,
        pix_retention_policy=policy,
        pix_partial_refund_percent=partial,
    )
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    session.add_all([tenant, patient])
    await session.flush()
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id="evt-1",
        appointment_type="Consulta",
        status=AppointmentStatus.CANCELLED,
        start_at=(datetime.now(UTC) + timedelta(hours=hours_ahead)) if hours_ahead else None,
    )
    session.add(appointment)
    row = await accounts.get_or_create_account_row(session, tenant_id=tenant.id, provider="asaas")
    cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
    row.status = "connected"
    await session.flush()
    deposit = PixDeposit(
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        patient_id=patient.id,
        amount_cents=6000,
        percent_applied=30,
        status=status,
        provider="asaas",
        provider_charge_id=charge_id,
        asaas_payment_id=charge_id,
        external_reference="r",
    )
    session.add(deposit)
    await session.commit()
    return tenant, appointment, deposit


async def _cancel(session, tenant, appointment, **kw):
    outcome = await deposit_lifecycle.on_appointment_cancelled(
        session, tenant=tenant, appointment=appointment, **kw
    )
    await session.commit()
    return outcome


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


async def test_an_early_patient_cancellation_asks_the_clinic_to_return_the_full_deposit(
    db, monkeypatch
):
    fake = _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, hours_ahead=48, window=24)
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)

    (item,) = await _items(db)
    assert outcome == "refund_due"
    assert (item.reason, item.suggested_refund_cents, item.amount_cents) == (
        "refund_due_policy",
        6000,
        6000,
    )
    assert item.service_label == "Consulta" and item.slot_start is not None
    assert deposit.status == PixDepositStatus.PAID  # nothing was refunded: the money is still here
    assert deposit.refunded_amount_cents is None
    assert deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION
    assert fake.calls == []  # no provider call at all


async def test_a_late_cancellation_with_total_retention_keeps_the_deposit_and_asks_nothing(
    db, monkeypatch
):
    fake = _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, hours_ahead=2, window=24)
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)

    assert outcome == "retained"
    assert deposit.status == PixDepositStatus.CANCELLED_RETAINED
    assert deposit.refunded_amount_cents == 0
    assert await _items(db) == [] and fake.calls == []


async def test_a_late_cancellation_with_partial_policy_asks_for_the_partial_amount(
    db, monkeypatch
):
    fake = _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(
            session, hours_ahead=2, window=24, policy="partial", partial=40
        )
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)

    (item,) = await _items(db)
    assert outcome == "partial_refund_due"
    assert (item.reason, item.suggested_refund_cents) == ("refund_due_policy", 2400)  # 40% of 60
    assert deposit.status == PixDepositStatus.PAID and fake.calls == []
    notice = deposit_lifecycle.cancellation_notice(outcome, tenant, deposit)
    assert "R$ 24,00" in notice and "clínica fará a devolução" in notice


async def test_a_cancellation_by_the_clinic_always_asks_for_the_full_amount(db, monkeypatch):
    fake = _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(
            session, hours_ahead=1, window=24, policy="partial"
        )
        outcome = await _cancel(session, tenant, appointment, cancelled_by="clinic")

    (item,) = await _items(db)
    assert outcome == "refund_due"
    assert (item.reason, item.suggested_refund_cents) == ("paid_then_cancelled", 6000)
    assert fake.calls == []


async def test_an_unknown_start_time_counts_as_early(db, monkeypatch):
    _install(monkeypatch)
    async with db() as session:
        tenant, appointment, _ = await _seed(session, hours_ahead=None)
        assert await _cancel(session, tenant, appointment) == "refund_due"


async def test_cancelling_twice_is_a_safe_noop_and_never_duplicates_the_item(db, monkeypatch):
    _install(monkeypatch)
    async with db() as session:
        tenant, appointment, _ = await _seed(session)
        first = await _cancel(session, tenant, appointment)
        second = await _cancel(session, tenant, appointment)
        third = await _cancel(session, tenant, appointment, cancelled_by="clinic")
    assert (first, second, third) == ("refund_due", None, None)
    assert len(await _items(db)) == 1


async def test_an_unpaid_deposit_is_voided_at_the_provider_then_expired(db, monkeypatch):
    fake = _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, status=PixDepositStatus.AWAITING)
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)

    assert outcome == "voided"
    assert [c[0] for c in fake.calls] == ["get_payment", "delete_payment"]
    assert (deposit.status, deposit.resolved_at is not None) == (PixDepositStatus.EXPIRED, True)
    assert deposit.fulfillment_status == FULFILLMENT_NONE


async def test_a_charge_the_provider_says_is_already_paid_is_never_deleted(db, monkeypatch):
    fake = _install(
        monkeypatch, payment={"status": "RECEIVED", "value": 60.0, "externalReference": "r"}
    )
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, status=PixDepositStatus.AWAITING)
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)

    assert outcome == "payment_in_flight"
    assert [c[0] for c in fake.calls] == ["get_payment"]  # no delete of a paid charge
    assert deposit.status == PixDepositStatus.AWAITING  # the webhook/reconciliation settles it


async def test_unknown_provider_state_keeps_the_charge_open_for_reconciliation(db, monkeypatch):
    fake = _install(monkeypatch, errors={"get_payment": AsaasError(503)})
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, status=PixDepositStatus.AWAITING)
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)
    assert outcome == "payment_in_flight" and deposit.status == PixDepositStatus.AWAITING
    assert "delete_payment" not in [c[0] for c in fake.calls]


async def test_a_failed_void_keeps_the_charge_open_for_reconciliation(db, monkeypatch):
    _install(monkeypatch, errors={"delete_payment": AsaasError(500)})
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, status=PixDepositStatus.AWAITING)
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)
    assert outcome == "payment_in_flight" and deposit.status == PixDepositStatus.AWAITING


async def test_no_show_voids_an_unpaid_deposit_and_retains_a_paid_one(db, monkeypatch):
    fake = _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, status=PixDepositStatus.AWAITING)
        assert await deposit_lifecycle.on_no_show(
            session, tenant=tenant, appointment=appointment
        ) == "voided"
    assert "delete_payment" in [c[0] for c in fake.calls]

    async with db() as session:
        tenant, appointment, deposit = await _seed(session, charge_id="pay_2")
        assert await deposit_lifecycle.on_no_show(
            session, tenant=tenant, appointment=appointment
        ) == "retained"
        assert deposit.status == PixDepositStatus.NO_SHOW_RETAINED


def test_every_patient_notice_promises_a_manual_return_never_an_automatic_refund():
    tenant = Tenant(
        id=uuid4(), clinic_name="C", pix_refund_window_hours=24, pix_partial_refund_percent=50
    )
    deposit = PixDeposit(tenant_id=tenant.id, amount_cents=6000, percent_applied=30)
    notices = {
        outcome: deposit_lifecycle.cancellation_notice(outcome, tenant, deposit)
        for outcome in (
            "refund_due",
            "partial_refund_due",
            "retained",
            "payment_in_flight",
            "voided",
            None,
            "refunded",
            "refund_failed",
        )
    }
    assert notices["voided"] is None and notices[None] is None
    assert notices["refunded"] is None and notices["refund_failed"] is None  # gone outcomes
    assert "clínica fará a devolução" in notices["refund_due"]
    assert "R$ 30,00" in notices["partial_refund_due"]  # 50% of R$ 60,00
    assert "menos de 24h" in notices["retained"]
    assert "clínica" in notices["payment_in_flight"]
    for text in notices.values():
        assert text is None or "estorno" not in text.lower()
