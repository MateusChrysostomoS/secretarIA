"""Who may resolve an attention item (owner answer 12, TASK-020).

`resolve` depends on `require_clinic_clinician` (a manager or a doctor). The gate is REAL here:
`make_hub_token` mints the token, `install_fake_brain_api` answers the introspection, and nothing
overrides the dependency (the behaviour tests of `test_payment_attention_hub.py` stand the gate in
for the tenant stub). A refusal must leave the item untouched, come before the body is read and
before the rate limiter, and the list stays readable by every Chat role.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub import payment_attention as hub_attention
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import PaymentAttentionItem, PixDeposit, Tenant
from secretaria.models.pix_deposit import FULFILLMENT_NEEDS_ATTENTION, PixDepositStatus
from tests.before_support import make_world
from tests.hub_auth import install_fake_brain_api, make_hub_token, reset_fake_brain_api
from tests.payments_support import make_sessionmaker

ITEMS = "/tenants/me/payment-attention-items"
BODY = {
    "action": "refunded_in_provider_panel",
    "note": "Devolvi o valor no painel do provedor hoje de manhã.",
    "resolved_by_name": "Dra. Ana",
}
NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _brain_api(monkeypatch):
    install_fake_brain_api(monkeypatch)
    yield
    reset_fake_brain_api()
    from secretaria.main import app

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _world_with_an_open_item(db):
    from fastapi import Depends

    from secretaria.main import app

    async with db() as session:
        world = await make_world(session, timing="before")
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=world.tenant_id,
            patient_id=world.patient.id,
            provider="asaas",
            provider_charge_id="pay_roles",
            asaas_payment_id=None,
            amount_cents=6000,
            percent_applied=30,
            paid_amount_cents=6000,
            status=PixDepositStatus.PAID,
            fulfillment_status=FULFILLMENT_NEEDS_ATTENTION,
            paid_at=NOW,
        )
        session.add(deposit)
        await session.flush()
        item = PaymentAttentionItem(
            id=uuid4(),
            tenant_id=world.tenant_id,
            deposit_id=deposit.id,
            provider="asaas",
            provider_charge_id="pay_roles",
            patient_id=world.patient.id,
            reason="late_slot_taken",
            status="open",
            amount_cents=6000,
            paid_amount_cents=6000,
            paid_at=NOW,
            slot_start=NOW + timedelta(days=2),
            slot_end=NOW + timedelta(days=2, minutes=30),
            service_label="Consulta",
            detected_at=NOW,
            dedupe_key=f"asaas:{uuid4().hex}:late_slot_taken",
        )
        session.add(item)
        await session.commit()

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, world.tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant  # only the LIST uses this one
    return world, item.id


def _bearer(world, role, **flags) -> dict[str, str]:
    token = make_hub_token(tenant_id=world.tenant_id, role=role, **flags)
    return {"Authorization": f"Bearer {token}"}


async def _status(db, item_id) -> str:
    async with db() as session:
        return await session.scalar(
            select(PaymentAttentionItem.status).where(PaymentAttentionItem.id == item_id)
        )


async def test_a_secretary_cannot_resolve_an_item_and_it_stays_open(client: AsyncClient, db):
    world, item_id = await _world_with_an_open_item(db)
    for flags in ({}, {"is_owner": True, "is_manager": True}):  # a flag never promotes her
        response = await client.post(
            f"{ITEMS}/{item_id}/resolve", json=BODY, headers=_bearer(world, "secretary", **flags)
        )
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "forbidden_role"
    assert await _status(db, item_id) == "open"


async def test_a_token_without_a_role_claim_is_role_claim_missing_on_resolve(
    client: AsyncClient, db
):
    world, item_id = await _world_with_an_open_item(db)
    response = await client.post(
        f"{ITEMS}/{item_id}/resolve", json=BODY, headers=_bearer(world, None)
    )
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "role_claim_missing"
    assert await _status(db, item_id) == "open"


@pytest.mark.parametrize(
    ("role", "flags"),
    [("manager", {"is_manager": True}), ("doctor", {})],
    ids=["manager", "doctor"],
)
async def test_a_manager_and_a_doctor_resolve_an_item(client: AsyncClient, db, role, flags):
    world, item_id = await _world_with_an_open_item(db)
    response = await client.post(
        f"{ITEMS}/{item_id}/resolve", json=BODY, headers=_bearer(world, role, **flags)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "resolved"
    assert await _status(db, item_id) == "resolved"


async def test_the_role_refusal_comes_before_the_body_and_the_rate_limiter(
    client: AsyncClient, db, monkeypatch
):
    world, item_id = await _world_with_an_open_item(db)

    async def limiter_must_not_run(*args, **kwargs):
        raise AssertionError("a role refusal must never consume a limiter attempt")

    monkeypatch.setattr(hub_attention, "allow", limiter_must_not_run)
    response = await client.post(
        f"{ITEMS}/{item_id}/resolve", content=b"{not json", headers=_bearer(world, "secretary")
    )
    assert response.status_code == 403  # not a 422 for the broken body: the role came first
    assert response.json()["detail"]["code"] == "forbidden_role"


async def test_a_secretary_can_still_read_the_items(client: AsyncClient, db):
    world, _item_id = await _world_with_an_open_item(db)
    response = await client.get(ITEMS, headers=_bearer(world, "secretary"))
    assert response.status_code == 200
    assert response.json()["open_count"] == 1
