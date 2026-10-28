"""The deposit settings a clinic edits in `/contexto` (spec sections 6.4, 12.1, 12.2; TASK-017).

Written through the EXISTING `PUT /tenants/me/configuration` (and the legacy `PUT
/tenants/me/config`); read through `GET /tenants/me/payment-settings`, which explains in words why
a control is disabled by asking the same functions the save uses. The rules: `before` only with
deposits on + the add-on active + a connected and verified account; the provider changes only
while nothing is pending with it; a provider without a wrapper cannot be chosen; the window is
30-120 minutes. A refused save is a 422 `{detail: {code, message}}` and changes nothing.
"""

from datetime import UTC, datetime

import pytest
import pytest_asyncio
from httpx import AsyncClient

from secretaria.api.hub.config import _claim_if_authenticated
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import PixDeposit, Tenant
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services.payments import registry
from tests.before_support import entitlement_summary, make_world
from tests.payments_support import FakeProvider, make_sessionmaker

CONFIGURATION = "/tenants/me/configuration"
SETTINGS = "/tenants/me/payment-settings"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


class Entitlement:
    value = entitlement_summary()


@pytest.fixture(autouse=True)
def _entitlement(monkeypatch):
    Entitlement.value = entitlement_summary()

    async def get(tenant_id, redis):
        return Entitlement.value

    monkeypatch.setitem(registry._PROVIDERS, "asaas", FakeProvider())


def _wire(db, tenant_id):
    from fastapi import Depends

    from secretaria.main import app

    async def _session():
        async with db() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant_id)

    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    # These tests are about the payment rules, so the caller is a manager. The ROLE gate itself is
    # proved with real tokens in `tests/test_payment_settings_roles.py`.
    app.dependency_overrides[_claim_if_authenticated] = lambda: SubscriptionClaim(
        tenant_id=tenant_id, active=True, role="manager", is_manager=True
    )


@pytest.fixture(autouse=True)
def _cleanup():
    yield
    from secretaria.main import app

    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.dependency_overrides.pop(_claim_if_authenticated, None)


async def _world(db, **kw):
    async with db() as session:
        world = await make_world(session, timing="after", **kw)
    _wire(db, world.tenant_id)
    return world


async def _put(client: AsyncClient, tenant_patch: dict, path=CONFIGURATION):
    body = {"tenant": tenant_patch} if path == CONFIGURATION else tenant_patch
    return await client.put(path, json=body)


async def _stored(db, tenant_id) -> Tenant:
    async with db() as session:
        return await session.get(Tenant, tenant_id)


def _code(response) -> str:
    return response.json()["detail"]["code"]


async def test_the_three_fields_are_saved_and_read_back(client: AsyncClient, db):
    world = await _world(db)
    response = await _put(client, {"deposit_timing": "before", "deposit_hold_ttl_minutes": 60})
    assert response.status_code == 200
    stored = await _stored(db, world.tenant_id)
    assert (stored.deposit_timing, stored.deposit_hold_ttl_minutes) == ("before", 60)
    read = (await client.get("/tenants/me/config")).json()
    assert (read["payment_provider"], read["deposit_timing"], read["deposit_hold_ttl_minutes"]) == (
        "asaas",
        "before",
        60,
    )


@pytest.mark.parametrize("minutes", [0, 29, 121, 500])
async def test_a_window_outside_30_to_120_minutes_is_refused(client: AsyncClient, db, minutes):
    world = await _world(db)
    response = await _put(client, {"deposit_hold_ttl_minutes": minutes})
    assert response.status_code == 422
    assert (await _stored(db, world.tenant_id)).deposit_hold_ttl_minutes == 30


async def test_an_unknown_timing_or_provider_is_refused(client: AsyncClient, db):
    await _world(db)
    assert (await _put(client, {"deposit_timing": "during"})).status_code == 422
    assert (await _put(client, {"payment_provider": "pagseguro"})).status_code == 422


async def test_before_is_accepted_without_the_old_addon(client: AsyncClient, db):
    """The deposit is a core SecretarIA capability since 2026-10-28: switched on + a connected
    account is all `before` needs; the plan's add-ons are never consulted."""
    world = await _world(db)
    Entitlement.value = entitlement_summary(addon=False)
    response = await _put(client, {"deposit_timing": "before", "deposit_hold_ttl_minutes": 90})
    assert response.status_code == 200
    stored = await _stored(db, world.tenant_id)
    assert (stored.deposit_timing, stored.deposit_hold_ttl_minutes) == ("before", 90)


