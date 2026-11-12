# ruff: noqa: F811
"""services/agenda_linking.py (TASK-083 spec §6): one `agendas` row per brain-api account.

Identity comes ONLY from brain-api's `professional-accounts` answer. A brain-api failure never
raises and never links anything: the professional keeps behaving exactly as before.
"""

from uuid import uuid4

import httpx
from sqlalchemy import select

from secretaria.models import Agenda, Professional
from secretaria.services import agenda_linking, brain_professionals
from secretaria.services.agenda_linking import (
    ensure_agenda_for_professional,
    reconcile_all_tenants,
    sync_tenant_agendas,
    try_link_professional,
)
from tests._local_agenda import seed_professional, seed_tenant
from tests._reminder_fixtures import db  # noqa: F401

ACCOUNT_A = str(uuid4())
ACCOUNT_B = str(uuid4())


def _fake_accounts(monkeypatch, by_tenant: dict | None):
    """brain-api's answer per tenant id (`None` = the call failed)."""
    calls: list = []

    async def fake(tenant_id):
        calls.append(tenant_id)
        if by_tenant is None:
            return None
        return by_tenant.get(tenant_id, {})

    monkeypatch.setattr(agenda_linking, "fetch_professional_accounts", fake)
    return calls


async def _agenda_ids(db, *professionals) -> list:
    async with db() as session:
        return [(await session.get(Professional, p.id)).agenda_id for p in professionals]


async def test_same_account_in_two_tenants_converges_on_one_agenda(db, monkeypatch):
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    _fake_accounts(
        monkeypatch,
        {tenant_p.id: {str(x_p.id): ACCOUNT_A}, tenant_c.id: {str(x_c.id): ACCOUNT_A}},
    )
    async with db() as session:
        await sync_tenant_agendas(session, tenant_p.id)
        await session.commit()
    async with db() as session:
        result = await sync_tenant_agendas(session, tenant_c.id)
        await session.commit()

    id_p, id_c = await _agenda_ids(db, x_p, x_c)
    assert id_p is not None and id_p == id_c
    assert (result.linked, result.shared) == (1, 1)
    async with db() as session:
        rows = (await session.scalars(select(Agenda))).all()
    assert [a.person_key for a in rows] == [ACCOUNT_A]


async def test_different_accounts_do_not_link(db, monkeypatch):
    tenant_p = await seed_tenant(db)
    tenant_c = await seed_tenant(db)
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")  # same NAME, different person
    _fake_accounts(
        monkeypatch,
        {tenant_p.id: {str(x_p.id): ACCOUNT_A}, tenant_c.id: {str(x_c.id): ACCOUNT_B}},
    )
    async with db() as session:
        await sync_tenant_agendas(session, tenant_p.id)
        result_c = await sync_tenant_agendas(session, tenant_c.id)
        await session.commit()
    id_p, id_c = await _agenda_ids(db, x_p, x_c)
    assert id_p != id_c and None not in (id_p, id_c)
    assert (result_c.linked, result_c.shared) == (1, 0)


async def test_sync_is_idempotent(db, monkeypatch):
    tenant = await seed_tenant(db)
    pro = await seed_professional(db, tenant, "Dr. X")
    _fake_accounts(monkeypatch, {tenant.id: {str(pro.id): ACCOUNT_A}})
    async with db() as session:
        first = await sync_tenant_agendas(session, tenant.id)
        await session.commit()
    async with db() as session:
        second = await sync_tenant_agendas(session, tenant.id)
        await session.commit()
    assert (first.changed, second.changed) == (1, 0)
    async with db() as session:
        assert len((await session.scalars(select(Agenda))).all()) == 1


async def test_professionals_brain_api_does_not_list_are_left_untouched(db, monkeypatch):
    tenant = await seed_tenant(db)
    linked = await seed_professional(db, tenant, "Dr. Linked")
    unlinked = await seed_professional(db, tenant, "Dr. NoAccount")
    _fake_accounts(monkeypatch, {tenant.id: {str(linked.id): ACCOUNT_A, str(uuid4()): ACCOUNT_B}})
    async with db() as session:
        result = await sync_tenant_agendas(session, tenant.id)
        await session.commit()
    id_linked, id_unlinked = await _agenda_ids(db, linked, unlinked)
    assert id_linked is not None and id_unlinked is None
    assert result.linked == 1
    async with db() as session:  # the unknown professional id never becomes an agenda
        assert len((await session.scalars(select(Agenda))).all()) == 1


async def test_another_tenants_professional_id_in_the_answer_is_never_linked(db, monkeypatch):
    tenant_p = await seed_tenant(db)
    tenant_c = await seed_tenant(db)
    foreign = await seed_professional(db, tenant_c, "Dr. Foreign")
    _fake_accounts(monkeypatch, {tenant_p.id: {str(foreign.id): ACCOUNT_A}})
    async with db() as session:
        await sync_tenant_agendas(session, tenant_p.id)
        await session.commit()
    assert await _agenda_ids(db, foreign) == [None]


async def test_brain_api_failure_links_nothing_and_does_not_raise(db, monkeypatch):
    tenant = await seed_tenant(db)
    pro = await seed_professional(db, tenant, "Dr. X")
    _fake_accounts(monkeypatch, None)
    async with db() as session:
        result = await sync_tenant_agendas(session, tenant.id)
        await session.commit()
    assert result.brain_ok is False and result.changed == 0 and result.linked == 0
    assert await _agenda_ids(db, pro) == [None]
    async with db() as session:
        assert (await session.scalars(select(Agenda))).all() == []


