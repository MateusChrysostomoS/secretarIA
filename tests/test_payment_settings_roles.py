"""Only a clinic MANAGER may write the payment configuration (owner answer 12, TASK-020).

A `PUT` (`/tenants/me/configuration` and the legacy `/tenants/me/config`) that touches
`payment_provider`, `deposit_timing` or `deposit_hold_ttl_minutes` needs a manager: a secretary
gets `403 forbidden_role`, a token without a role claim `403 role_claim_missing`, and NOTHING is
saved. The refusal comes before every payment rule and before any call to brain-api. A save that
touches none of those fields is open to whoever could save it before, and the diagnosis
(`GET /tenants/me/payment-settings`) stays readable by every Chat role.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Tenant
from secretaria.services.payments import deposit_gate, registry
from tests.before_support import entitlement_summary, make_world
from tests.hub_auth import install_fake_brain_api, make_hub_token, reset_fake_brain_api
from tests.payments_support import FakeProvider, make_sessionmaker

CONFIGURATION = "/tenants/me/configuration"
LEGACY = "/tenants/me/config"
SETTINGS = "/tenants/me/payment-settings"
PAYMENT_PATCHES = [
    {"payment_provider": "asaas"},
    {"deposit_timing": "before"},
    {"deposit_hold_ttl_minutes": 60},
]


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _gate(monkeypatch):
    install_fake_brain_api(monkeypatch)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", FakeProvider())

    async def entitlements(tenant_id, redis):
        return entitlement_summary()

    monkeypatch.setattr(deposit_gate, "get_entitlements", entitlements)
    yield
    reset_fake_brain_api()
    from secretaria.main import app

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _world(db):
    from fastapi import Depends

    from secretaria.main import app

    async with db() as session:
        world = await make_world(session, timing="after")

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, world.tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant  # tenant only; the ROLE is real
    return world


def _bearer(world, role, **flags) -> dict[str, str]:
    token = make_hub_token(tenant_id=world.tenant_id, role=role, **flags)
    return {"Authorization": f"Bearer {token}"}


async def _put(client: AsyncClient, world, role, patch, *, legacy=False, **flags):
    path, body = (LEGACY, patch) if legacy else (CONFIGURATION, {"tenant": patch})
    return await client.put(path, json=body, headers=_bearer(world, role, **flags))


async def _stored(db, world) -> Tenant:
    async with db() as session:
        return await session.get(Tenant, world.tenant_id)


@pytest.mark.parametrize("legacy", [False, True], ids=["configuration", "config"])
@pytest.mark.parametrize("patch", PAYMENT_PATCHES, ids=lambda p: next(iter(p)))
async def test_a_secretary_cannot_change_the_payment_fields_and_nothing_is_saved(
    client: AsyncClient, db, patch, legacy
):
    world = await _world(db)
    before = await _stored(db, world)
    for flags in ({}, {"is_owner": True, "is_manager": True}):  # a flag never promotes her
        response = await _put(client, world, "secretary", patch, legacy=legacy, **flags)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "forbidden_role"
    after = await _stored(db, world)
    assert (after.payment_provider, after.deposit_timing, after.deposit_hold_ttl_minutes) == (
        before.payment_provider,
        before.deposit_timing,
        before.deposit_hold_ttl_minutes,
    )


async def test_a_doctor_who_is_not_a_manager_cannot_either(client: AsyncClient, db):
    world = await _world(db)
    response = await _put(client, world, "doctor", {"deposit_hold_ttl_minutes": 60})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden_role"
    assert (await _stored(db, world)).deposit_hold_ttl_minutes == 30


async def test_a_token_without_a_role_claim_is_role_claim_missing(client: AsyncClient, db):
    world = await _world(db)
    response = await _put(client, world, None, {"deposit_hold_ttl_minutes": 60})
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "role_claim_missing"
    assert (await _stored(db, world)).deposit_hold_ttl_minutes == 30


@pytest.mark.parametrize(
    ("role", "flags"),
    [("manager", {"is_manager": True}), ("doctor", {"is_owner": True})],
    ids=["manager", "owner-doctor"],
)
async def test_a_manager_saves_the_payment_fields(client: AsyncClient, db, role, flags):
    world = await _world(db)
    response = await _put(client, world, role, {"deposit_hold_ttl_minutes": 60}, **flags)
    assert response.status_code == 200
    assert (await _stored(db, world)).deposit_hold_ttl_minutes == 60


async def test_a_save_that_touches_no_payment_field_needs_no_manager(client: AsyncClient, db):
    world = await _world(db)
    response = await _put(client, world, "secretary", {"pix_reschedule_limit": 2})
    assert response.status_code == 200
    assert (await _stored(db, world)).pix_reschedule_limit == 2


async def test_the_role_refusal_comes_before_the_payment_rules_and_brain_api(
    client: AsyncClient, db, monkeypatch
):
    world = await _world(db)

    async def entitlement_must_not_be_asked(tenant_id, redis):
        raise AssertionError("a role refusal must not reach the add-on check")

    monkeypatch.setattr(deposit_gate, "get_entitlements", entitlement_must_not_be_asked)
    response = await _put(client, world, "secretary", {"deposit_timing": "before"})
    assert response.status_code == 403  # not the 422 `addon_required` a manager would get
    assert response.json()["detail"]["code"] == "forbidden_role"


async def test_the_diagnosis_stays_readable_by_every_chat_role(client: AsyncClient, db):
    world = await _world(db)
    response = await client.get(SETTINGS, headers=_bearer(world, "secretary"))
    assert response.status_code == 200
    assert response.json()["deposit_timing"] == "after"
