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


async def _use_every_reschedule(session, tenant, deposit, *, limit=2):
    tenant.pix_reschedule_limit = limit
    deposit.reschedule_count = limit
    await session.commit()


async def test_a_patient_who_used_every_reschedule_loses_the_deposit_even_when_cancelling_early(
    db, monkeypatch
):
    fake = _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, hours_ahead=200, window=24)
        await _use_every_reschedule(session, tenant, deposit)
        preview = deposit_lifecycle.preview_cancellation_outcome(tenant, deposit, appointment)
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)

    assert preview == outcome == "retained"
    assert deposit.status == PixDepositStatus.CANCELLED_RETAINED
    assert deposit.refunded_amount_cents == 0
    assert await _items(db) == [] and fake.calls == []
    assert "já usou as 2 remarcações" in deposit_lifecycle.cancellation_notice(
        outcome, tenant, deposit
    )


async def test_the_reschedule_limit_beats_a_partial_refund_policy(db, monkeypatch):
    _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(
            session, hours_ahead=2, window=24, policy="partial"
        )
        await _use_every_reschedule(session, tenant, deposit)
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(deposit)

    assert outcome == "retained" and deposit.status == PixDepositStatus.CANCELLED_RETAINED
    assert await _items(db) == []


async def test_a_cancellation_by_the_clinic_is_still_refunded_at_the_reschedule_limit(
    db, monkeypatch
):
    _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, hours_ahead=200)
        await _use_every_reschedule(session, tenant, deposit)
        outcome = await _cancel(
            session, tenant, appointment, cancelled_by=deposit_lifecycle.CANCELLED_BY_CLINIC
        )

    (item,) = await _items(db)
    assert outcome == "refund_due" and item.reason == "paid_then_cancelled"


async def test_below_the_reschedule_limit_and_with_no_allowance_nothing_changes(db, monkeypatch):
    _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, hours_ahead=200)
        deposit.reschedule_count = 1
        await session.commit()
        assert not deposit_lifecycle.at_reschedule_limit(tenant, deposit)
        tenant.pix_reschedule_limit = 0  # no allowance configured: never the retention rule
        deposit.reschedule_count = 5
        assert not deposit_lifecycle.at_reschedule_limit(tenant, deposit)


# --------------------------------------------------------------------------
# service-change adjustments (plan 2026-10-26 Task 8; the full journey is in
# tests/test_service_change_cancel.py): the cancellation figures use what the clinic HOLDS
# --------------------------------------------------------------------------


def _adjustment(tenant, appointment, deposit, *, kind, status, diff, charge_id=None):
    from secretaria.models.deposit_adjustment import DepositAdjustment

    return DepositAdjustment(
        id=uuid4(),
        tenant_id=tenant.id,
        deposit_id=deposit.id,
        appointment_id=appointment.id,
        kind=kind,
        status=status,
        old_service="Consulta",
        new_service="Outro",
        diff_cents=diff,
        provider_charge_id=charge_id,
    )


async def test_a_paid_difference_joins_the_refund_and_an_unpaid_one_is_voided(db, monkeypatch):
    from secretaria.models.deposit_adjustment import (
        KIND_DECREASE,
        KIND_INCREASE,
        STATUS_APPLIED,
        STATUS_AWAITING_PAYMENT,
        STATUS_CANCELLED,
        STATUS_REFUND_REQUESTED,
    )

    fake = _install(monkeypatch)
    async with db() as session:
        tenant, appointment, deposit = await _seed(session, hours_ahead=48, window=24)
        paid = _adjustment(
            tenant, appointment, deposit, kind=KIND_INCREASE, status=STATUS_APPLIED, diff=1500,
            charge_id="pay_diff_paid",
        )
        asked_back = _adjustment(
            tenant, appointment, deposit, kind=KIND_DECREASE, status=STATUS_REFUND_REQUESTED,
            diff=500,
        )
        unpaid = _adjustment(
            tenant, appointment, deposit, kind=KIND_INCREASE, status=STATUS_AWAITING_PAYMENT,
            diff=900, charge_id="pay_diff_open",
        )
        session.add_all([paid, asked_back, unpaid])
        await session.commit()
        outcome = await _cancel(session, tenant, appointment)
        await session.refresh(unpaid)
        await session.refresh(paid)

    (item,) = await _items(db)
    assert outcome == "refund_due"
    # R$ 60 + R$ 15 paid - R$ 5 already asked back (its own item); the unpaid R$ 9 is not money.
    assert (item.reason, item.suggested_refund_cents) == ("refund_due_policy", 7000)
    assert fake.calls == [("get_payment", "pay_diff_open"), ("delete_payment", "pay_diff_open")]
    assert (unpaid.status, paid.status) == (STATUS_CANCELLED, STATUS_APPLIED)


def test_the_rule_the_patient_reads_names_the_limit_and_the_loss():
    tenant = Tenant(id=uuid4(), clinic_name="C", phone_number_id="x", pix_reschedule_limit=2)
    text = deposit_lifecycle.reschedule_rules_text(tenant)
    assert "no máximo 2 vezes" in text
    assert "não será possível remarcar" in text
    assert "o sinal fica com a clínica, sem devolução" in text
    tenant.pix_reschedule_limit = 1
    assert "no máximo 1 vez." in deposit_lifecycle.reschedule_rules_text(tenant)
