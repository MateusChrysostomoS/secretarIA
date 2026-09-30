"""Hub endpoints of the payment connection (api/hub/payment_accounts.py), spec section 12.3.

The single exception to "a credential never enters through a form": ONE write-only field, the
Asaas API key, in `PUT /tenants/me/payment-accounts/asaas/connection`. Every test here that
sends a key also proves the key is in NO response - success, refusal, validation error.
"""

import json
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub.deps import (
    get_current_tenant,
    require_clinic_clinician,
    require_clinic_manager,
)
from secretaria.config import get_settings
from secretaria.core.database import get_session
from secretaria.models import ClinicPaymentAccount, Tenant
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, asaas_connection, asaas_provider
from secretaria.services.payments.asaas import AsaasError
from secretaria.services.payments.asaas_probes import (
    CODE_EXCESS,
    CODE_UNAVAILABLE,
    Verdict,
    VerdictKind,
)
from tests.payments_support import FakeArqPool, FakeAsaasClient, make_sessionmaker

BASE = "https://secretaria.example.test"
KEY = "$aact_prod_0123456789abcdef0123456789abcdef0123456789"
CAPS = {"customers": True, "payments": True, "webhooks": True}
ENDPOINT = "/tenants/me/payment-accounts"
CONNECTION = f"{ENDPOINT}/asaas/connection"
ACCEPTED = Verdict(VerdictKind.ACCEPTED, None, (), (), dict(CAPS))


@pytest_asyncio.fixture
async def world(monkeypatch):
    from fastapi import Depends

    from secretaria.main import app

    engine, maker = await make_sessionmaker()
    async with maker() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id="pn-a")
        other = Tenant(id=uuid4(), clinic_name="Other", phone_number_id="pn-b")
        session.add_all([tenant, other])
        await session.commit()

    async def _session():
        async with maker() as session:
            yield session

    async def _tenant(session=Depends(get_session)):
        return await session.get(Tenant, tenant.id)

    pool = FakeArqPool()
    app.dependency_overrides[get_session] = _session
    app.dependency_overrides[get_current_tenant] = _tenant
    # The role gate (TASK-020) is proved in tests/test_hub_payment_accounts_roles.py, through the
    # real dependency chain; in THIS file it is a pass so every test exercises the endpoint itself.
    app.dependency_overrides[require_clinic_manager] = lambda: None
    app.dependency_overrides[require_clinic_clinician] = lambda: None
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    monkeypatch.setenv("PAYMENTS_WEBHOOK_BASE_URL", BASE)
    get_settings.cache_clear()

    fake = FakeAsaasClient()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    verdict = {"value": ACCEPTED}

    async def probes(api_key):
        return verdict["value"]

    monkeypatch.setattr(asaas_connection, "run_probe_suite", probes)
    yield tenant, other, maker, pool, fake, verdict
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.dependency_overrides.pop(require_clinic_manager, None)
    app.dependency_overrides.pop(require_clinic_clinician, None)
    get_settings.cache_clear()
    await engine.dispose()


async def _put(client: AsyncClient, body):
    content = body if isinstance(body, (bytes, str)) else json.dumps(body)
    headers = {"content-type": "application/json"}
    return await client.put(CONNECTION, content=content, headers=headers)


async def test_a_good_key_connects_and_the_response_carries_only_the_status(client, world):
    tenant, _, maker, _, fake, _ = world
    response = await _put(client, {"api_key": KEY})

    assert response.status_code == 200
    assert response.json() == {
        "provider": "asaas",
        "status": "connected",
        "incomplete_reason": None,
        "capabilities": CAPS,
    }
    assert KEY not in response.text and "authToken" not in response.text
    async with maker() as session:
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        assert row.status == "connected" and row.api_key_encrypted != KEY


async def test_an_excess_permission_refuses_with_422_group_codes_and_pt_br_labels(client, world):
    tenant, _, maker, _, fake, verdict = world
    verdict["value"] = Verdict(VerdictKind.REFUSED, CODE_EXCESS, (), ("BILL", "TRANSFER"))
    response = await _put(client, {"api_key": KEY})

    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "key_excess_permissions",
            "missing_groups": [],
            "excess_groups": ["BILL", "TRANSFER"],
            "missing_labels": [],
            "excess_labels": ["Pagamento de contas", "Transferências"],
        }
    }
    assert KEY not in response.text
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []
    assert fake.calls == []


@pytest.mark.parametrize("code", [CODE_UNAVAILABLE, "probes_not_qualified"])
async def test_an_inconclusive_check_is_503_and_stores_nothing(client, world, code):
    _, _, maker, _, _, verdict = world
    verdict["value"] = Verdict(VerdictKind.INCONCLUSIVE, code)
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": code}}
    assert KEY not in response.text
    async with maker() as session:
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []


