# ruff: noqa: F811
"""TASK-085 R / UB1, fix round 1 (reviews/UB1-review.md I1, I2, I3, m2-m4).

* I1 - two overlapping sweeps: the pending notice is re-read UNDER the per-consultation claim, so
  the second sweep never mails a patient whose notice the first one just committed; a lost row is
  never counted as sent.
* I2 - a service switched off / without a price is not a price change (AWAITING mirrors PAID's
  `blocked`): no notice; a real price whose new sinal is under the provider minimum still is.
* I3 - a capped or failed notice is never silently dropped: a follow-up sweep is enqueued (capped
  -> the clinic's next local day; failed -> ~30 min), bounded to 3 attempts per consultation
  version; then the clinic item `price_change_no_email` (reason reused). A sweep that runs out of
  time budget resumes in a follow-up job.
* m3 - the no-e-mail item is per consultation VERSION; m4 - a suspended consultation is left out by
  the visibility clause itself, not by the blocking-status list.
"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import Appointment, Tenant
from secretaria.models.appointment_reminder import AppointmentReminder
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    MODE_SUSPEND,
    STATUS_OLD_SLOT_LOST,
    STATUS_RESTORING,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import REASON_PRICE_CHANGE_NO_EMAIL
from secretaria.services.email import EmailOutcome
from secretaria.services.payments import price_change
from tests.payments_support import FakeArqPool
from tests.test_sinal_trocas_ub1_sweep import (  # noqa: F401 - fixtures
    CHANGED_AT,
    NOW,
    START,
    db,
    items,
    mail,
    needed,
    notices,
    redis,
    reprice,
    seed_tenant,
    seed_visit,
    sweep,
)


@pytest.fixture
def pool() -> FakeArqPool:
    """The worker's `ctx["redis"]` is an ArqRedis: Redis calls AND `enqueue_job`."""
    return FakeArqPool()


def _jobs(pool: FakeArqPool) -> list[tuple]:
    return [job for job in pool.jobs if job[0] == price_change.SWEEP_JOB]


async def _off(db, tenant_id, **entry) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.appointment_types = [
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}
            | entry
        ]
        await session.commit()


# ------------------------------------------------------------------ I1


@pytest.mark.parametrize("answered", [False, True])
async def test_a_sweep_that_lost_the_race_never_mails_again(db, mail, redis, monkeypatch, answered):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    if answered:  # the re-arm path: both sweeps would update the same row
        await sweep(tenant.id, redis)
        async with db() as session:
            [row] = list(await session.scalars(select(AppointmentReminder)))
            row.answered_at = NOW
            row.answer = "opened"
            await session.commit()
        mail["sent"].clear()

    b_read, a_done = asyncio.Event(), asyncio.Event()
    real = price_change._patient_email

    async def _email(notice):  # B read "no pending row", then waits on brain-api until A is done
        if not b_read.is_set():
            b_read.set()
            await a_done.wait()
        return await real(notice)

    monkeypatch.setattr(price_change, "_patient_email", _email)

    async def sweep_b():
        return await price_change._notify_one(tenant.id, appointment.id, now=NOW, redis=redis)

    async def sweep_a():
        await b_read.wait()
        outcome = await price_change._notify_one(tenant.id, appointment.id, now=NOW, redis=redis)
        a_done.set()
        return outcome

    outcome_b, outcome_a = await asyncio.gather(sweep_b(), sweep_a())

    assert (outcome_a, outcome_b) == ("sent", "pending")
    assert len(mail["sent"]) == 1
    [row] = await notices(db, appointment.id)
    assert mail["sent"][0][2]["link_line"].endswith(str(row.id))


