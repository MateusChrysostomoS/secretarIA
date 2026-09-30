"""The recurring payment jobs (services/payments/jobs.py, workers/payments_cron.py):
expiry, reconciliation, the two sweeps and the daily maintenance (spec section 9)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import PaymentWebhookEvent, PixDeposit, Tenant
from secretaria.models.pix_deposit import (
    FULFILLMENT_NONE,
    FULFILLMENT_PROMOTING,
    PixDepositStatus,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, alerts, deposit_lifecycle, jobs, registry
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.workers import arq_worker, payments_cron
from tests.payments_support import (
    FakeProvider,
    FakeRedis,
    make_sessionmaker,
    recording_enqueue,
)

NOW = datetime(2026, 9, 29, 15, 0, tzinfo=UTC)
PAID = ChargeState(status=PaymentStatus.PAID, paid_amount_cents=6000, external_reference="ref")
OPEN = ChargeState(status=PaymentStatus.PENDING, external_reference="ref")


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _clean_hooks(monkeypatch):
    monkeypatch.setattr(jobs, "EXPIRY_HOOKS", [])
    monkeypatch.setattr(jobs, "DAILY_STEPS", [])


def _provider(monkeypatch, **kw) -> FakeProvider:
    fake = FakeProvider(**kw)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _tenant(session, **kw) -> Tenant:
    tenant = Tenant(
        id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12],
        contact_email="c@example.test", **kw,
    )
    session.add(tenant)
    await session.flush()
    row = await accounts.get_or_create_account_row(session, tenant_id=tenant.id, provider="asaas")
    cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
    row.status = "connected"
    return tenant


async def _deposit(
    session, tenant, *, charge="pay_1", timing="before", status=PixDepositStatus.AWAITING,
    expires_in_minutes: float | None = -5, created_minutes_ago: float = 30,
    fulfillment="none", paid_minutes_ago: float | None = None,
) -> PixDeposit:
    deposit = PixDeposit(
        tenant_id=tenant.id,
        amount_cents=6000,
        percent_applied=30,
        status=status,
        provider="asaas",
        provider_charge_id=charge,
        external_reference="ref",
        deposit_timing=timing,
        expires_at=(
            None if expires_in_minutes is None else NOW + timedelta(minutes=expires_in_minutes)
        ),
        created_at=NOW - timedelta(minutes=created_minutes_ago),
        fulfillment_status=fulfillment,
        paid_at=None if paid_minutes_ago is None else NOW - timedelta(minutes=paid_minutes_ago),
    )
    session.add(deposit)
    await session.flush()
    return deposit


async def _events(db) -> list[PaymentWebhookEvent]:
    async with db() as session:
        return list((await session.scalars(select(PaymentWebhookEvent))).all())


# --------------------------------------------------------------------------
# expiry
# --------------------------------------------------------------------------


async def test_a_due_unpaid_before_charge_is_voided_then_expired_and_hooks_run(db, monkeypatch):
    fake = _provider(monkeypatch, state=OPEN)
    seen = []

    async def hook(session, deposit):
        seen.append(deposit.id)

    jobs.register_expiry_hook(hook)
    async with db() as session:
        tenant = await _tenant(session)
        deposit = await _deposit(session, tenant)
        deposit_id = deposit.id
        await session.commit()
        sent, enqueue = recording_enqueue()
        report = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
        again = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
        after = await session.get(PixDeposit, deposit_id)

    assert report.expired == 1 and again.expired == 0
    assert (after.status, after.resolved_at is not None) == (PixDepositStatus.EXPIRED, True)
    assert [c[0] for c in fake.calls] == ["get_charge", "cancel_charge"]  # ask BEFORE voiding
    assert seen == [deposit_id]  # F2 releases the hold and tells the patient from this hook
    assert sent == []


async def test_expiry_rotates_past_charges_whose_provider_keeps_failing(db, monkeypatch):
    fake = _provider(monkeypatch, state=OPEN)
    original_get = fake.get_charge

    async def get_charge(account, charge_id):
        if charge_id in {"stuck-1", "stuck-2"}:
            raise ProviderError("unavailable", status_code=503, retryable=True)
        return await original_get(account, charge_id)

    monkeypatch.setattr(fake, "get_charge", get_charge)
    async with db() as session:
        tenant = await _tenant(session)
        await _deposit(session, tenant, charge="stuck-1", expires_in_minutes=-30)
        await _deposit(session, tenant, charge="stuck-2", expires_in_minutes=-20)
        last = await _deposit(session, tenant, charge="last", expires_in_minutes=-10)
        await session.commit()
        _, enqueue = recording_enqueue()
        await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue, limit=2)
        await jobs.expire_due_charges(
            session, now=NOW + timedelta(minutes=1), enqueue=enqueue, limit=2
        )
        await session.refresh(last)

    assert last.status == PixDepositStatus.EXPIRED


async def test_reconciliation_rotates_past_older_pending_charges(db, monkeypatch):
    fake = _provider(monkeypatch, state=OPEN)

    async def get_charge(account, charge_id):
        fake.calls.append(("get_charge", charge_id))
        return PAID if charge_id == "paid-later" else OPEN

    monkeypatch.setattr(fake, "get_charge", get_charge)
    async with db() as session:
        tenant = await _tenant(session)
        await _deposit(session, tenant, charge="old-1", timing="after", created_minutes_ago=40)
        await _deposit(session, tenant, charge="old-2", timing="after", created_minutes_ago=30)
        await _deposit(session, tenant, charge="paid-later", timing="after", created_minutes_ago=20)
        await session.commit()
        _, enqueue = recording_enqueue()
        await jobs.reconcile_open_charges(session, now=NOW, enqueue=enqueue, limit_per_provider=2)
        await jobs.reconcile_open_charges(
            session, now=NOW + timedelta(minutes=1), enqueue=enqueue, limit_per_provider=2
        )

    assert "paid-later" in [call[1] for call in fake.calls if call[0] == "get_charge"]


async def test_paid_after_notice_is_recovered_after_event_commit(db, monkeypatch):
    sent = []

    async def notify(session, tenant, deposit):
        sent.append(deposit.id)

    monkeypatch.setattr(deposit_lifecycle, "_notify_paid", notify)
    async with db() as session:
        tenant = await _tenant(session)
        deposit = await _deposit(
            session, tenant, charge="paid-no-notice", timing="after", status=PixDepositStatus.PAID,
            paid_minutes_ago=5,
        )
        deposit_id = deposit.id
        await session.commit()
        first = await jobs.recover_paid_notices(session, now=NOW)
        second = await jobs.recover_paid_notices(session, now=NOW)
        await session.refresh(deposit)

    assert (first, second) == (1, 0)
    assert sent == [deposit_id]
    assert deposit.paid_notice_sent_at is not None


async def test_only_due_before_charges_are_considered(db, monkeypatch):
    fake = _provider(monkeypatch, state=OPEN)
    async with db() as session:
        tenant = await _tenant(session)
        await _deposit(session, tenant, charge="not_due", expires_in_minutes=+10)
        await _deposit(session, tenant, charge="after_mode", timing="after", expires_in_minutes=-5)
        await _deposit(session, tenant, charge="no_expiry", expires_in_minutes=None)
        await _deposit(session, tenant, charge="paid", status=PixDepositStatus.PAID)
        await session.commit()
        _, enqueue = recording_enqueue()
        report = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
    assert report.expired == 0 and fake.calls == []


async def test_a_provider_outage_moves_no_state_and_the_next_run_recovers(db, monkeypatch):
    fake = _provider(
        monkeypatch,
        get_error=ProviderError("get_charge_failed", status_code=503, retryable=True),
    )
    seen = []

    async def hook(session, deposit):
        seen.append(deposit.id)

    jobs.register_expiry_hook(hook)
    async with db() as session:
        tenant = await _tenant(session)
        deposit = await _deposit(session, tenant)
        deposit_id = deposit.id
        await session.commit()
        _, enqueue = recording_enqueue()
        down = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
        assert (await session.get(PixDeposit, deposit_id)).status == PixDepositStatus.AWAITING
        assert down.expired == 0 and down.skipped_provider_down == 1
        assert "cancel_charge" not in [c[0] for c in fake.calls] and seen == []

        fake.get_error, fake.state = None, OPEN
        up = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
        assert up.expired == 1
        assert (await session.get(PixDeposit, deposit_id)).status == PixDepositStatus.EXPIRED


async def test_a_charge_that_turns_out_paid_is_never_expired_and_is_reconciled_once(
    db, monkeypatch
):
    _provider(monkeypatch, state=PAID)
    async with db() as session:
        tenant = await _tenant(session)
        deposit = await _deposit(session, tenant)
        deposit_id = deposit.id
        await session.commit()
        sent, enqueue = recording_enqueue()
        first = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
        second = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
        assert (await session.get(PixDeposit, deposit_id)).status == PixDepositStatus.AWAITING

    assert (first.expired, first.paid_found, second.paid_found) == (0, 1, 1)
    (event,) = await _events(db)
    assert event.event_id == "reconcile:pay_1:PAID" and event.payload["synthetic"] is True
    assert sent == [("process_payment_event", str(event.id))]  # enqueued once, not twice


async def test_a_failed_void_leaves_the_charge_awaiting_for_the_next_run(db, monkeypatch):
    _provider(
        monkeypatch,
        state=OPEN,
        cancel_error=ProviderError("cancel_failed", status_code=500, retryable=True),
    )
    async with db() as session:
        tenant = await _tenant(session)
        deposit = await _deposit(session, tenant)
        deposit_id = deposit.id
        await session.commit()
        _, enqueue = recording_enqueue()
        report = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
        after = await session.get(PixDeposit, deposit_id)
    assert report.expired == 0 and report.failed_cancel == 1
    assert after.status == PixDepositStatus.AWAITING


async def test_each_charge_uses_its_own_clinics_account_and_one_failure_does_not_stop_the_rest(
    db, monkeypatch
):
    fake = _provider(monkeypatch, state=OPEN)
    calls = {"n": 0}

    async def flaky_hook(session, deposit):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("hook exploded")

    jobs.register_expiry_hook(flaky_hook)
    async with db() as session:
        a, b = await _tenant(session), await _tenant(session)
        await _deposit(session, a, charge="pay_a")
        await _deposit(session, b, charge="pay_b")
        tenant_ids = {a.id, b.id}
        await session.commit()
        _, enqueue = recording_enqueue()
        report = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue)
    assert report.expired == 2
    assert set(fake.tenants_seen) == tenant_ids


async def test_expiry_processes_at_most_the_batch_limit_per_tick(db, monkeypatch):
    fake = _provider(monkeypatch, state=OPEN)
    async with db() as session:
        tenant = await _tenant(session)
        for n in range(5):
            await _deposit(session, tenant, charge=f"pay_{n}")
        await session.commit()
        _, enqueue = recording_enqueue()
        report = await jobs.expire_due_charges(session, now=NOW, enqueue=enqueue, limit=3)
    assert report.expired == 3 and fake.called("get_charge") == 3


# --------------------------------------------------------------------------
# reconciliation
# --------------------------------------------------------------------------


async def test_reconciliation_finds_a_paid_charge_whose_webhook_never_came_and_alerts(
    db, monkeypatch
):
    _provider(monkeypatch, state=PAID)
    mails = []

    async def fake_send(to_email, clinic_name, kind):
        mails.append(kind)

    monkeypatch.setattr(alerts.email, "send_payment_alert", fake_send)
    async with db() as session:
        tenant = await _tenant(session)
        await _deposit(session, tenant, timing="after", expires_in_minutes=None)
        await session.commit()
        sent, enqueue = recording_enqueue()
        report = await jobs.reconcile_open_charges(
            session, now=NOW, enqueue=enqueue, redis=FakeRedis()
        )
    assert (report.checked, report.paid_found, report.webhooks_missing) == (1, 1, 1)
    assert mails == []  # operational issue is shown in the clinic UI
    assert len(sent) == 1 and sent[0][0] == "process_payment_event"


async def test_no_alert_when_the_provider_did_deliver_a_webhook_for_that_charge(db, monkeypatch):
    _provider(monkeypatch, state=PAID)
    mails = []

    async def fake_send(to_email, clinic_name, kind):
        mails.append(kind)

    monkeypatch.setattr(alerts.email, "send_payment_alert", fake_send)
    async with db() as session:
        tenant = await _tenant(session)
        await _deposit(session, tenant, timing="after", expires_in_minutes=None)
        session.add(
            PaymentWebhookEvent(
                tenant_id=tenant.id, provider="asaas", event_id="evt_real",
                provider_charge_id="pay_1", status="failed",
            )
        )
        await session.commit()
        _, enqueue = recording_enqueue()
        report = await jobs.reconcile_open_charges(
            session, now=NOW, enqueue=enqueue, redis=FakeRedis()
        )
    assert report.paid_found == 1 and report.webhooks_missing == 0 and mails == []


async def test_reconciliation_skips_young_unpaid_and_already_resolved_charges(db, monkeypatch):
    fake = _provider(monkeypatch, state=OPEN)
    async with db() as session:
        tenant = await _tenant(session)
        await _deposit(session, tenant, charge="young", created_minutes_ago=3)
        await _deposit(session, tenant, charge="old_open", created_minutes_ago=30)
        await _deposit(session, tenant, charge="done", status=PixDepositStatus.PAID)
        await session.commit()
        sent, enqueue = recording_enqueue()
        report = await jobs.reconcile_open_charges(session, now=NOW, enqueue=enqueue)
    assert report.checked == 1 and report.paid_found == 0 and sent == []
    assert [c for c in fake.calls if c[0] == "get_charge"] == [("get_charge", "old_open")]


async def test_reconciliation_survives_a_provider_error_on_one_charge(db, monkeypatch):
    fake = _provider(monkeypatch)
    fake.state = {"pay_bad": None, "pay_ok": PAID}

    async def get_charge(account, charge_id):
        if charge_id == "pay_bad":
            raise ProviderError("get_charge_failed", status_code=503, retryable=True)
        return PAID

    fake.get_charge = get_charge
    async with db() as session:
        tenant = await _tenant(session)
        await _deposit(session, tenant, charge="pay_bad", timing="after", expires_in_minutes=None)
        await _deposit(session, tenant, charge="pay_ok", timing="after", expires_in_minutes=None)
        await session.commit()
        sent, enqueue = recording_enqueue()
        report = await jobs.reconcile_open_charges(session, now=NOW, enqueue=enqueue)
    assert report.paid_found == 1 and len(sent) == 1


async def test_reconciliation_respects_the_per_provider_limit(db, monkeypatch):
    fake = _provider(monkeypatch, state=OPEN)
    async with db() as session:
        tenant = await _tenant(session)
        for n in range(4):
            await _deposit(session, tenant, charge=f"pay_{n}", timing="after")
        await session.commit()
        _, enqueue = recording_enqueue()
        report = await jobs.reconcile_open_charges(
            session, now=NOW, enqueue=enqueue, limit_per_provider=2
        )
    assert report.checked == 2 and fake.called("get_charge") == 2


# --------------------------------------------------------------------------
# sweeps
# --------------------------------------------------------------------------


async def test_the_event_sweep_reenqueues_only_old_received_events(db):
    async with db() as session:
        tenant = await _tenant(session)
        rows = {}
        for label, status, age in (
            ("stuck", "received", 5), ("young", "received", 0.2),
            ("done", "processed", 30), ("dead", "failed", 30),
        ):
            row = PaymentWebhookEvent(
                tenant_id=tenant.id, provider="asaas", event_id=label, status=status,
                received_at=NOW - timedelta(minutes=age),
            )
            session.add(row)
            rows[label] = row
        await session.commit()
        sent, enqueue = recording_enqueue()
        count = await jobs.requeue_stuck_events(session, now=NOW, enqueue=enqueue)
    assert count == 1 and sent == [("process_payment_event", str(rows["stuck"].id))]


async def test_the_promotion_sweep_reenqueues_paid_before_charges_that_never_fulfilled(db):
    async with db() as session:
        tenant = await _tenant(session)
        stuck = await _deposit(
            session, tenant, charge="a", status=PixDepositStatus.PAID,
            fulfillment=FULFILLMENT_PROMOTING, paid_minutes_ago=5,
        )
        await _deposit(  # too young
            session, tenant, charge="b", status=PixDepositStatus.PAID,
            fulfillment=FULFILLMENT_NONE, paid_minutes_ago=0.2,
        )
        await _deposit(  # `after` timing is fulfilled by the notice, never swept
            session, tenant, charge="c", timing="after", status=PixDepositStatus.PAID,
            fulfillment=FULFILLMENT_NONE, paid_minutes_ago=5,
        )
        await _deposit(  # already fulfilled
            session, tenant, charge="d", status=PixDepositStatus.PAID,
            fulfillment="confirmed", paid_minutes_ago=5,
        )
        await session.commit()
        sent, enqueue = recording_enqueue()
        count = await jobs.requeue_stuck_fulfillments(session, now=NOW, enqueue=enqueue)
    assert count == 1 and sent == [("fulfill_paid_charge_job", str(stuck.id))]


# --------------------------------------------------------------------------
# daily maintenance
# --------------------------------------------------------------------------


async def test_daily_prunes_old_payloads_keeps_the_rows_and_runs_every_step(db):
    ran = []

    async def good(session, now, redis):
        ran.append("good")

    async def bad(session, now, redis):
        raise RuntimeError("step exploded")

    jobs.register_daily_step(bad)
    jobs.register_daily_step(good)
    async with db() as session:
        tenant = await _tenant(session)
        for label, age_days in (("old", 40), ("recent", 10)):
            session.add(
                PaymentWebhookEvent(
                    tenant_id=tenant.id, provider="asaas", event_id=label,
                    received_at=NOW - timedelta(days=age_days), payload={"raw": {"x": 1}},
                )
            )
        await session.commit()
        report = await jobs.daily_maintenance(session, now=NOW, redis=FakeRedis())
        rows = {r.event_id: r for r in (await session.scalars(select(PaymentWebhookEvent))).all()}

    assert report.payloads_pruned == 1
    assert rows["old"].payload is None and rows["recent"].payload == {"raw": {"x": 1}}
    assert ran == ["good"]  # a failing step never blocks the next one


# --------------------------------------------------------------------------
# cron wiring
# --------------------------------------------------------------------------


def test_cron_minutes_never_collide_with_each_other_or_the_existing_crons():
    taken = {0, 15, 30, 45, 10, 7} | set(range(0, 60, 5))  # handover, reminders, nudges, parity
    sets = {
        "expiry": payments_cron.EXPIRY_MINUTES,
        "reconcile": payments_cron.RECONCILE_MINUTES,
        "sweeps": payments_cron.SWEEP_MINUTES,
    }
    for name, minutes in sets.items():
        assert minutes and not (minutes & taken), name
    assert not (sets["expiry"] & sets["reconcile"])
    assert not (sets["expiry"] & sets["sweeps"])
    assert not (sets["reconcile"] & sets["sweeps"])
    assert (payments_cron.DAILY_HOUR, payments_cron.DAILY_MINUTE) != (3, 30)  # metering


def test_the_four_jobs_are_registered_on_the_worker():
    names = arq_worker.registered_cron_names()
    for name in (
        "cron:run_payment_expiry",
        "cron:run_payment_reconciliation",
        "cron:run_payment_sweeps",
        "cron:run_payment_daily",
    ):
        assert name in names


async def test_the_expiry_cron_wrapper_runs_the_job_with_its_own_session(db, monkeypatch):
    monkeypatch.setattr(payments_cron, "async_session_factory", db)
    fake = _provider(monkeypatch, state=OPEN)
    async with db() as session:
        tenant = await _tenant(session)
        await _deposit(session, tenant, expires_in_minutes=-1_000_000)  # long overdue
        await session.commit()
    await payments_cron.run_payment_expiry({"redis": FakeRedis()})
    assert fake.called("cancel_charge") == 1
