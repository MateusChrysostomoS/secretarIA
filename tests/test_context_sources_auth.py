"""Context sources through the REAL hub authentication (TASK-067 port of TASK-026).

tests/test_context_sources_api.py overrides `get_current_tenant`; here only the brain-api answer is
faked, so the routes go through the same chain as production: bearer -> fresh introspection (no
positive cache, a revoked staff session is refused at once) -> the tenant of the CLAIM, never one
from the request. The routes are role-blind on purpose: they gate exactly like the config Save of
the same fields (clinic facts / messages are not manager-only); the model never writes, Save does.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.api.hub import deps as hub_deps
from secretaria.core.database import get_session
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import Tenant
from secretaria.models.context_source import ContextSource
from tests.payments_support import FakeArqPool, make_sessionmaker

BASE = "/tenants/me/context-sources"
HEADERS = {"authorization": "Bearer sources-test-token"}
TEXT = "Estacionamento gratuito para pacientes na garagem do prédio. " * 3


@pytest_asyncio.fixture
async def world(monkeypatch):
    from secretaria.main import app

    engine, maker = await make_sessionmaker()
    clinic_a, clinic_b = uuid4(), uuid4()
    async with maker() as session:
        session.add_all(
            [
                Tenant(id=clinic_a, clinic_name="Clínica A", phone_number_id=None),
                Tenant(id=clinic_b, clinic_name="Clínica B", phone_number_id=None),
            ]
        )
        await session.commit()

    async def _session():
        async with maker() as session:
            yield session

    pool = FakeArqPool()
    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    caller = {"claim": SubscriptionClaim(tenant_id=clinic_a, active=True, role="manager")}
    fresh_flags: list[bool] = []

    async def _verify(token, *, fresh=False):
        fresh_flags.append(fresh)
        return caller["claim"]

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield {
        "maker": maker,
        "pool": pool,
        "a": clinic_a,
        "b": clinic_b,
        "caller": caller,
        "fresh": fresh_flags,
    }
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


async def _seed(maker, tenant_id) -> ContextSource:
    async with maker() as session:
        source = ContextSource(
            tenant_id=tenant_id,
            kind="text",
            label="Texto colado",
            status="done",
            proposal={"fields": [], "services": []},
        )
        session.add(source)
        await session.commit()
        return source


@pytest.mark.parametrize(
    "method,path",
    [("POST", BASE), ("GET", BASE), ("GET", f"{BASE}/{uuid4()}"), ("DELETE", f"{BASE}/{uuid4()}")],
)
async def test_no_bearer_is_401_and_nothing_is_queued(client, world, method, path):
    body = {"kind": "text", "text": TEXT} if method == "POST" else None
    response = await client.request(method, path, json=body)
    assert response.status_code == 401
    assert world["pool"].jobs == []


async def test_a_revoked_staff_session_is_refused_with_a_fresh_check(client, world):
    world["caller"]["claim"] = SubscriptionClaim(tenant_id=world["a"], active=False, role="doctor")
    response = await client.post(BASE, json={"kind": "text", "text": TEXT}, headers=HEADERS)
    assert response.status_code == 401
    assert world["fresh"] and all(world["fresh"])  # never a cached "still allowed"
    assert world["pool"].jobs == []
    async with world["maker"]() as session:
        assert (await session.scalars(select(ContextSource))).first() is None


async def test_the_source_belongs_to_the_clinic_of_the_token(client, world):
    response = await client.post(BASE, json={"kind": "text", "text": TEXT}, headers=HEADERS)
    assert response.status_code == 202
    assert world["pool"].jobs == [("ingest_context_source", (response.json()["id"],), {})]
    async with world["maker"]() as session:
        row = (await session.scalars(select(ContextSource))).one()
    assert row.tenant_id == world["a"]


async def test_another_clinic_cannot_read_list_or_delete_a_source(client, world):
    theirs = await _seed(world["maker"], world["a"])
    world["caller"]["claim"] = SubscriptionClaim(tenant_id=world["b"], active=True, role="manager")

    assert (await client.get(BASE, headers=HEADERS)).json()["items"] == []
    assert (await client.get(f"{BASE}/{theirs.id}", headers=HEADERS)).status_code == 404
    assert (await client.delete(f"{BASE}/{theirs.id}", headers=HEADERS)).status_code == 404

    async with world["maker"]() as session:
        intact = await session.get(ContextSource, theirs.id)
    assert intact.deleted_at is None and intact.proposal == {"fields": [], "services": []}


@pytest.mark.parametrize("role", ["manager", "doctor", "secretary"])
async def test_every_staff_role_that_can_save_the_context_can_submit_a_source(client, world, role):
    world["caller"]["claim"] = SubscriptionClaim(tenant_id=world["a"], active=True, role=role)
    response = await client.post(BASE, json={"kind": "text", "text": TEXT}, headers=HEADERS)
    assert response.status_code == 202