async def test_a_row_lost_to_a_competing_insert_is_not_counted_as_sent(
    db, mail, redis, monkeypatch
):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    async def _send_and_lose(to, template, variables):
        mail["sent"].append((to, template, variables))
        async with db() as session:  # someone else's row of the same version lands meanwhile
            session.add(
                AppointmentReminder(
                    tenant_id=tenant.id,
                    appointment_id=appointment.id,
                    patient_id=appointment.patient_id,
                    kind="price_change",
                    appointment_start_at=START,
                    due_at=NOW,
                    status="sent",
                    channel="email",
                    with_prompt=False,
                    sent_at=NOW,
                )
            )
            await session.commit()
        return EmailOutcome.SENT

    monkeypatch.setattr(price_change, "send_transactional_email_result", _send_and_lose)
    outcome = await price_change._notify_one(tenant.id, appointment.id, now=NOW, redis=redis)
    assert outcome == "row_lost"


# ------------------------------------------------------------------ I2


@pytest.mark.parametrize(
    "entry",
    [{"is_active": False}, {"price": ""}, {"price": "a combinar"}, {"name": "Outro serviço"}],
    ids=["switched_off", "no_price", "unreadable_price", "service_gone"],
)
async def test_a_price_that_cannot_be_read_is_not_a_price_change(db, mail, redis, entry):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, awaiting, _d = await seed_visit(session, tenant, awaiting_cents=6000)
        _p, paid, _d = await seed_visit(session, tenant, email="b@example.test")
        await session.commit()
    await _off(db, tenant.id, **entry)

    assert await needed(db, awaiting.id) is False
    assert await needed(db, paid.id) is False
    assert await sweep(tenant.id, redis) == 0
    assert mail["sent"] == [] and await notices(db) == [] and await items(db) == []


async def test_a_real_price_whose_new_sinal_is_under_the_minimum_is_still_told(db):
    # mapa-u §2.4: sinal novo < R$ 5 (não pago) -> só anula e avisa.
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, awaiting, _d = await seed_visit(session, tenant, awaiting_cents=6000)
        await session.commit()
    await reprice(db, tenant.id, "R$ 10,00")  # 30% = R$ 3, under the Asaas minimum -> 0
    assert await needed(db, awaiting.id) is True


# ------------------------------------------------------------------ I3


async def _weekly_visits(db, count: int):
    async with db() as session:
        tenant = await seed_tenant(session)
        patient, first, _d = await seed_visit(session, tenant)
        visits = [first]
        for n in range(1, count):
            _p, visit, _d = await seed_visit(
                session, tenant, patient=patient, start=START + timedelta(days=7 * n)
            )
            visits.append(visit)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    return tenant, visits


async def test_a_capped_notice_is_swept_again_on_the_clinics_next_day(db, mail, pool):
    tenant, visits = await _weekly_visits(db, 4)

    assert await sweep(tenant.id, pool) == 3

    [(name, args, kwargs)] = _jobs(pool)
    assert args == (str(tenant.id), CHANGED_AT.isoformat())
    # NOW = 18/11 09:00 in São Paulo -> 19/11 00:05 local = 03:05 UTC.
    assert kwargs["_defer_until"] == datetime(2026, 11, 19, 3, 5, tzinfo=UTC)
    assert kwargs["_job_id"] == f"pricechg:{tenant.id}:nextday:20261119"
    assert kwargs["_job_id"] != price_change.sweep_job_id(tenant.id, CHANGED_AT)

    next_day = datetime(2026, 11, 19, 3, 5, tzinfo=UTC)
    assert await sweep(tenant.id, pool, now=next_day) == 1
    assert {row.appointment_id for row in await notices(db)} == {v.id for v in visits}
    assert len(_jobs(pool)) == 1  # nothing left: no further follow-up
    assert not [key for key in pool.store if key.startswith("price_change_tries:")]


async def test_a_capped_notice_counts_one_attempt_per_day(db, mail, pool):
    tenant, visits = await _weekly_visits(db, 4)
    await sweep(tenant.id, pool)
    await sweep(tenant.id, pool, now=NOW + timedelta(minutes=30))  # same local day
    [tries] = [value for key, value in pool.store.items() if key.startswith("price_change_tries:")]
    assert tries == 1