async def test_a_key_of_the_wrong_shape_is_a_clean_422(client, world):
    response = await _put(client, {"api_key": "$aact_hmlg_" + "a" * 40})  # sandbox key vs prod
    assert response.status_code == 422
    assert response.json() == {
        "detail": {
            "code": "key_invalid",
            "missing_groups": [],
            "excess_groups": [],
            "missing_labels": [],
            "excess_labels": [],
        }
    }


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": 12345},
        {},
        {"api_key": KEY, "extra": "field"},
        {"key": KEY},
        [KEY],
        "not json " + KEY,
        b"{broken " + KEY.encode(),
        {"api_key": KEY + "x" * 5000},
    ],
)
async def test_a_malformed_body_is_422_and_the_response_never_echoes_what_was_sent(
    client, world, body
):
    response = await _put(client, body)
    assert response.status_code == 422
    assert response.json() == {"detail": {"code": "invalid_body"}}
    assert KEY not in response.text and "x" * 50 not in response.text


async def test_the_eleventh_attempt_in_the_window_is_429(client, world):
    _, _, _, pool, _, verdict = world
    verdict["value"] = Verdict(VerdictKind.REFUSED, CODE_EXCESS, (), ("BILL",))
    statuses = [(await _put(client, {"api_key": KEY})).status_code for _ in range(11)]
    assert statuses == [422] * 10 + [429]
    assert list(pool.ttls.values()) == [600]


async def test_without_a_working_rate_limiter_the_endpoint_fails_closed(client, world, monkeypatch):
    from secretaria.main import app

    monkeypatch.setattr(app.state, "arq_pool", None, raising=False)
    response = await _put(client, {"api_key": KEY})
    assert response.status_code == 503
    assert response.json() == {"detail": {"code": "rate_limiter_unavailable"}}


async def test_listing_shows_status_only_and_never_a_secret(client, world):
    tenant, other, maker, _, _, _ = world
    await _put(client, {"api_key": KEY})
    async with maker() as session:
        legacy = await accounts.get_or_create_account_row(
            session, tenant_id=other.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(legacy, api_key=KEY + "legacy")
        legacy.status = "connected"  # another clinic: must never appear
        await session.commit()

    response = await client.get(ENDPOINT)
    body = response.json()
    assert response.status_code == 200 and len(body) == 1
    (item,) = body
    assert set(item) == {
        "provider", "status", "incomplete_reason", "capabilities", "excess_groups",
        "excess_labels", "connected_at", "last_verified_at", "ready_for_before",
    }
    assert item["provider"] == "asaas" and item["ready_for_before"] is True
    assert KEY not in response.text and "legacy" not in response.text


async def test_the_retry_endpoint_finishes_an_incomplete_connection_without_the_key(
    client, world, monkeypatch
):
    _, _, _, _, fake, _ = world
    fake.errors["create_webhook"] = AsaasError(500)
    first = await _put(client, {"api_key": KEY})
    assert first.json()["status"] == "incomplete"
    assert first.json()["incomplete_reason"] == "webhook_create_failed"

    fake.errors.clear()
    second = await client.post(f"{ENDPOINT}/asaas/retry-webhook")
    assert second.status_code == 200
    assert second.json()["status"] == "connected" and KEY not in second.text


async def test_retry_without_a_stored_key_is_422(client, world):
    response = await client.post(f"{ENDPOINT}/asaas/retry-webhook")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "no_key"


async def test_disconnect_revokes_the_account_and_a_second_call_is_404(client, world):
    tenant, _, maker, _, _, _ = world
    await _put(client, {"api_key": KEY})
    first = await client.delete(f"{ENDPOINT}/asaas")
    assert first.status_code == 200 and first.json()["status"] == "revoked"
    async with maker() as session:
        row = await accounts.get_account_row(session, tenant_id=tenant.id, provider="asaas")
        assert row.api_key_encrypted is None
    assert (await client.delete(f"{ENDPOINT}/asaas")).status_code == 404


async def test_the_endpoints_require_the_hub_token(client, world):
    from secretaria.main import app

    app.dependency_overrides.pop(get_current_tenant, None)
    for method, path in (("GET", ENDPOINT), ("PUT", CONNECTION), ("DELETE", f"{ENDPOINT}/asaas")):
        response = await client.request(method, path, json={"api_key": KEY})
        assert response.status_code == 401, (method, path)
        assert KEY not in response.text


async def test_the_paths_are_in_the_openapi_schema(client, world):
    schema = (await client.get("/openapi.json")).json()
    assert ENDPOINT in schema["paths"] and CONNECTION in schema["paths"]
    assert f"{ENDPOINT}/asaas/retry-webhook" in schema["paths"]
