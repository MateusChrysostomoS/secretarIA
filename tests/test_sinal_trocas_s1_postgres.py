"""TASK-085 R / S1 - concurrency proofs on Postgres (what SQLite cannot show).

* a paid SUSPENDED change applied by many webhook workers is written once and frees its hold once;
* the patient's tap ("Manter a antiga") racing the silence sweep at the deadline: exactly one wins;
* a cancellation (appointment row first, then `settle_open_adjustments`) racing the paid webhook
  of a suspended change never deadlocks and never applies the change onto a cancelled visit.

Opt-in like tests/test_service_change_postgres.py: `PAYMENTS_TEST_PG_URL` (a throwaway server).
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    STATUS_APPLIED,
    STATUS_CANCELLED,
    STATUS_KEPT_OLD,
    DepositAdjustment,
)
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from secretaria.services import booking_hold
from secretaria.services.payments import adjustment_queries, notify, service_change
from tests import test_service_change as base, test_service_change_postgres as pg
from tests.test_sinal_trocas_s1_unpaid import CLOCK, _webhook

pytestmark = pg.pytestmark
NOW = base.NOW
WORKERS = 6
provider = base.provider
no_default_hooks = base.no_default_hooks
maker = pg.maker


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    CLOCK["now"] = NOW
    monkeypatch.setattr(booking_hold, "_now", lambda: CLOCK["now"])

    async def deliver(*args, **kwargs):
        return None

    monkeypatch.setattr(notify, "deliver", deliver)

    async def replan(*args, **kwargs):
        return 0

    from secretaria.services import reminder_hooks

    monkeypatch.setattr(reminder_hooks, "after_appointment_rescheduled", replan)


async def _suspended(maker, provider):
    async with maker() as session:
        seeded = await pg._seed_pg(session)
        tenant = await session.get(Tenant, seeded[0].id)
        tenant.deposit_timing = "before"
        await session.commit()
    new_start = seeded[2].start_at + timedelta(days=1)
    async with maker() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        created = await service_change.request_paid_change(
            session, tenant=tenant, appointment=row, deposit=deposit, new_service="Avaliação",
            pending_edit={
                **base._edit(row, "Avaliação", 60),
                "start_at": new_start,
                "end_at": new_start + timedelta(minutes=60),
                "time_changed": True,
            },
            now=NOW,
        )
        await session.commit()
    assert created.kind == "suspended"
    return seeded, created


def _paid(provider, created):
    from secretaria.services.payments.provider import ChargeState, PaymentStatus

    provider.state = ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=3000,
        external_reference=str(created.adjustment_id), paid_at=NOW + timedelta(minutes=5),
    )


async def test_many_workers_apply_one_suspended_change_exactly_once(maker, provider):
    from secretaria.services.payments.provider import PaymentStatus

    seeded, created = await _suspended(maker, provider)
    _paid(provider, created)

    results = await asyncio.gather(
        *(
            _webhook(maker, seeded[0].id, PaymentStatus.PAID, event_id=f"evt_{n}",
                     at=NOW + timedelta(minutes=5))
            for n in range(WORKERS)
        )
    )

    outcomes = sorted(r.outcome for r in results)
    assert outcomes == ["adjustment_already_applied"] * (WORKERS - 1) + ["adjustment_applied"]
    async with maker() as session:
        row = await session.get(Appointment, seeded[2].id)
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        notices = await session.scalar(select(func.count()).select_from(ProfessionalEditNotice))
        holds = await session.scalar(select(func.count()).select_from(BookingHold))
    assert (row.appointment_type, adjustment.status) == ("Avaliação", STATUS_APPLIED)
    assert (notices, holds, deposit.reschedule_count) == (1, 0, 1)


async def test_a_tap_and_the_silence_sweep_at_the_deadline_decide_once(maker, provider):
    seeded, created = await _suspended(maker, provider)
    async with maker() as session:
        await service_change.expire_adjustments(session, now=NOW + timedelta(minutes=31))
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
        deadline = adjustment.expires_at
    # The tap a moment before the deadline, the sweep a moment after it, at the same time.
    tap_at = deadline - timedelta(seconds=1)
    sweep_at = deadline + timedelta(seconds=1)
    CLOCK["now"] = tap_at

    async def tap():
        async with maker() as session:
            outcome = await service_change.choose_after_expiry(
                session, tenant_id=seeded[0].id, adjustment_id=created.adjustment_id,
                choice="keep", now=tap_at,
            )
            await session.commit()
            return outcome.kind

    async def sweep():
        async with maker() as session:
            return await service_change.expire_adjustments(session, now=sweep_at)

    kinds = await asyncio.gather(tap(), sweep())

    async with maker() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
    assert adjustment.status == STATUS_KEPT_OLD
    # Exactly one of them decided it.
    assert (kinds[0] == "kept_old") != (kinds[1] == 1)


async def test_a_cancellation_racing_the_paid_webhook_of_a_suspended_change(maker, provider):
    from secretaria.services.payments.provider import PaymentStatus

    seeded, created = await _suspended(maker, provider)
    _paid(provider, created)
    appointment_id = seeded[2].id

    async with maker() as cancel:
        await cancel.execute(
            update(Appointment)
            .where(Appointment.id == appointment_id)
            .values(status=AppointmentStatus.CANCELLED)
        )
        webhook = asyncio.create_task(
            _webhook(maker, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=5))
        )
        await asyncio.sleep(1.0)  # the webhook verified the charge and queued on the row
        tenant = await cancel.get(Tenant, seeded[0].id)
        deposit = await cancel.get(PixDeposit, seeded[3].id)
        await adjustment_queries.settle_open_adjustments(
            cancel, tenant=tenant, deposit=deposit, now=NOW + timedelta(minutes=5)
        )
        await cancel.commit()
    result = await asyncio.wait_for(webhook, timeout=15)

    async with maker() as session:
        row = await session.get(Appointment, appointment_id)
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
        holds = await session.scalar(select(func.count()).select_from(BookingHold))
        items = list(await session.scalars(select(PaymentAttentionItem.reason)))
    assert (row.appointment_type, row.status) == ("Consulta", AppointmentStatus.CANCELLED)
    # Paid at the provider: the settle leaves it to the webhook, which finds the visit gone.
    assert result.outcome in ("adjustment_not_applicable", "adjustment_paid_late")
    assert adjustment.status == STATUS_CANCELLED
    assert holds == 0
    assert items == ["orphan_payment"]