async def test_a_failed_email_is_retried_in_30_minutes_then_handed_to_the_clinic(db, mail, pool):
    mail["outcome"] = EmailOutcome.SEND_FAILED
    async with db() as session:
        tenant = await seed_tenant(session)
        patient, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    moments = [NOW, NOW + timedelta(minutes=30), NOW + timedelta(minutes=60)]
    for moment in moments:
        assert await sweep(tenant.id, pool, now=moment) == 0

    retries = _jobs(pool)
    assert [kwargs["_defer_until"] for _n, _a, kwargs in retries] == [
        moments[0] + timedelta(minutes=30),
        moments[1] + timedelta(minutes=30),
    ]  # the 3rd failure is the last attempt: no 4th job
    assert len({kwargs["_job_id"] for _n, _a, kwargs in retries}) == 2
    assert await notices(db) == []
    [item] = await items(db)
    assert item.reason == REASON_PRICE_CHANGE_NO_EMAIL and item.patient_id == patient.id
    assert f"price_change_mail:{appointment.id}" in item.dedupe_key
    assert len(mail["sent"]) == 3


async def test_a_consultation_error_is_retried_and_bounded(db, mail, pool, monkeypatch):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, _d = await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    async def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(price_change, "price_notice_needed", _boom)
    for minutes in (0, 30, 60):
        await sweep(tenant.id, pool, now=NOW + timedelta(minutes=minutes))

    assert len(_jobs(pool)) == 2
    [item] = await items(db)
    assert f"price_change_mail:{appointment.id}" in item.dedupe_key


async def test_without_an_arq_pool_a_failure_is_only_logged(db, mail, redis):
    mail["outcome"] = EmailOutcome.SEND_FAILED
    async with db() as session:
        tenant = await seed_tenant(session)
        await seed_visit(session, tenant)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    assert await sweep(tenant.id, redis) == 0  # plain Redis: no enqueue_job, never raises


async def test_a_sweep_out_of_time_resumes_in_a_follow_up_job(db, mail, pool, monkeypatch):
    monkeypatch.setattr(price_change, "_SWEEP_BUDGET", timedelta(0))
    async with db() as session:
        tenant = await seed_tenant(session)
        for n in range(3):
            await seed_visit(
                session, tenant, email=f"p{n}@example.test", start=START + timedelta(hours=n)
            )
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, pool) == 1  # at least one consultation per run

    [(_name, args, kwargs)] = _jobs(pool)
    assert args == (str(tenant.id), CHANGED_AT.isoformat())
    assert kwargs["_job_id"].startswith(f"pricechg:{tenant.id}:resume:")
    assert kwargs["_defer_until"] == NOW + price_change.SWEEP_DEFER


# ------------------------------------------------------------------ m3 / m4


async def test_the_no_email_item_is_per_consultation_version(db, mail, redis):
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, _d = await seed_visit(session, tenant, portal=False, email=None)
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")
    await sweep(tenant.id, redis)
    async with db() as session:  # rescheduled: a new version of the consultation
        stored = await session.get(Appointment, appointment.id)
        stored.start_at = START + timedelta(days=2)
        stored.end_at = START + timedelta(days=2, minutes=30)
        await session.commit()
    await sweep(tenant.id, redis)
    await sweep(tenant.id, redis)  # replayed: still one per version

    assert len(await items(db)) == 2


@pytest.mark.parametrize("status", [STATUS_RESTORING, STATUS_OLD_SLOT_LOST])
async def test_a_suspended_consultation_is_left_out_by_the_visibility_clause(
    db, mail, redis, status
):
    assert status not in price_change._BLOCKING_ADJUSTMENT_STATUSES
    async with db() as session:
        tenant = await seed_tenant(session)
        _p, appointment, deposit = await seed_visit(session, tenant)
        session.add(
            DepositAdjustment(
                id=uuid4(),
                tenant_id=tenant.id,
                deposit_id=deposit.id,
                appointment_id=appointment.id,
                kind=KIND_INCREASE,
                status=status,
                mode=MODE_SUSPEND,
                old_service="Consulta",
                new_service="Consulta",
                diff_cents=1200,
            )
        )
        await session.commit()
    await reprice(db, tenant.id, "R$ 240,00")

    assert await sweep(tenant.id, redis) == 0
    assert mail["sent"] == [] and await items(db) == []
