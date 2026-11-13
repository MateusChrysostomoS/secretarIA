# ruff: noqa: F811
"""workers/agenda_cron.py (TASK-083): catch-up linking of professionals brain-api bound late."""

from uuid import uuid4

from secretaria.config import get_settings
from secretaria.models import Agenda, Professional
from secretaria.services import agenda_linking
from secretaria.workers import agenda_cron
from tests._local_agenda import seed_professional, seed_tenant
from tests._reminder_fixtures import db  # noqa: F401

ACCOUNT = str(uuid4())


def _wire(monkeypatch, db, accounts_by_tenant: dict, *, raises_for=(), limit: int | None = None):
    """The cron's own session factory -> test DB; brain-api faked per tenant."""
    monkeypatch.setattr(agenda_cron, "async_session_factory", db)
    asked: list = []

    async def fake(tenant_id):
        asked.append(tenant_id)
        if tenant_id in raises_for:
            raise RuntimeError("brain-api exploded")
        return accounts_by_tenant.get(tenant_id)  # missing key -> None = brain-api failure

    monkeypatch.setattr(agenda_linking, "fetch_professional_accounts", fake)
    if limit is not None:
        monkeypatch.setattr(get_settings(), "AGENDA_RECONCILE_MAX_TENANTS_PER_RUN", limit)
    return asked


async def _agenda_id(db, professional):
    async with db() as session:
        return (await session.get(Professional, professional.id)).agenda_id


async def test_cron_picks_only_tenants_with_active_unlinked_professionals(db, monkeypatch):
    needs = await seed_tenant(db, name="Needs")
    linked = await seed_tenant(db, name="Linked")
    inactive_only = await seed_tenant(db, name="InactiveOnly")
    empty = await seed_tenant(db, name="Empty")
    pro_needs = await seed_professional(db, needs, "Dr. N")
    pro_linked = await seed_professional(db, linked, "Dr. L")
    pro_inactive = await seed_professional(db, inactive_only, "Dr. I")
    async with db() as session:
        agenda = Agenda(person_key=str(uuid4()))
        session.add(agenda)
        await session.flush()
        (await session.get(Professional, pro_linked.id)).agenda_id = agenda.id
        (await session.get(Professional, pro_inactive.id)).is_active = False
        await session.commit()
    asked = _wire(monkeypatch, db, {needs.id: {str(pro_needs.id): ACCOUNT}})

    counts = await agenda_cron.run_agenda_reconcile({})

    assert asked == [needs.id]  # not Linked, not InactiveOnly, not Empty
    assert empty.id not in asked
    assert counts == {"tenants": 1, "changed": 1, "brain_unavailable": 0, "failed": 0}
    assert await _agenda_id(db, pro_needs) is not None


async def test_one_tenant_failing_does_not_stop_the_others(db, monkeypatch):
    bad = await seed_tenant(db, name="Bad")
    down = await seed_tenant(db, name="Down")
    good = await seed_tenant(db, name="Good")
    pros = {t.id: await seed_professional(db, t, "Dr.") for t in (bad, down, good)}
    asked = _wire(
        monkeypatch,
        db,
        {good.id: {str(pros[good.id].id): ACCOUNT}},  # `down` is absent -> brain-api unavailable
        raises_for={bad.id},
    )

    counts = await agenda_cron.run_agenda_reconcile({})

    assert set(asked) == {bad.id, down.id, good.id}
    assert counts == {"tenants": 3, "changed": 1, "brain_unavailable": 1, "failed": 1}
    assert await _agenda_id(db, pros[good.id]) is not None
    assert await _agenda_id(db, pros[bad.id]) is None
    assert await _agenda_id(db, pros[down.id]) is None


async def test_the_per_run_bound_is_respected_and_later_ticks_finish_the_job(db, monkeypatch):
    tenants = [await seed_tenant(db, name=f"T{i}") for i in range(5)]
    pros = {t.id: await seed_professional(db, t, "Dr.") for t in tenants}
    accounts = {t.id: {str(pros[t.id].id): str(uuid4())} for t in tenants}
    asked = _wire(monkeypatch, db, accounts, limit=2)

    first = await agenda_cron.run_agenda_reconcile({})
    assert first["tenants"] == 2 and len(asked) == 2

    for _ in range(4):  # already-linked tenants are never picked again, so this converges
        await agenda_cron.run_agenda_reconcile({})
    assert len(asked) == 5 and len(set(asked)) == 5
    for tenant in tenants:
        assert await _agenda_id(db, pros[tenant.id]) is not None
    assert (await agenda_cron.run_agenda_reconcile({}))["tenants"] == 0  # nothing left


async def test_a_professional_brain_api_does_not_return_stays_null_and_is_retried(db, monkeypatch):
    tenant = await seed_tenant(db)
    pro = await seed_professional(db, tenant, "Dr. Late")
    answers = {tenant.id: {}}  # brain-api answers, but has not bound the user yet
    asked = _wire(monkeypatch, db, answers)

    first = await agenda_cron.run_agenda_reconcile({})
    assert first["changed"] == 0 and await _agenda_id(db, pro) is None

    answers[tenant.id] = {str(pro.id): ACCOUNT}  # the invite flow binds the account later
    second = await agenda_cron.run_agenda_reconcile({})
    assert asked == [tenant.id, tenant.id]
    assert second["changed"] == 1 and await _agenda_id(db, pro) is not None

    assert (await agenda_cron.run_agenda_reconcile({}))["tenants"] == 0  # idempotent: not picked


async def test_a_zero_bound_does_nothing(db, monkeypatch):
    tenant = await seed_tenant(db)
    await seed_professional(db, tenant, "Dr. X")
    asked = _wire(monkeypatch, db, {}, limit=0)
    assert (await agenda_cron.run_agenda_reconcile({}))["tenants"] == 0
    assert asked == []


def test_schedule_comes_from_settings_and_is_a_valid_arq_minute_set(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "AGENDA_RECONCILE_INTERVAL_MINUTES", 15)
    assert agenda_cron.agenda_reconcile_minutes(settings) == {9, 24, 39, 54}
    monkeypatch.setattr(settings, "AGENDA_RECONCILE_INTERVAL_MINUTES", 60)
    assert agenda_cron.agenda_reconcile_minutes(settings) == {9}
    monkeypatch.setattr(settings, "AGENDA_RECONCILE_INTERVAL_MINUTES", 0)  # clamped, never empty
    assert agenda_cron.agenda_reconcile_minutes(settings) == set(range(60))


def test_the_cron_is_registered_on_the_worker():
    from secretaria.workers.arq_worker import WorkerSettings

    jobs = {getattr(j.coroutine, "__name__", ""): j for j in WorkerSettings.cron_jobs}
    assert jobs["run_agenda_reconcile"].minute == {9, 24, 39, 54}
