"""The event core (services/payments/event_processor.py): what a normalised payment event
does to a deposit, an appointment and the clinic's attention list (spec sections 8.2-8.6, 10).

Every scenario runs against a scripted `FakeProvider`, so what is under test is OUR decision
logic - in particular that the provider (`get_charge`) is the authority before money state
changes, that "paid" happens exactly once, and that no path ever refunds.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

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
    FULFILLMENT_PROMOTING,
    PixDepositStatus,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, event_processor, registry
from secretaria.services.payments.provider import (
    ChargeState,
    NormalizedPaymentEvent,
    PaymentStatus,
    ProviderError,
)
from secretaria.services.payments.webhook_payload import event_to_dict
from tests.payments_support import FakeProvider, make_event as _event, make_sessionmaker

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _seed(
    session,
    *,
    timing="after",
    appointment_status=AppointmentStatus.SCHEDULED,
    deposit_status=PixDepositStatus.AWAITING,
    charge_id="pay_1",
    external_reference="dep-ref-1",
    amount=6000,
    with_appointment=True,
    legacy_charge_id_only=False,
):
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    session.add_all([tenant, patient])
    await session.flush()
    row = await accounts.get_or_create_account_row(session, tenant_id=tenant.id, provider="asaas")
    cfg.set_payment_account_secrets(row, api_key="key", webhook_token="token")
    appointment = None
    if with_appointment:
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id="evt-1",
            appointment_type="Consulta",
            phone=patient.wa_id,
            status=appointment_status,
        )
        session.add(appointment)
        await session.flush()
    deposit = PixDeposit(
        tenant_id=tenant.id,
        appointment_id=appointment.id if appointment else None,
        patient_id=patient.id,
        amount_cents=amount,
        percent_applied=30,
        status=deposit_status,
        provider="asaas",
        provider_charge_id=None if legacy_charge_id_only else charge_id,
        asaas_payment_id=charge_id if legacy_charge_id_only else None,
        external_reference=external_reference,
        deposit_timing=timing,
    )
    session.add(deposit)
    await session.commit()
    return tenant, patient, appointment, deposit


async def _row(session, tenant, event: NormalizedPaymentEvent, **kw) -> PaymentWebhookEvent:
    row = PaymentWebhookEvent(
        tenant_id=tenant.id,
        provider="asaas",
        event_id=event.event_id,
        provider_charge_id=event.provider_charge_id,
        event_type=event.event_type,
        payload={"normalized": event_to_dict(event)},
        **kw,
    )
    session.add(row)
    await session.commit()
    return row


def _paid_state(amount=6000, reference="dep-ref-1") -> ChargeState:
    return ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=amount, external_reference=reference
    )


def _provider(monkeypatch, **kw) -> FakeProvider:
    fake = FakeProvider(**kw)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _process(db, tenant, event, *, fresh_row=True, row=None):
    async with db() as session:
        row = row or await _row(session, tenant, event)
        return await event_processor.process_payment_event_row(session, row.id, now=NOW)


async def _deposit_now(db, deposit_id) -> PixDeposit:
    async with db() as session:
        return await session.get(PixDeposit, deposit_id)


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


# --------------------------------------------------------------------------
# paid: the provider is the authority, and "paid" happens once
# --------------------------------------------------------------------------


async def test_paid_event_is_confirmed_by_the_provider_then_marked_paid_once(db, monkeypatch):
    fake = _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, appointment, deposit = await _seed(session)
    result = await _process(db, tenant, _event())

    after = await _deposit_now(db, deposit.id)
    assert result.outcome == "paid" and result.notify_paid_deposit_id == deposit.id
    assert after.status == PixDepositStatus.PAID
    assert (after.paid_amount_cents, after.confirming_event_id) == (6000, "evt_1")
    assert after.fulfillment_status == FULFILLMENT_CONFIRMED
    assert after.paid_at is not None
    assert fake.called("get_charge") == 1
    assert await _items(db) == []
    async with db() as session:
        stored = await session.scalar(select(PaymentWebhookEvent))
    assert stored.status == "processed" and stored.processed_at is not None


async def test_received_then_confirmed_for_the_same_payment_is_paid_only_once(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
    first = await _process(db, tenant, _event(event_id="evt_received"))
    second = await _process(
        db, tenant, _event(event_id="evt_confirmed", event_type="PAYMENT_CONFIRMED")
    )

    after = await _deposit_now(db, deposit.id)
    assert first.outcome == "paid" and second.outcome == "already_paid"
    assert second.notify_paid_deposit_id is None
    assert after.confirming_event_id == "evt_received"
    assert await _items(db) == []


async def test_replaying_a_processed_event_row_does_nothing_and_never_asks_the_provider(
    db, monkeypatch
):
    fake = _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
        row = await _row(session, tenant, _event())
    first = await _process(db, tenant, _event(), row=row)
    replay = await _process(db, tenant, _event(), row=row)

    assert (first.outcome, replay.outcome) == ("paid", "replay_noop")
    assert fake.called("get_charge") == 1
    assert (await _deposit_now(db, deposit.id)).status == PixDepositStatus.PAID


async def test_a_forged_paid_event_for_an_unpaid_charge_changes_nothing(db, monkeypatch):
    _provider(monkeypatch, state=ChargeState(status=PaymentStatus.PENDING))
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
    result = await _process(db, tenant, _event())

    after = await _deposit_now(db, deposit.id)
    assert result.outcome == "ignored_not_paid_at_provider"
    assert after.status == PixDepositStatus.AWAITING and after.paid_at is None
    assert result.notify_paid_deposit_id is None
    async with db() as session:
        assert (await session.scalar(select(PaymentWebhookEvent))).status == "ignored"


async def test_a_paid_event_for_a_charge_the_provider_does_not_know_is_ignored(db, monkeypatch):
    _provider(monkeypatch, get_error=ProviderError("get_charge_failed", status_code=404))
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
    result = await _process(db, tenant, _event())
    assert result.outcome == "ignored_not_paid_at_provider"
    assert (await _deposit_now(db, deposit.id)).status == PixDepositStatus.AWAITING


async def test_a_provider_outage_while_confirming_propagates_and_moves_no_money_state(
    db, monkeypatch
):
    _provider(
        monkeypatch,
        get_error=ProviderError("get_charge_failed", status_code=503, retryable=True),
    )
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
        deposit_id = deposit.id
        row = await _row(session, tenant, _event())
        with pytest.raises(ProviderError):
            await event_processor.process_payment_event_row(session, row.id, now=NOW)
        await session.rollback()
    assert (await _deposit_now(db, deposit_id)).status == PixDepositStatus.AWAITING
    async with db() as session:
        assert (await session.scalar(select(PaymentWebhookEvent))).status == "received"


async def test_a_reference_the_provider_reports_differently_opens_an_item_and_does_not_pay(
    db, monkeypatch
):
    _provider(monkeypatch, state=_paid_state(reference="someone-elses-ref"))
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
    result = await _process(db, tenant, _event())

    items = await _items(db)
    assert result.outcome == "ignored_mismatch"
    assert [i.reason for i in items] == ["reference_mismatch"]
    after = await _deposit_now(db, deposit.id)
    assert after.status == PixDepositStatus.AWAITING
    assert after.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION


async def test_a_deposit_that_predates_the_core_is_matched_by_its_appointment_id(db, monkeypatch):
    fake = _provider(monkeypatch)
    async with db() as session:
        tenant, _, appointment, deposit = await _seed(session, external_reference=None)
    fake.state = _paid_state(reference=str(appointment.id))
    result = await _process(db, tenant, _event(reference=str(appointment.id)))
    assert result.outcome == "paid"


async def test_a_deposit_written_by_pre_core_code_is_found_by_the_legacy_payment_id(
    db, monkeypatch
):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, _, deposit = await _seed(session, legacy_charge_id_only=True)
    result = await _process(db, tenant, _event())
    assert result.outcome == "paid"
    assert (await _deposit_now(db, deposit.id)).status == PixDepositStatus.PAID


async def test_a_smaller_amount_is_not_promoted_and_asks_the_clinic(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state(amount=1000))
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
    result = await _process(db, tenant, _event(amount=1000))

    (item,) = await _items(db)
    assert result.outcome == "amount_under"
    assert (item.reason, item.paid_amount_cents, item.amount_cents) == ("amount_under", 1000, 6000)
    assert (await _deposit_now(db, deposit.id)).status == PixDepositStatus.AWAITING
    assert result.notify_paid_deposit_id is None


async def test_a_larger_amount_is_paid_and_flagged_and_stays_flagged(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state(amount=9000))
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
    result = await _process(db, tenant, _event(amount=9000))

    after = await _deposit_now(db, deposit.id)
    assert result.outcome == "paid" and result.notify_paid_deposit_id == deposit.id
    assert after.status == PixDepositStatus.PAID
    assert [i.reason for i in await _items(db)] == ["amount_over"]
    # `needs_attention` is sticky: the later `confirmed` step never overwrites it.
    assert after.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION


async def test_paid_after_the_appointment_was_cancelled_opens_paid_then_cancelled(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, _, deposit = await _seed(
            session, appointment_status=AppointmentStatus.CANCELLED
        )
    result = await _process(db, tenant, _event())

    assert result.outcome == "paid_appointment_cancelled"
    assert result.notify_paid_deposit_id is None
    assert [i.reason for i in await _items(db)] == ["paid_then_cancelled"]
    assert (await _deposit_now(db, deposit.id)).status == PixDepositStatus.PAID


async def test_a_payment_after_overdue_cancelled_the_consultation_is_flagged_not_refunded(
    db, monkeypatch
):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, _, deposit = await _seed(
            session,
            deposit_status=PixDepositStatus.EXPIRED,
            appointment_status=AppointmentStatus.CANCELLED,
        )
    result = await _process(db, tenant, _event())

    after = await _deposit_now(db, deposit.id)
    assert result.outcome == "paid_appointment_cancelled"
    assert after.status == PixDepositStatus.PAID  # the money arrived: the row says so
    assert [i.reason for i in await _items(db)] == ["paid_then_cancelled"]


async def test_after_a_paid_appointment_without_a_row_is_an_orphan(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, _, deposit = await _seed(session, with_appointment=False)
    result = await _process(db, tenant, _event())
    assert result.outcome == "paid_appointment_missing"
    assert [i.reason for i in await _items(db)] == ["orphan_payment"]


async def test_before_timing_hands_the_paid_charge_to_fulfillment(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, _, deposit = await _seed(session, timing="before", with_appointment=False)
    result = await _process(db, tenant, _event())

    after = await _deposit_now(db, deposit.id)
    assert result.outcome == "paid_before"
    assert result.fulfill_deposit_id == deposit.id and result.notify_paid_deposit_id is None
    assert after.status == PixDepositStatus.PAID
    assert after.fulfillment_status == FULFILLMENT_PROMOTING


# --------------------------------------------------------------------------
# expiry (OVERDUE / DELETED): never cancels what the clinic or patient confirmed
# --------------------------------------------------------------------------


async def test_overdue_cancels_a_scheduled_appointment_and_reports_the_slot_freed(db, monkeypatch):
    _provider(monkeypatch, state=ChargeState(status=PaymentStatus.EXPIRED))
    async with db() as session:
        tenant, _, appointment, deposit = await _seed(session)
    result = await _process(
        db, tenant, _event(PaymentStatus.EXPIRED, event_type="PAYMENT_OVERDUE", event_id="evt_o")
    )

    async with db() as session:
        appt = await session.get(Appointment, appointment.id)
    after = await _deposit_now(db, deposit.id)
    assert result.outcome == "expired" and result.notify_expired_appointment_id == appointment.id
    assert appt.status == AppointmentStatus.CANCELLED
    assert (after.status, after.resolved_at is not None) == (PixDepositStatus.EXPIRED, True)


async def test_stale_overdue_does_not_cancel_provider_paid_charge(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, appointment, deposit = await _seed(session)

    result = await _process(
        db, tenant, _event(PaymentStatus.EXPIRED, event_type="PAYMENT_OVERDUE", event_id="late"),
    )
    async with db() as session:
        current_appointment = await session.get(Appointment, appointment.id)
        current_deposit = await session.get(PixDeposit, deposit.id)

    assert current_appointment.status == AppointmentStatus.SCHEDULED
    assert current_deposit.status == PixDepositStatus.PAID
    assert result.notify_expired_appointment_id is None


@pytest.mark.parametrize(
    "status",
    [
        AppointmentStatus.CONFIRMED,
        AppointmentStatus.ATTENDED,
        AppointmentStatus.RESCHEDULED,
        AppointmentStatus.NO_SHOW,
    ],
)
async def test_overdue_never_cancels_a_consultation_that_is_already_confirmed_or_past(
    db, monkeypatch, status
):
    _provider(monkeypatch, state=ChargeState(status=PaymentStatus.EXPIRED))
    async with db() as session:
        tenant, _, appointment, deposit = await _seed(session, appointment_status=status)
    result = await _process(
        db, tenant, _event(PaymentStatus.EXPIRED, event_type="PAYMENT_OVERDUE", event_id="evt_o")
    )

    async with db() as session:
        appt = await session.get(Appointment, appointment.id)
    assert appt.status == status  # untouched
    assert result.outcome == "expired_appointment_kept"
    assert result.alert_kind == "overdue_kept" and result.notify_expired_appointment_id is None
    assert (await _deposit_now(db, deposit.id)).status == PixDepositStatus.EXPIRED


async def test_our_own_void_followed_by_the_providers_deleted_event_is_a_noop(db, monkeypatch):
    _provider(monkeypatch)
    async with db() as session:
        tenant, _, appointment, deposit = await _seed(
            session,
            deposit_status=PixDepositStatus.EXPIRED,
            appointment_status=AppointmentStatus.CANCELLED,
        )
    result = await _process(
        db, tenant, _event(PaymentStatus.EXPIRED, event_type="PAYMENT_DELETED", event_id="evt_d")
    )
    assert result.outcome == "expired_replay" and result.notify_expired_appointment_id is None


async def test_overdue_on_a_paid_deposit_changes_nothing(db, monkeypatch):
    _provider(monkeypatch)
    async with db() as session:
        tenant, _, appointment, deposit = await _seed(
            session, deposit_status=PixDepositStatus.PAID
        )
    result = await _process(
        db, tenant, _event(PaymentStatus.EXPIRED, event_type="PAYMENT_OVERDUE", event_id="evt_o")
    )
    async with db() as session:
        appt = await session.get(Appointment, appointment.id)
    assert result.outcome == "ignored_expired_on_paid"
    assert appt.status == AppointmentStatus.SCHEDULED
    assert await _items(db) == []


# --------------------------------------------------------------------------
# refunded, unknown charges, isolation
# --------------------------------------------------------------------------


async def test_a_refund_done_in_the_providers_panel_opens_an_informative_item(db, monkeypatch):
    _provider(monkeypatch)
    async with db() as session:
        tenant, _, _, deposit = await _seed(session, deposit_status=PixDepositStatus.PAID)
        deposit_row = await session.get(PixDeposit, deposit.id)
        deposit_row.paid_at = NOW - timedelta(days=1)
        await session.commit()
    result = await _process(
        db,
        tenant,
        _event(PaymentStatus.REFUNDED, event_type="PAYMENT_REFUNDED", event_id="evt_r"),
    )
    assert result.outcome == "refund_observed"
    assert [i.reason for i in await _items(db)] == ["refunded_externally"]


async def test_a_refund_on_a_charge_with_an_open_item_only_stamps_it(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state(reference="other"))
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
    await _process(db, tenant, _event(event_id="evt_p"))  # opens reference_mismatch
    async with db() as session:
        row = await session.get(PixDeposit, deposit.id)
        row.status, row.paid_at = PixDepositStatus.PAID, NOW
        await session.commit()
    result = await _process(
        db,
        tenant,
        _event(PaymentStatus.REFUNDED, event_type="PAYMENT_REFUNDED", event_id="evt_r"),
    )

    (item,) = await _items(db)
    assert result.outcome == "refund_observed"
    assert item.reason == "reference_mismatch" and item.status == "open"
    assert item.provider_refund_seen_at is not None


async def test_an_unknown_charge_is_ignored(db, monkeypatch):
    _provider(monkeypatch)
    async with db() as session:
        tenant, _, _, _ = await _seed(session)
    result = await _process(db, tenant, _event(charge_id="pay_nobody", reference="x"))
    assert result.outcome == "ignored_unknown_charge" and await _items(db) == []


async def test_an_unknown_charge_carrying_our_reference_becomes_an_orphan_item(db, monkeypatch):
    _provider(monkeypatch)
    async with db() as session:
        tenant, _, _, _ = await _seed(session)
    result = await _process(db, tenant, _event(charge_id="pay_adopt", reference="dep-ref-1"))

    (item,) = await _items(db)
    assert result.outcome == "orphan_payment"
    assert (item.reason, item.provider_charge_id, item.deposit_id) == (
        "orphan_payment",
        "pay_adopt",
        None,
    )


async def test_another_clinics_charge_is_never_matched_by_this_clinics_event(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        _, _, _, deposit_b = await _seed(session)  # clinic B owns charge pay_1
        tenant_a, _, _, _ = await _seed(session, charge_id="pay_a", external_reference="ref-a")
    result = await _process(db, tenant_a, _event(charge_id="pay_1", reference="dep-ref-1"))

    assert result.outcome in {"ignored_unknown_charge", "orphan_payment"}
    assert (await _deposit_now(db, deposit_b.id)).status == PixDepositStatus.AWAITING


async def test_a_synthetic_event_is_processed_like_a_webhook(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state())
    async with db() as session:
        tenant, _, _, deposit = await _seed(session)
        row = PaymentWebhookEvent(
            tenant_id=tenant.id,
            provider="asaas",
            event_id="reconcile:pay_1:PAID",
            provider_charge_id="pay_1",
            event_type="reconcile",
            payload={"normalized": event_to_dict(_event(event_id="reconcile:pay_1:PAID")),
                     "synthetic": True},
        )
        session.add(row)
        await session.commit()
        result = await event_processor.process_payment_event_row(session, row.id, now=NOW)
    assert result.outcome == "paid"


@pytest.mark.parametrize("payload", [None, {}, {"raw": {}}])
async def test_a_row_without_a_normalised_event_is_ignored(db, monkeypatch, payload):
    _provider(monkeypatch)
    async with db() as session:
        tenant, _, _, _ = await _seed(session)
        row = PaymentWebhookEvent(
            tenant_id=tenant.id, provider="asaas", event_id="e", payload=payload
        )
        session.add(row)
        await session.commit()
        result = await event_processor.process_payment_event_row(session, row.id, now=NOW)
    assert result.outcome == "ignored_no_payload"


async def test_record_event_failure_counts_attempts_and_gives_up_at_the_limit(db, monkeypatch):
    async with db() as session:
        tenant, _, _, _ = await _seed(session)
        row = await _row(session, tenant, _event())
        for attempt in range(1, event_processor.MAX_EVENT_ATTEMPTS + 1):
            await event_processor.record_event_failure(session, row.id, error_code="ProviderError")
            await session.commit()
            await session.refresh(row)
            expected = "received" if attempt < event_processor.MAX_EVENT_ATTEMPTS else "failed"
            assert (row.attempts, row.status) == (attempt, expected)
            assert row.error_code == "ProviderError"


async def test_counts_are_tenant_scoped(db, monkeypatch):
    _provider(monkeypatch, state=_paid_state(amount=1000))
    async with db() as session:
        tenant, _, _, _ = await _seed(session)
    await _process(db, tenant, _event(amount=1000))
    async with db() as session:
        total = await session.scalar(select(func.count()).select_from(PaymentAttentionItem))
        mine = await session.scalar(
            select(func.count())
            .select_from(PaymentAttentionItem)
            .where(PaymentAttentionItem.tenant_id == tenant.id)
        )
    assert total == mine == 1


async def test_overdue_on_a_paid_deposit_whose_consultation_was_cancelled_asks_the_clinic_once(
    db, monkeypatch
):
    _provider(monkeypatch)
    async with db() as session:
        tenant, _, _, deposit = await _seed(
            session,
            deposit_status=PixDepositStatus.PAID,
            appointment_status=AppointmentStatus.CANCELLED,
        )
    overdue = dict(status=PaymentStatus.EXPIRED, event_type="PAYMENT_OVERDUE")
    await _process(db, tenant, _event(event_id="evt_o1", **overdue))
    await _process(db, tenant, _event(event_id="evt_o2", **overdue))
    assert [i.reason for i in await _items(db)] == ["paid_then_cancelled"]
