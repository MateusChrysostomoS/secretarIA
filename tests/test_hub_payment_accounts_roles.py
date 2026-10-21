"""Role enforcement on the payment endpoints (TASK-020 dependencies), through the REAL chain.

Owner decision 2026-09-29: only a clinic MANAGER may touch the payment key/config, enforced in the
backend. manager -> 200; doctor -> may read the status, never mutate; secretary -> 403
`forbidden_role`; token without a role claim -> 403 `role_claim_missing`. The 403 answers before
the rate limiter and before any provider call, and nothing is stored or echoed.
"""

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.api.hub import deps as hub_deps
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import ClinicPaymentAccount, Tenant
from secretaria.services.payments import accounts, asaas_connection, asaas_provider
from secretaria.services.payments.asaas_probes import Verdict, VerdictKind
from tests.payments_support import FakeArqPool, FakeAsaasClient, make_sessionmaker

BASE = "https://secretaria.example.test"
KEY = "$aact_prod_0123456789abcdef0123456789abcdef0123456789"
CAPS = {"customers": True, "payments": True, "webhooks": True}
ENDPOINT = "/tenants/me/payment-accounts"
CONNECTION = f"{ENDPOINT}/asaas/connection"
HEADERS = {"authorization": "Bearer role-test-token", "content-type": "application/json"}
MUTATIONS = (
    ("PUT", CONNECTION),
    ("POST", f"{ENDPOINT}/asaas/retry-webhook"),
    ("DELETE", f"{ENDPOINT}/asaas"),
)


def _claim(tenant_id, role):
    """The claim brain-api would return. TASK-020 adds `role`; adjust ONLY here if it differs."""
    return SubscriptionClaim(tenant_id=tenant_id, active=True, role=role)


@pytest_asyncio.fixture
async def world(monkeypatch):
    from secretaria.main import app

    engine, maker = await make_sessionmaker()
    async with maker() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id="pn-role")
        session.add(tenant)
        await session.commit()

    async def _session():
        async with maker() as session:
            yield session

    pool = FakeArqPool()
    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    monkeypatch.setenv("PAYMENTS_WEBHOOK_BASE_URL", BASE)
    get_settings.cache_clear()
    fake = FakeAsaasClient()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    probe_calls: list[str] = []

    async def probes(api_key):
        probe_calls.append("probe")
        return Verdict(VerdictKind.ACCEPTED, None, (), (), dict(CAPS))

    monkeypatch.setattr(asaas_connection, "run_probe_suite", probes)
    role = {"value": None}

    async def _verify(token, *, fresh=False):
        assert fresh is True  # main requires live staff revocation checks on hub requests.
        return _claim(tenant.id, role["value"])

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield tenant, maker, pool, fake, probe_calls, role
    app.dependency_overrides.pop(get_session, None)
    get_settings.cache_clear()
    await engine.dispose()


async def _call(client, method, path):
    body = json.dumps({"api_key": KEY}) if method == "PUT" else None
    return await client.request(method, path, content=body, headers=HEADERS)


@pytest.mark.parametrize("method,path", MUTATIONS)
async def test_a_secretary_is_403_forbidden_role_on_every_mutating_endpoint_and_nothing_is_stored(
    client, world, method, path
):
    tenant, maker, pool, fake, probe_calls, role = world
    role["value"] = "secretary"
    response = await _call(client, method, path)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden_role"
    assert KEY not in response.text
    assert probe_calls == [] and fake.calls == [] and pool.ttls == {}
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []


@pytest.mark.parametrize("method,path", [*MUTATIONS, ("GET", ENDPOINT)])
async def test_a_token_without_a_role_claim_is_403_role_claim_missing_on_every_endpoint(
    client, world, method, path
):
    _, maker, pool, fake, probe_calls, role = world
    role["value"] = None
    response = await _call(client, method, path)

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "role_claim_missing"
    assert KEY not in response.text
    assert probe_calls == [] and fake.calls == [] and pool.ttls == {}
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []


async def test_a_manager_connects_retries_and_disconnects_with_200(client, world):
    tenant, maker, _, fake, _, role = world
    role["value"] = "manager"

    connected = await _call(client, "PUT", CONNECTION)
    assert connected.status_code == 200
    assert connected.json()["status"] == "connected" and KEY not in connected.text

    listed = await _call(client, "GET", ENDPOINT)
    assert listed.status_code == 200 and len(listed.json()) == 1

    retried = await _call(client, "POST", f"{ENDPOINT}/asaas/retry-webhook")
    assert retried.status_code == 200 and KEY not in retried.text

    removed = await _call(client, "DELETE", f"{ENDPOINT}/asaas")
    assert removed.status_code == 200 and removed.json()["status"] == "revoked"
    async with maker() as session:
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        assert row.api_key_encrypted is None


async def test_a_doctor_may_read_the_status_but_never_mutate(client, world):
    _, maker, pool, fake, probe_calls, role = world
    role["value"] = "doctor"

    assert (await _call(client, "GET", ENDPOINT)).status_code == 200
    for method, path in MUTATIONS:
        response = await _call(client, method, path)
        assert response.status_code == 403
        assert response.json()["detail"]["code"] == "forbidden_role"
    assert probe_calls == [] and fake.calls == [] and pool.ttls == {}


async def test_the_role_check_runs_before_the_rate_limiter_and_the_key_is_never_echoed(
    client, world
):
    _, _, pool, _, _, role = world
    role["value"] = "secretary"
    statuses = [(await _call(client, "PUT", CONNECTION)).status_code for _ in range(12)]

    assert statuses == [403] * 12  # never 429: a refused role does not spend an attempt
    assert pool.ttls == {}


async def test_a_secretary_cannot_read_the_status_either(client, world):
    _, _, _, _, _, role = world
    role["value"] = "secretary"
    response = await _call(client, "GET", ENDPOINT)
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "forbidden_role"
