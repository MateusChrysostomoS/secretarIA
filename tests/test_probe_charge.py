"""The clinic's "Testar cobrança": a real Pix charge that is voided right away (TASK-071, Plan 4).

The charge goes through the SAME provider path as a patient's, so a success is real proof. Whatever
happens, the test charge and the test customer are removed; the result is written with the same
vocabulary as a patient's failure; the document typed for the test is used in memory and never
stored or logged; only a manager may press the button, and only one test runs at a time.
"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
import structlog

from secretaria.api.hub import deps as hub_deps
from secretaria.core.database import get_session
from secretaria.core.subscription import SubscriptionClaim
from secretaria.models import ClinicPaymentAccount, Tenant
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, probe_charge, registry
from secretaria.services.payments.failure_reason import FailureReason
from secretaria.services.payments.provider import CreatedCharge, ProviderError
from tests.payments_support import FakeArqPool, FakeProvider, FakeRedis, make_sessionmaker

CPF = "52998224725"  # synthetic, valid check digits
ENDPOINT = "/tenants/me/payment-accounts/asaas/test-charge"
HEADERS = {"authorization": "Bearer role-test-token", "content-type": "application/json"}


def _created(code="000201probe6304ABCD") -> CreatedCharge:
    return CreatedCharge(
        provider_charge_id="pay_probe",
        copy_paste=code,
        provider_customer_id="cus_probe",
        customer_created_here=True,
    )


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider()
    fake.created_charge = _created()
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _seed(session):
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    session.add(tenant)
    await session.flush()
    row = await accounts.get_or_create_account_row(session, tenant_id=tenant.id, provider="asaas")
    cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
    row.status = "connected"
    row.last_verified_at = datetime.now(UTC)
    await session.commit()
    return tenant


async def _run(session, tenant, redis=None, document=CPF):
    return await probe_charge.run_probe_charge(
        session, tenant_id=tenant.id, payer_document=document, redis=redis or FakeRedis()
    )


async def _row(session, tenant) -> ClinicPaymentAccount:
    tenant_id = tenant.id  # read before expire_all(): an expired tenant would lazy-load
    session.expire_all()
    return await accounts.get_account_row(session, tenant_id=tenant_id, provider="asaas")


async def test_probe_success_marks_health_ok_and_deletes_everything(db, provider):
    async with db() as session:
        tenant = await _seed(session)
        result = await _run(session, tenant)
        row = await _row(session, tenant)

    assert (result.ok, result.reason, result.stage, result.cleaned) == (True, None, None, True)
    assert accounts.charge_health(row) == "ok"
    assert provider.called("create_pix_charge") == 1
    assert ("cancel_charge", "pay_probe") in provider.calls
    assert ("delete_customer", "cus_probe") in provider.calls
    request = provider.calls[0][1]
    assert request.amount_cents == probe_charge.PROBE_AMOUNT_CENTS == 500
    assert request.external_reference.startswith("probe-")  # never a patient's deposit reference


async def test_probe_failure_stores_reason_and_still_cleans_up(db, provider):
    """The charge was created but Asaas gave no Pix code (no Pix key): reason + stage recorded,
    and the charge and the customer are removed all the same."""
    provider.created_charge = _created(code=None)
    async with db() as session:
        tenant = await _seed(session)
        result = await _run(session, tenant)
        row = await _row(session, tenant)

    assert (result.ok, result.reason, result.stage) == (False, FailureReason.qr_unavailable, "qr")
    assert result.cleaned is True
    assert accounts.charge_health(row) == "failing"
    assert row.last_charge_failure_reason == "qr_unavailable"
    assert row.last_charge_failure_stage == "qr"
    assert ("cancel_charge", "pay_probe") in provider.calls
    assert ("delete_customer", "cus_probe") in provider.calls


async def test_probe_provider_refusal_is_recorded_with_the_provider_reason(db, provider):
    provider.create_error = ProviderError(
        "charge_failed", status_code=400, reason=FailureReason.pix_key_missing, stage="charge"
    )
    async with db() as session:
        tenant = await _seed(session)
        result = await _run(session, tenant)
        row = await _row(session, tenant)

    assert (result.ok, result.reason, result.stage) == (
        False, FailureReason.pix_key_missing, "charge",
    )
    assert row.last_charge_failure_reason == "pix_key_missing"
    assert provider.called("cancel_charge") == 0  # nothing was created


async def test_probe_cleanup_failure_is_reported_and_does_not_hide_the_result(db, provider):
    provider.cancel_error = ProviderError("cancel_failed", status_code=500)
    async with db() as session:
        tenant = await _seed(session)
        result = await _run(session, tenant)
        row = await _row(session, tenant)

    assert result.ok is True and result.cleaned is False
    assert accounts.charge_health(row) == "ok"
    assert ("delete_customer", "cus_probe") in provider.calls  # still tried the customer


async def test_probe_second_click_is_rejected(db, provider):
    redis = FakeRedis()
    async with db() as session:
        tenant = await _seed(session)
        first = await _run(session, tenant, redis=redis)
        second = await _run(session, tenant, redis=redis)

    assert first.ok is True
    assert (second.ok, second.busy) == (False, True)
    assert provider.called("create_pix_charge") == 1  # the second click never reached Asaas
    assert redis.ttls[f"probe_charge:{tenant.id}"] == 60


async def test_probe_lock_is_per_clinic(db, provider):
    redis = FakeRedis()
    provider.created_charge = [_created(), _created()]
    async with db() as session:
        first = await _seed(session)
        other = await _seed(session)
        assert (await _run(session, first, redis=redis)).ok
        assert (await _run(session, other, redis=redis)).ok


async def test_probe_without_a_valid_document_never_calls_asaas(db, provider):
    async with db() as session:
        tenant = await _seed(session)
        result = await _run(session, tenant, document="123")
        row = await _row(session, tenant)

    assert (result.ok, result.reason, result.stage) == (
        False, FailureReason.customer_document_invalid, "input",
    )
    assert provider.calls == []
    assert row.last_charge_failure_reason is None  # a typo of the manager is not an account failure


async def test_probe_without_an_account(db, provider):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.commit()
        result = await _run(session, tenant)
    assert (result.ok, result.reason) == (False, FailureReason.no_account)
    assert provider.calls == []


async def test_probe_never_stores_or_logs_the_document(db, provider):
    provider.created_charge = _created(code=None)  # a failing run logs more
    with structlog.testing.capture_logs() as logs:
        async with db() as session:
            tenant = await _seed(session)
            result = await _run(session, tenant)
            row = await _row(session, tenant)
    assert CPF not in json.dumps(logs, default=str)
    assert CPF not in repr(result)
    columns = {c.name: getattr(row, c.name) for c in ClinicPaymentAccount.__table__.columns}
    assert CPF not in json.dumps(columns, default=str)


# --- the endpoint: manager only ------------------------------------------------------------


@pytest_asyncio.fixture
async def world(monkeypatch, provider):
    from secretaria.main import app

    engine, maker = await make_sessionmaker()
    async with maker() as session:
        tenant = await _seed(session)

    async def _session():
        async with maker() as session:
            yield session

    pool = FakeArqPool()
    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    role = {"value": "manager"}

    async def _verify(token, *, fresh=False):
        return SubscriptionClaim(tenant_id=tenant.id, active=True, role=role["value"])

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _verify)
    yield tenant, maker, pool, role
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


async def test_probe_requires_manager_role(client, world, provider):
    tenant, maker, pool, role = world
    for value in ("secretary", "doctor", None):
        role["value"] = value
        response = await client.post(
            ENDPOINT, content=json.dumps({"document": CPF}), headers=HEADERS
        )
        assert response.status_code == 403, value
        assert CPF not in response.text
    assert provider.calls == [] and pool.store == {}


async def test_the_manager_runs_the_probe_through_the_endpoint(client, world, provider):
    tenant, maker, pool, role = world
    response = await client.post(ENDPOINT, content=json.dumps({"document": CPF}), headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True and body["cleaned"] is True and body["busy"] is False
    assert CPF not in response.text
    again = await client.post(ENDPOINT, content=json.dumps({"document": CPF}), headers=HEADERS)
    assert again.status_code == 429 and again.json()["detail"]["code"] == "probe_in_progress"


async def test_a_malformed_body_never_echoes_the_document(client, world, provider):
    response = await client.post(
        ENDPOINT, content=json.dumps({"document": CPF, "extra": 1}), headers=HEADERS
    )
    assert response.status_code == 422 and CPF not in response.text
    assert provider.calls == []