async def _through_real_client(monkeypatch, handler):
    settings = brain_professionals.get_settings()
    monkeypatch.setattr(settings, "BRAIN_API_BASE_URL", "https://brain.test", raising=False)
    monkeypatch.setattr(settings, "INTERNAL_API_KEY", "pair-key", raising=False)
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(brain_professionals.httpx, "AsyncClient", factory)


async def test_http_500_and_timeout_from_the_real_client_leave_no_agenda_and_no_error(
    db, monkeypatch
):
    tenant = await seed_tenant(db)
    pro = await seed_professional(db, tenant, "Dr. X")

    await _through_real_client(monkeypatch, lambda r: httpx.Response(500))
    async with db() as session:
        await sync_tenant_agendas(session, tenant.id)
        await try_link_professional(session, tenant.id, pro.id)

    def timeout(request):
        raise httpx.ReadTimeout("slow", request=request)

    await _through_real_client(monkeypatch, timeout)
    async with db() as session:
        await sync_tenant_agendas(session, tenant.id)
        await try_link_professional(session, tenant.id, pro.id)
    assert await _agenda_ids(db, pro) == [None]


async def test_ensure_agenda_relinks_to_the_current_account_and_reuses_rows(db):
    tenant = await seed_tenant(db)
    pro = await seed_professional(db, tenant, "Dr. X")
    async with db() as session:
        row = await session.get(Professional, pro.id)
        first = await ensure_agenda_for_professional(session, row, ACCOUNT_A)
        again = await ensure_agenda_for_professional(session, row, ACCOUNT_A)
        assert first.id == again.id == row.agenda_id
        moved = await ensure_agenda_for_professional(session, row, ACCOUNT_B)
        assert moved.id != first.id and row.agenda_id == moved.id
        await session.commit()


async def test_try_link_professional_links_only_that_professional_and_commits(db, monkeypatch):
    tenant = await seed_tenant(db)
    one = await seed_professional(db, tenant, "Dr. One")
    two = await seed_professional(db, tenant, "Dr. Two")
    _fake_accounts(monkeypatch, {tenant.id: {str(one.id): ACCOUNT_A, str(two.id): ACCOUNT_B}})
    async with db() as session:
        await try_link_professional(session, tenant.id, one.id)
    id_one, id_two = await _agenda_ids(db, one, two)
    assert id_one is not None and id_two is None


async def test_try_link_professional_swallows_unexpected_errors(db, monkeypatch):
    tenant = await seed_tenant(db)
    pro = await seed_professional(db, tenant, "Dr. X")

    async def boom(tenant_id):
        raise RuntimeError("brain-api client exploded")

    monkeypatch.setattr(agenda_linking, "fetch_professional_accounts", boom)
    async with db() as session:
        await try_link_professional(session, tenant.id, pro.id)  # must not raise
    assert await _agenda_ids(db, pro) == [None]


async def test_reconcile_all_tenants_links_everyone_and_dry_run_writes_nothing(db, monkeypatch):
    tenant_p = await seed_tenant(db)
    tenant_c = await seed_tenant(db)
    empty = await seed_tenant(db)  # no professionals: no brain-api call at all
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    calls = _fake_accounts(
        monkeypatch,
        {tenant_p.id: {str(x_p.id): ACCOUNT_A}, tenant_c.id: {str(x_c.id): ACCOUNT_A}},
    )

    dry = await reconcile_all_tenants(db, dry_run=True)
    assert await _agenda_ids(db, x_p, x_c) == [None, None]
    assert {tid: r.changed for tid, r in dry.items()} == {tenant_p.id: 1, tenant_c.id: 1}
    assert empty.id not in calls and empty.id not in dry
    async with db() as session:
        assert (await session.scalars(select(Agenda))).all() == []

    real = await reconcile_all_tenants(db, dry_run=False)
    id_p, id_c = await _agenda_ids(db, x_p, x_c)
    assert id_p is not None and id_p == id_c
    assert sum(r.changed for r in real.values()) == 2

    again = await reconcile_all_tenants(db, dry_run=False)  # idempotent
    assert sum(r.changed for r in again.values()) == 0
    assert await _agenda_ids(db, x_p, x_c) == [id_p, id_c]


async def test_reconcile_survives_one_tenant_failing(db, monkeypatch):
    tenant_a = await seed_tenant(db)
    tenant_b = await seed_tenant(db)
    pro_a = await seed_professional(db, tenant_a, "Dr. A")
    pro_b = await seed_professional(db, tenant_b, "Dr. B")

    async def fake(tenant_id):
        if tenant_id == tenant_a.id:
            raise RuntimeError("boom")
        return {str(pro_b.id): ACCOUNT_B}

    monkeypatch.setattr(agenda_linking, "fetch_professional_accounts", fake)
    result = await reconcile_all_tenants(db, dry_run=False)
    assert tenant_a.id not in result and result[tenant_b.id].changed == 1
    id_a, id_b = await _agenda_ids(db, pro_a, pro_b)
    assert id_a is None and id_b is not None


async def test_person_lock_takes_one_stable_key_per_account_on_postgres_only(db):
    from tests.test_agenda_service import _PgSession

    pg = _PgSession()
    await agenda_linking._person_lock(pg, ACCOUNT_A)
    await agenda_linking._person_lock(pg, ACCOUNT_A)
    await agenda_linking._person_lock(pg, ACCOUNT_B)
    keys = [params["key"] for _, params in pg.calls]
    assert len(keys) == 3 and keys[0] == keys[1] != keys[2]
    async with db() as session:  # SQLite: nothing to execute
        await agenda_linking._person_lock(session, ACCOUNT_A)
