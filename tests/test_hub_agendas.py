# ruff: noqa: F811
"""Hub + internal triggers of the shared agenda (TASK-083 spec §6/§9).

  POST /tenants/me/agendas/sync                       (manager only)
  GET  /tenants/me/professionals/{id}/agenda          (any hub user, never names a clinic)
  POST /internal/tenants/{id}/professionals           (attach -> best-effort agenda link)

The hub NEVER accepts `agenda_id` / `person_key` from the client.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402

from secretaria.api.hub import deps as hub_deps  # noqa: E402
from secretaria.core.database import get_session  # noqa: E402
from secretaria.core.subscription import SubscriptionClaim  # noqa: E402
from secretaria.models import Agenda, Professional  # noqa: E402
from secretaria.services import agenda_linking  # noqa: E402
from tests._local_agenda import seed_professional, seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

HEADERS = {"authorization": "Bearer agenda-test-token"}
INTERNAL = {"X-Internal-Api-Key": "test-internal-key"}
ACCOUNT = str(uuid4())


class _AnyProfessional(dict):
    """brain-api says: whoever you ask about is account ACCOUNT."""

    def get(self, key, default=None):
        return ACCOUNT

    def __contains__(self, key):  # everyone is listed, so the unlink pass leaves them alone
        return True


@pytest_asyncio.fixture
async def world(db, monkeypatch):
    from secretaria.main import app

    tenant_p = await seed_tenant(db, name="Clínica Pessoal")
    tenant_c = await seed_tenant(db, name="Clínica Concorrente")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    role = {"value": "manager"}
    accounts = {"value": {tenant_p.id: {str(x_p.id): ACCOUNT}, tenant_c.id: {str(x_c.id): ACCOUNT}}}

    async def _session():
        async with db() as session:
            yield session

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(tenant_id=tenant_p.id, active=True, role=role["value"])

    async def _accounts(tenant_id):
        value = accounts["value"]
        return None if value is None else value.get(tenant_id, {})

    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    monkeypatch.setattr(agenda_linking, "fetch_professional_accounts", _accounts)
    yield {
        "tenant_p": tenant_p,
        "tenant_c": tenant_c,
        "x_p": x_p,
        "x_c": x_c,
        "role": role,
        "accounts": accounts,
        "db": db,
    }
    app.dependency_overrides.pop(get_session, None)


async def _agenda_ids(db, *pros):
    async with db() as session:
        return [(await session.get(Professional, p.id)).agenda_id for p in pros]


# ------------------------------------------------------------------ sync


async def test_manager_sync_links_the_tenants_professionals_and_reports_counts(client, world):
    first = await client.post("/tenants/me/agendas/sync", headers=HEADERS)
    assert first.status_code == 200, first.text
    assert first.json() == {"linked": 1, "shared": 0}  # C is not synced yet: nobody to share with

    # The other clinic's own sync (C's tenant resolves from ITS token) closes the pair.
    world["accounts"]["value"][world["tenant_p"].id] = {str(world["x_p"].id): ACCOUNT}
    async with world["db"]() as session:
        await agenda_linking.sync_tenant_agendas(session, world["tenant_c"].id)
        await session.commit()
    second = await client.post("/tenants/me/agendas/sync", headers=HEADERS)
    assert second.json() == {"linked": 1, "shared": 1}
    id_p, id_c = await _agenda_ids(world["db"], world["x_p"], world["x_c"])
    assert id_p is not None and id_p == id_c


async def test_sync_only_touches_the_callers_tenant(client, world):
    await client.post("/tenants/me/agendas/sync", headers=HEADERS)
    assert await _agenda_ids(world["db"], world["x_c"]) == [None]


@pytest.mark.parametrize("role", ["secretary", "doctor"])
async def test_sync_is_manager_only(client, world, role):
    world["role"]["value"] = role
    response = await client.post("/tenants/me/agendas/sync", headers=HEADERS)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden_role"
    assert await _agenda_ids(world["db"], world["x_p"]) == [None]


async def test_sync_without_token_is_401(client, world):
    assert (await client.post("/tenants/me/agendas/sync")).status_code == 401


@pytest.mark.parametrize("body", [{"agenda_id": str(uuid4())}, {"person_key": "x"}])
async def test_sync_rejects_identity_fields_in_the_body(client, world, body):
    response = await client.post("/tenants/me/agendas/sync", headers=HEADERS, json=body)
    assert response.status_code == 422
    assert await _agenda_ids(world["db"], world["x_p"]) == [None]


async def test_sync_brain_api_down_answers_200_with_nothing_linked(client, world):
    world["accounts"]["value"] = None
    response = await client.post("/tenants/me/agendas/sync", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"linked": 0, "shared": 0}
    assert await _agenda_ids(world["db"], world["x_p"]) == [None]


async def test_professional_patch_ignores_a_client_supplied_agenda_id(client, world):
    async with world["db"]() as session:
        mine = Agenda(person_key="mine")
        session.add(mine)
        await session.commit()
        forged = mine.id
    response = await client.patch(
        f"/tenants/me/professionals/{world['x_p'].id}",
        headers=HEADERS,
        json={"name": "Dr. X", "agenda_id": str(forged), "person_key": "mine"},
    )
    assert response.status_code == 200
    assert await _agenda_ids(world["db"], world["x_p"]) == [None]


# ------------------------------------------------------------------ GET .../agenda


async def test_agenda_endpoint_says_shared_without_naming_anyone(client, world):
    async with world["db"]() as session:
        await agenda_linking.sync_tenant_agendas(session, world["tenant_p"].id)
        await agenda_linking.sync_tenant_agendas(session, world["tenant_c"].id)
        await session.commit()
    response = await client.get(
        f"/tenants/me/professionals/{world['x_p'].id}/agenda", headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json() == {"shared": True, "other_tenants_count": 1}
    for leaked in (
        "Concorrente",
        "Clínica",
        str(world["tenant_c"].id),
        str(world["x_c"].id),
        ACCOUNT,
    ):
        assert leaked not in response.text


async def test_agenda_endpoint_not_shared_when_alone_or_unlinked(client, world):
    unlinked = await client.get(
        f"/tenants/me/professionals/{world['x_p'].id}/agenda", headers=HEADERS
    )
    assert unlinked.json() == {"shared": False, "other_tenants_count": 0}
    async with world["db"]() as session:
        await agenda_linking.sync_tenant_agendas(session, world["tenant_p"].id)
        await session.commit()
    alone = await client.get(f"/tenants/me/professionals/{world['x_p'].id}/agenda", headers=HEADERS)
    assert alone.json() == {"shared": False, "other_tenants_count": 0}


async def test_agenda_endpoint_counts_distinct_other_tenants(client, world):
    third = await seed_tenant(world["db"], name="Clínica Terceira")
    x_t = await seed_professional(world["db"], third, "Dr. X")
    world["accounts"]["value"][third.id] = {str(x_t.id): ACCOUNT}
    async with world["db"]() as session:
        for tenant in (world["tenant_p"], world["tenant_c"], third):
            await agenda_linking.sync_tenant_agendas(session, tenant.id)
        await session.commit()
    response = await client.get(
        f"/tenants/me/professionals/{world['x_p'].id}/agenda", headers=HEADERS
    )
    assert response.json() == {"shared": True, "other_tenants_count": 2}


async def test_agenda_endpoint_secretary_may_read_and_other_tenants_professional_is_404(
    client, world
):
    world["role"]["value"] = "secretary"
    ok = await client.get(f"/tenants/me/professionals/{world['x_p'].id}/agenda", headers=HEADERS)
    assert ok.status_code == 200
    foreign = await client.get(
        f"/tenants/me/professionals/{world['x_c'].id}/agenda", headers=HEADERS
    )
    assert foreign.status_code == 404
    bad = await client.get("/tenants/me/professionals/not-a-uuid/agenda", headers=HEADERS)
    assert bad.status_code == 404


# ------------------------------------------------------------------ attach trigger


async def test_internal_attach_links_the_new_professional_when_brain_api_knows_the_account(
    client, world, monkeypatch
):
    async def accounts(tenant_id):
        return _AnyProfessional()

    monkeypatch.setattr(agenda_linking, "fetch_professional_accounts", accounts)
    response = await client.post(
        f"/internal/tenants/{world['tenant_p'].id}/professionals",
        headers=INTERNAL,
        json={"name": "Dra. Nova"},
    )
    assert response.status_code == 200
    new_id = response.json()["professional_id"]
    async with world["db"]() as session:
        agendas = (await session.scalars(select(Agenda))).all()
        new = (
            await session.scalars(select(Professional).where(Professional.id == UUID(new_id)))
        ).one()
        assert new.agenda_id is not None and [a.person_key for a in agendas] == [ACCOUNT]


@pytest.mark.parametrize("failure", ["none", "raises"])
async def test_internal_attach_is_unchanged_when_brain_api_fails(
    client, world, monkeypatch, failure
):
    async def accounts(tenant_id):
        if failure == "raises":
            raise RuntimeError("brain-api exploded")
        return None

    monkeypatch.setattr(agenda_linking, "fetch_professional_accounts", accounts)
    response = await client.post(
        f"/internal/tenants/{world['tenant_p'].id}/professionals",
        headers=INTERNAL,
        json={"name": "Dra. Nova"},
    )
    assert response.status_code == 200
    assert response.json()["created"] is True
    async with world["db"]() as session:
        assert (await session.scalars(select(Agenda))).all() == []