async def test_before_is_refused_with_deposits_switched_off(client: AsyncClient, db):
    await _world(db, pix_deposit_enabled=False)
    response = await _put(client, {"deposit_timing": "before"})
    assert _code(response) == "deposit_disabled"


async def test_before_can_be_turned_on_in_the_same_save_that_enables_deposits(
    client: AsyncClient, db
):
    world = await _world(db, pix_deposit_enabled=False)
    response = await _put(client, {"pix_deposit_enabled": True, "deposit_timing": "before"})
    assert response.status_code == 200
    assert (await _stored(db, world.tenant_id)).deposit_timing == "before"


async def test_before_is_refused_without_a_connected_and_verified_account(
    client: AsyncClient, db
):
    await _world(db, with_account=False)
    response = await _put(client, {"deposit_timing": "before"})
    assert _code(response) == "account_not_ready"


async def test_before_never_waits_on_brain_api(client: AsyncClient, db):
    await _world(db)
    Entitlement.value = None
    assert (await _put(client, {"deposit_timing": "before"})).status_code == 200


async def test_a_save_that_touches_neither_field_is_never_blocked_by_these_rules(
    client: AsyncClient, db
):
    """A config save is always allowed: a clinic already on `before` whose add-on lapsed can
    still fix its address or its hours."""
    world = await _world(db)
    assert (await _put(client, {"deposit_timing": "before"})).status_code == 200
    Entitlement.value = entitlement_summary(addon=False)
    response = await _put(client, {"clinic_description": "Oftalmologia."})
    assert response.status_code == 200
    assert (await _stored(db, world.tenant_id)).deposit_timing == "before"


async def test_going_back_to_after_is_always_allowed(client: AsyncClient, db):
    world = await _world(db)
    await _put(client, {"deposit_timing": "before"})
    Entitlement.value = entitlement_summary(addon=False)
    assert (await _put(client, {"deposit_timing": "after"})).status_code == 200
    assert (await _stored(db, world.tenant_id)).deposit_timing == "after"


async def test_the_legacy_single_scope_put_enforces_the_same_rules(client: AsyncClient, db):
    world = await _world(db, with_account=False)
    response = await _put(client, {"deposit_timing": "before"}, path="/tenants/me/config")
    assert response.status_code == 422 and _code(response) == "account_not_ready"
    assert (await _stored(db, world.tenant_id)).deposit_timing == "after"


async def test_a_provider_without_a_wrapper_cannot_be_selected(client: AsyncClient, db):
    world = await _world(db)
    response = await _put(client, {"payment_provider": "mercadopago"})
    assert _code(response) == "provider_unavailable"
    assert (await _stored(db, world.tenant_id)).payment_provider == "asaas"


async def test_the_provider_changes_once_a_wrapper_exists_and_nothing_is_pending(
    client: AsyncClient, db, monkeypatch
):
    world = await _world(db)
    monkeypatch.setitem(registry._PROVIDERS, "mercadopago", FakeProvider(name="mercadopago"))
    response = await _put(client, {"payment_provider": "mercadopago"})
    assert response.status_code == 200
    assert (await _stored(db, world.tenant_id)).payment_provider == "mercadopago"


@pytest.mark.parametrize(
    ("status", "fulfillment"),
    [
        (PixDepositStatus.AWAITING, "none"),
        (PixDepositStatus.PAID, "promoting"),
    ],
)
async def test_the_provider_cannot_change_under_a_live_charge(
    client: AsyncClient, db, monkeypatch, status, fulfillment
):
    world = await _world(db)
    monkeypatch.setitem(registry._PROVIDERS, "mercadopago", FakeProvider(name="mercadopago"))
    async with db() as session:
        session.add(
            PixDeposit(
                tenant_id=world.tenant_id,
                amount_cents=6000,
                percent_applied=30,
                status=status,
                fulfillment_status=fulfillment,
                paid_at=datetime.now(UTC),
            )
        )
        await session.commit()
    response = await _put(client, {"payment_provider": "mercadopago"})
    assert _code(response) == "open_deposits"
    assert (await _stored(db, world.tenant_id)).payment_provider == "asaas"


