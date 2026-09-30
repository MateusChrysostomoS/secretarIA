"""LGPD for payments (spec section 14): erasure anonymises deposits and asks the provider to
delete the remote customer; export includes the subject's deposits; retention keeps the money
rows (accounting) but never the payer's identity."""

from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.core.database import get_session
from secretaria.models import (
    Patient,
    PatientPaymentCustomer,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, asaas_provider, registry
from secretaria.services.payments.asaas import AsaasError
from secretaria.services.payments.asaas_provider import AsaasProvider
from secretaria.services.payments.provider import PaymentAccount, ProviderError
from secretaria.workers import payments_tasks
from tests.payments_support import FakeArqPool, FakeAsaasClient, FakeProvider, make_sessionmaker

HEADERS = {"X-Internal-Api-Key": "test-internal-key"}
WA_ID = "5511900000009"


@pytest_asyncio.fixture
async def world(monkeypatch):
    import os

    os.environ["INTERNAL_API_KEY"] = "test-internal-key"
    from secretaria.config import get_settings
    from secretaria.main import app

    get_settings.cache_clear()
    engine, maker = await make_sessionmaker()

    async def _session():
        async with maker() as session:
            yield session

    pool = FakeArqPool()
    app.dependency_overrides[get_session] = _session
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    yield maker, pool
    app.dependency_overrides.pop(get_session, None)
    await engine.dispose()


async def _seed(maker, *, legacy_customer=None, second_provider=False):
    async with maker() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        other = Tenant(id=uuid4(), clinic_name="Other", phone_number_id=str(uuid4())[:12])
        session.add_all([tenant, other])
        await session.flush()
        patient = Patient(
            id=uuid4(), tenant_id=tenant.id, wa_id=WA_ID, name="Maria",
            asaas_customer_id=legacy_customer,
        )
        bystander = Patient(id=uuid4(), tenant_id=other.id, wa_id=WA_ID, name="Homonym")
        session.add_all([patient, bystander])
        await session.flush()
        session.add(
            PatientPaymentCustomer(
                tenant_id=tenant.id, patient_id=patient.id, provider="asaas",
                provider_customer_id="cus_mine",
            )
        )
        session.add(
            PatientPaymentCustomer(
                tenant_id=other.id, patient_id=bystander.id, provider="asaas",
                provider_customer_id="cus_theirs",
            )
        )
        deposit = PixDeposit(
            tenant_id=tenant.id, patient_id=patient.id, amount_cents=6000, percent_applied=30,
            status=PixDepositStatus.PAID, provider="asaas", provider_charge_id="pay_1",
            asaas_payment_id="pay_1", pix_copy_paste="00020126SECRETPIX",
        )
        session.add(deposit)
        await session.flush()
        session.add(
            PaymentAttentionItem(
                tenant_id=tenant.id, deposit_id=deposit.id, patient_id=patient.id,
                provider="asaas", reason="amount_under", dedupe_key="asaas:pay_1:amount_under",
            )
        )
        await session.commit()
        return tenant, other, patient, bystander, deposit


async def test_erasure_anonymises_deposits_and_items_removes_the_mapping_and_asks_the_provider(
    client: AsyncClient, world
):
    maker, pool = world
    tenant, other, patient, bystander, deposit = await _seed(maker, legacy_customer="cus_legacy")

    response = await client.delete(
        f"/internal/privacy/tenants/{tenant.id}/subjects/{WA_ID}", headers=HEADERS
    )
    assert response.status_code == 200
    erased = response.json()["erased"]
    assert erased["pix_deposits"] == 1
    assert erased["payment_attention_items"] == 1
    assert erased["payment_customers"] == 2  # the mapping row + the legacy column, both ids

    async with maker() as session:
        kept = await session.get(PixDeposit, deposit.id)
        item = await session.scalar(select(PaymentAttentionItem))
        mappings = (await session.scalars(select(PatientPaymentCustomer))).all()
        homonym = await session.get(Patient, bystander.id)
    assert kept is not None and kept.patient_id is None  # accounting row kept, identity gone
    assert (kept.amount_cents, kept.status) == (6000, PixDepositStatus.PAID)
    assert item is not None and item.patient_id is None
    assert [m.provider_customer_id for m in mappings] == ["cus_theirs"]  # another clinic: untouched
    assert homonym is not None

    jobs = sorted(job[1] for job in pool.jobs if job[0] == "delete_provider_customer_job")
    assert jobs == [
        (str(tenant.id), "asaas", "cus_legacy"),
        (str(tenant.id), "asaas", "cus_mine"),
    ]
    flat = repr(pool.jobs)
    assert WA_ID not in flat and "Maria" not in flat  # ids only


async def test_the_same_customer_id_in_both_places_is_asked_for_once(client, world):
    maker, pool = world
    tenant, *_ = await _seed(maker, legacy_customer="cus_mine")
    await client.delete(f"/internal/privacy/tenants/{tenant.id}/subjects/{WA_ID}", headers=HEADERS)
    assert [job[1] for job in pool.jobs] == [(str(tenant.id), "asaas", "cus_mine")]


async def test_a_queue_outage_never_blocks_the_local_erasure(client, world, monkeypatch):
    from secretaria.main import app

    maker, _ = world
    monkeypatch.setattr(app.state, "arq_pool", FakeArqPool(fail_enqueue=True), raising=False)
    tenant, _, patient, *_ = await _seed(maker)
    response = await client.delete(
        f"/internal/privacy/tenants/{tenant.id}/subjects/{WA_ID}", headers=HEADERS
    )
    assert response.status_code == 200 and response.json()["erased"]["patients"] == 1
    async with maker() as session:
        assert await session.get(Patient, patient.id) is None


async def test_a_second_erasure_is_all_zeros_and_enqueues_nothing(client, world):
    maker, pool = world
    tenant, *_ = await _seed(maker)
    await client.delete(f"/internal/privacy/tenants/{tenant.id}/subjects/{WA_ID}", headers=HEADERS)
    pool.jobs.clear()
    again = await client.delete(
        f"/internal/privacy/tenants/{tenant.id}/subjects/{WA_ID}", headers=HEADERS
    )
    assert set(again.json()["erased"].values()) == {0}
    assert pool.jobs == []


async def test_export_includes_the_subjects_deposits_without_provider_ids_or_pix_codes(
    client: AsyncClient, world
):
    maker, _ = world
    tenant, other, patient, bystander, deposit = await _seed(maker)
    response = await client.get(
        f"/internal/privacy/tenants/{tenant.id}/subjects/{WA_ID}/export", headers=HEADERS
    )
    body = response.json()
    assert response.status_code == 200
    (item,) = body["pix_deposits"]
    assert set(item) == {
        "id", "appointment_id", "provider", "amount_cents", "status", "paid_at", "created_at",
    }
    assert item["amount_cents"] == 6000 and item["provider"] == "asaas"
    assert item["status"] == "confirmado_pago"
    assert "SECRETPIX" not in response.text and "pay_1" not in response.text


async def test_the_empty_export_has_an_empty_deposit_list(client: AsyncClient, world):
    maker, _ = world
    tenant, *_ = await _seed(maker)
    response = await client.get(
        f"/internal/privacy/tenants/{tenant.id}/subjects/5500000000000/export", headers=HEADERS
    )
    assert response.json()["pix_deposits"] == []


# --------------------------------------------------------------------------
# the remote deletion job
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(payments_tasks, "async_session_factory", maker)
    yield maker
    await engine.dispose()


async def _account(maker, *, connected=True) -> Tenant:
    async with maker() as session:
        tenant = Tenant(id=uuid4(), clinic_name="C", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        if connected:
            row = await accounts.get_or_create_account_row(
                session, tenant_id=tenant.id, provider="asaas"
            )
            cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        await session.commit()
        return tenant


async def test_the_job_deletes_the_customer_with_that_clinics_own_account(db, monkeypatch):
    seen = []

    class Recording(FakeProvider):
        async def delete_customer(self, account, provider_customer_id):
            seen.append((account.tenant_id, provider_customer_id))

    monkeypatch.setitem(registry._PROVIDERS, "asaas", Recording())
    tenant = await _account(db)
    await payments_tasks.delete_provider_customer_job({}, str(tenant.id), "asaas", "cus_1")
    assert seen == [(tenant.id, "cus_1")]


async def test_the_job_skips_quietly_without_an_account_or_a_provider_that_cannot_delete(
    db, monkeypatch
):
    monkeypatch.setitem(registry._PROVIDERS, "asaas", FakeProvider())  # has no delete_customer
    connected = await _account(db)
    await payments_tasks.delete_provider_customer_job({}, str(connected.id), "asaas", "cus_1")
    disconnected = await _account(db, connected=False)
    await payments_tasks.delete_provider_customer_job({}, str(disconnected.id), "asaas", "cus_1")
    await payments_tasks.delete_provider_customer_job({}, str(uuid4()), "nope", "cus_1")


async def test_a_retryable_provider_failure_propagates_for_the_arq_retry(db, monkeypatch):
    class Failing(FakeProvider):
        async def delete_customer(self, account, provider_customer_id):
            raise ProviderError("delete_customer_failed", status_code=503, retryable=True)

    monkeypatch.setitem(registry._PROVIDERS, "asaas", Failing())
    tenant = await _account(db)
    with pytest.raises(ProviderError):
        await payments_tasks.delete_provider_customer_job({}, str(tenant.id), "asaas", "cus_1")


async def test_asaas_delete_customer_is_idempotent_and_maps_errors(monkeypatch):
    account = PaymentAccount(tenant_id=uuid4(), provider="asaas", api_key="k")
    fake = FakeAsaasClient()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    await AsaasProvider().delete_customer(account, "cus_1")
    assert ("delete_customer", "cus_1") in fake.calls

    fake.errors["delete_customer"] = AsaasError(404)
    await AsaasProvider().delete_customer(account, "cus_1")  # already gone: success

    fake.errors["delete_customer"] = AsaasError(500)
    with pytest.raises(ProviderError) as failure:
        await AsaasProvider().delete_customer(account, "cus_1")
    assert failure.value.retryable is True
