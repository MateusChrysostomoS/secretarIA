"""Payment accounts: the single decrypt point, dual-read of the legacy Asaas columns, the
per-clinic slug lookup, the "usable for this timing" gate and the per-patient customer id."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
import pytest_asyncio

from secretaria.core.crypto import decrypt
from secretaria.models import Patient, Tenant
from secretaria.models.clinic_payment_account import (
    ACCOUNT_CONNECTED,
    ACCOUNT_INCOMPLETE,
    ACCOUNT_REVOKED,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts
from tests.payments_support import make_sessionmaker

KEY = "$aact_hmlg_key-secret-0123456789"
TOKEN = "webhook-token-secret-0123456789"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _tenant(session, **kw) -> Tenant:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], **kw)
    session.add(tenant)
    await session.flush()
    return tenant


async def test_secrets_are_encrypted_at_rest_and_decrypted_only_through_the_seam(db):
    async with db() as session:
        tenant = await _tenant(session)
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key=KEY, webhook_token=TOKEN)
        await session.commit()

        assert row.api_key_encrypted != KEY and KEY not in row.api_key_encrypted
        assert decrypt(row.api_key_encrypted) == KEY
        account = await cfg.get_payment_account(session, tenant.id, "asaas")

    assert (account.api_key, account.webhook_token) == (KEY, TOKEN)
    assert account.webhook_slug == row.webhook_slug
    assert KEY not in repr(account) and TOKEN not in repr(account)


async def test_set_only_writes_the_given_secrets_and_clear_wipes_them_all(db):
    async with db() as session:
        tenant = await _tenant(session)
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key=KEY)
        assert row.webhook_token_encrypted is None
        cfg.set_payment_account_secrets(row, webhook_token=TOKEN)
        assert decrypt(row.api_key_encrypted) == KEY  # untouched by the second call
        cfg.clear_payment_account_secrets(row)
        assert row.api_key_encrypted is None and row.webhook_token_encrypted is None


async def test_no_account_and_no_legacy_credentials_is_none(db):
    async with db() as session:
        tenant = await _tenant(session)
        assert await cfg.get_payment_account(session, tenant.id, "asaas") is None
        assert await cfg.get_payment_account(session, tenant.id, "mercadopago") is None
        assert await cfg.has_asaas_api_key(session, tenant.id) is False


async def test_legacy_asaas_columns_are_read_when_there_is_no_account_row(db):
    async with db() as session:
        tenant = await _tenant(session)
        await cfg.set_asaas_api_key(session, tenant.id, KEY)
        await cfg.set_asaas_webhook_token(session, tenant.id, TOKEN)
        await session.commit()
        account = await cfg.get_payment_account(session, tenant.id, "asaas")
        assert await cfg.has_asaas_api_key(session, tenant.id) is True
        assert await cfg.get_payment_account(session, tenant.id, "mercadopago") is None

    assert (account.api_key, account.webhook_token, account.webhook_slug) == (KEY, TOKEN, None)


async def test_has_asaas_api_key_is_true_for_an_account_row_alone(db):
    async with db() as session:
        tenant = await _tenant(session)
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key=KEY)
        await session.commit()
        assert await cfg.has_asaas_api_key(session, tenant.id) is True


async def test_get_or_create_is_idempotent_and_slugs_are_unique_opaque_uuids(db):
    async with db() as session:
        a, b = await _tenant(session), await _tenant(session)
        first = await accounts.get_or_create_account_row(session, tenant_id=a.id, provider="asaas")
        again = await accounts.get_or_create_account_row(session, tenant_id=a.id, provider="asaas")
        other = await accounts.get_or_create_account_row(session, tenant_id=b.id, provider="asaas")
    assert first.id == again.id
    assert first.webhook_slug != other.webhook_slug
    assert str(a.id) not in first.webhook_slug  # never derived from the tenant id
    assert len(first.webhook_slug) == 36


async def test_slug_lookup_is_scoped_by_provider(db):
    async with db() as session:
        tenant = await _tenant(session)
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        await session.commit()
        found = await accounts.get_account_by_slug(
            session, provider="asaas", webhook_slug=row.webhook_slug
        )
        wrong_provider = await accounts.get_account_by_slug(
            session, provider="mercadopago", webhook_slug=row.webhook_slug
        )
        unknown = await accounts.get_account_by_slug(
            session, provider="asaas", webhook_slug="does-not-exist"
        )
    assert found.tenant_id == tenant.id
    assert wrong_provider is None and unknown is None


@pytest.mark.parametrize(
    ("status", "verified", "after", "before"),
    [
        (ACCOUNT_CONNECTED, True, True, True),
        (ACCOUNT_CONNECTED, False, True, False),  # migrated legacy row: never verified
        (ACCOUNT_INCOMPLETE, True, True, False),  # e.g. excess permissions: no NEW `before`
        (ACCOUNT_REVOKED, True, False, False),
    ],
)
async def test_load_charge_account_gates_by_timing(db, status, verified, after, before):
    async with db() as session:
        tenant = await _tenant(session)
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key=KEY, webhook_token=TOKEN)
        row.status = status
        row.last_verified_at = datetime(2026, 9, 29, tzinfo=UTC) if verified else None
        await session.commit()
        got_after = await accounts.load_charge_account(
            session, tenant_id=tenant.id, provider="asaas", timing="after"
        )
        got_before = await accounts.load_charge_account(
            session, tenant_id=tenant.id, provider="asaas", timing="before"
        )
    assert (got_after is not None, got_before is not None) == (after, before)


async def test_a_legacy_only_tenant_can_charge_after_but_never_before(db):
    async with db() as session:
        tenant = await _tenant(session)
        await cfg.set_asaas_api_key(session, tenant.id, KEY)
        await session.commit()
        after = await accounts.load_charge_account(
            session, tenant_id=tenant.id, provider="asaas", timing="after"
        )
        before = await accounts.load_charge_account(
            session, tenant_id=tenant.id, provider="asaas", timing="before"
        )
    assert after is not None and after.api_key == KEY
    assert before is None


async def test_customer_id_roundtrip_legacy_fallback_and_tenant_isolation(db):
    async with db() as session:
        tenant, other = await _tenant(session), await _tenant(session)
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="M")
        legacy = Patient(
            id=uuid4(),
            tenant_id=tenant.id,
            wa_id="5511888888888",
            asaas_customer_id="cus_legacy",
        )
        session.add_all([patient, legacy])
        await session.flush()

        assert await accounts.get_customer_id(
            session, tenant_id=tenant.id, patient_id=patient.id, provider="asaas"
        ) is None
        await accounts.remember_customer_id(
            session,
            tenant_id=tenant.id,
            patient_id=patient.id,
            provider="asaas",
            provider_customer_id="cus_1",
        )
        await accounts.remember_customer_id(  # an upsert, not a second row
            session,
            tenant_id=tenant.id,
            patient_id=patient.id,
            provider="asaas",
            provider_customer_id="cus_2",
        )
        await session.flush()
        assert await accounts.get_customer_id(
            session, tenant_id=tenant.id, patient_id=patient.id, provider="asaas"
        ) == "cus_2"
        assert await accounts.get_customer_id(
            session, tenant_id=tenant.id, patient_id=legacy.id, provider="asaas"
        ) == "cus_legacy"
        assert await accounts.get_customer_id(
            session, tenant_id=tenant.id, patient_id=legacy.id, provider="mercadopago"
        ) is None
        assert await accounts.get_customer_id(
            session, tenant_id=other.id, patient_id=patient.id, provider="asaas"
        ) is None