async def test_a_confirmed_or_expired_charge_does_not_lock_the_provider(
    client: AsyncClient, db, monkeypatch
):
    world = await _world(db)
    monkeypatch.setitem(registry._PROVIDERS, "mercadopago", FakeProvider(name="mercadopago"))
    async with db() as session:
        session.add_all(
            [
                PixDeposit(
                    tenant_id=world.tenant_id,
                    amount_cents=6000,
                    percent_applied=30,
                    status=PixDepositStatus.PAID,
                    fulfillment_status="confirmed",
                ),
                PixDeposit(
                    tenant_id=world.tenant_id,
                    amount_cents=6000,
                    percent_applied=30,
                    status=PixDepositStatus.EXPIRED,
                ),
            ]
        )
        await session.commit()
    assert (await _put(client, {"payment_provider": "mercadopago"})).status_code == 200


async def test_the_settings_read_explains_every_disabled_control(client: AsyncClient, db):
    world = await _world(db, with_account=False, pix_deposit_enabled=False)
    Entitlement.value = entitlement_summary(addon=False)
    async with db() as session:
        session.add(
            PixDeposit(
                tenant_id=world.tenant_id,
                amount_cents=6000,
                percent_applied=30,
                status=PixDepositStatus.AWAITING,
            )
        )
        await session.commit()
    body = (await client.get(SETTINGS)).json()
    assert body["payment_provider"] == "asaas" and body["deposit_timing"] == "after"
    assert body["deposit_hold_ttl_minutes"] == 30
    assert (body["ttl_min_minutes"], body["ttl_max_minutes"]) == (30, 120)
    assert body["deposit_enabled"] is False and body["deposit_percent"] == 30
    assert {c["provider"]: c["available"] for c in body["providers"]} == {
        "asaas": True,
        "mercadopago": False,
    }
    assert [b["code"] for b in body["before_blockers"]] == [
        "deposit_disabled",
        "account_not_ready",
    ]
    assert all(b["message"] for b in body["before_blockers"])
    assert [b["code"] for b in body["provider_switch_blockers"]] == ["open_deposits"]


async def test_a_ready_clinic_has_no_blockers(client: AsyncClient, db):
    await _world(db)
    body = (await client.get(SETTINGS)).json()
    assert body["before_blockers"] == [] and body["provider_switch_blockers"] == []


async def test_another_clinics_charge_never_locks_this_clinics_provider(
    client: AsyncClient, db
):
    await _world(db)
    async with db() as session:
        other = await make_world(session)
        session.add(
            PixDeposit(
                tenant_id=other.tenant_id,
                amount_cents=6000,
                percent_applied=30,
                status=PixDepositStatus.AWAITING,
            )
        )
        await session.commit()
    assert (await client.get(SETTINGS)).json()["provider_switch_blockers"] == []


# --------------------------------------------------------------------------
# TASK-053: the optional environment block of the "Pagamento do sinal" screen
# --------------------------------------------------------------------------


async def test_the_asaas_environment_follows_the_base_url(client: AsyncClient, db, monkeypatch):
    from secretaria.config import get_settings

    await _world(db)
    try:
        monkeypatch.setenv("ASAAS_BASE_URL", "https://api-sandbox.asaas.com/v3")
        get_settings.cache_clear()
        assert (await client.get(SETTINGS)).json()["asaas_environment"] == "sandbox"
        monkeypatch.setenv("ASAAS_BASE_URL", "https://api.asaas.com/v3")
        get_settings.cache_clear()
        assert (await client.get(SETTINGS)).json()["asaas_environment"] == "production"
    finally:
        get_settings.cache_clear()


async def test_no_egress_ip_is_exposed_or_configurable(client: AsyncClient, db):
    """Owner decision 2026-10-12: Asaas does not require a fixed IP; the IP list is the clinic's
    own option. No server setting, no field - the screen carries only a fixed informational note."""
    from secretaria.config import get_settings

    await _world(db)
    body = (await client.get(SETTINGS)).json()
    assert "egress_ip" not in body
    assert not hasattr(get_settings(), "PAYMENTS_EGRESS_IP")
