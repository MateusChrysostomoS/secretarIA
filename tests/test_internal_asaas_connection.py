"""Tests for POST /internal/tenants/{tenant_id}/asaas-connection.

Mirrors tests/test_internal_provisioning.py's whatsapp-connection coverage (auth guard, 404,
encrypted-at-rest round trip, no echo). Since TASK-016 the endpoint runs the SAME connection
service as the clinic's own screen: the key is format-checked and permission-probed before it
is stored, and the clinic's webhook is created and read back. The probes and the Asaas client
are faked here; their own suites are test_asaas_probes.py / test_asaas_connection.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import ClinicPaymentAccount, Tenant  # noqa: E402
from secretaria.models.tenant_credentials import TenantCredentials  # noqa: E402
from secretaria.services.payments import asaas_connection, asaas_provider  # noqa: E402
from secretaria.services.payments.asaas_probes import (  # noqa: E402
    CODE_EXCESS,
    CODE_UNAVAILABLE,
    Verdict,
    VerdictKind,
)
from secretaria.services.tenant_config import (  # noqa: E402
    get_asaas_api_key,
    get_asaas_webhook_token,
    has_asaas_api_key,
)
from tests.payments_support import FakeAsaasClient  # noqa: E402

GOOD_KEY = "test-internal-key"
HEADERS = {"X-Internal-Api-Key": GOOD_KEY}
API_KEY = "$aact_prod_0123456789abcdef0123456789abcdef0123456789"
WEBHOOK_TOKEN = "whsec-token-0123456789-0123456789-0123456789"
VALID_BODY = {"api_key": API_KEY, "webhook_token": WEBHOOK_TOKEN}
ACCEPTED = Verdict(
    VerdictKind.ACCEPTED, None, (), (), {"customers": True, "payments": True, "webhooks": True}
)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _override_session(db):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _fake_get_session
    yield
    app.dependency_overrides.pop(get_session, None)


@pytest.fixture(autouse=True)
def _asaas(monkeypatch):
    from secretaria.config import get_settings

    monkeypatch.setenv("PAYMENTS_WEBHOOK_BASE_URL", "https://secretaria.example.test")
    get_settings.cache_clear()
    fake = FakeAsaasClient()
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    verdict = {"value": ACCEPTED}

    async def probes(api_key):
        return verdict["value"]

    monkeypatch.setattr(asaas_connection, "run_probe_suite", probes)
    yield verdict
    get_settings.cache_clear()


async def _seed_tenant(db, **overrides) -> Tenant:
    fields = dict(id=uuid4(), clinic_name="Clinic", phone_number_id=None, is_active=False)
    fields.update(overrides)
    async with db() as session:
        tenant = Tenant(**fields)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


# --------------------------------------------------------------------------
# Auth guard — mirrors test_internal_provisioning.py
# --------------------------------------------------------------------------


async def test_missing_key_is_unauthorized(client: AsyncClient) -> None:
    response = await client.post(
        f"/internal/tenants/{uuid4()}/asaas-connection", json=VALID_BODY
    )
    assert response.status_code == 401


async def test_wrong_key_is_unauthorized(client: AsyncClient) -> None:
    response = await client.post(
        f"/internal/tenants/{uuid4()}/asaas-connection",
        json=VALID_BODY,
        headers={"X-Internal-Api-Key": "wrong-key"},
    )
    assert response.status_code == 401


async def test_unconfigured_key_is_forbidden(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.config import get_settings

    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = await client.post(
            f"/internal/tenants/{uuid4()}/asaas-connection",
            json=VALID_BODY,
            headers={"X-Internal-Api-Key": "anything"},
        )
        assert response.status_code == 403
    finally:
        monkeypatch.setenv("INTERNAL_API_KEY", GOOD_KEY)
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# 404 unknown tenant
# --------------------------------------------------------------------------


async def test_unknown_tenant_is_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/internal/tenants/{uuid4()}/asaas-connection", json=VALID_BODY, headers=HEADERS
    )
    assert response.status_code == 404


# --------------------------------------------------------------------------
# The key is checked BEFORE it is stored
# --------------------------------------------------------------------------


async def test_a_key_with_excess_permissions_is_refused_422_and_never_stored(
    client: AsyncClient, db, _asaas
) -> None:
    _asaas["value"] = Verdict(VerdictKind.REFUSED, CODE_EXCESS, (), ("TRANSFER",))
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection", json=VALID_BODY, headers=HEADERS
    )
    assert response.status_code == 422
    assert response.json()["detail"] == {
        "code": "key_excess_permissions",
        "missing_groups": [],
        "excess_groups": ["TRANSFER"],
    }
    assert API_KEY not in response.text and WEBHOOK_TOKEN not in response.text
    async with db() as session:
        assert await has_asaas_api_key(session, tenant.id) is False
        assert (await session.scalars(select(ClinicPaymentAccount))).all() == []


async def test_an_inconclusive_verification_is_503_and_stores_nothing(
    client: AsyncClient, db, _asaas
) -> None:
    _asaas["value"] = Verdict(VerdictKind.INCONCLUSIVE, CODE_UNAVAILABLE)
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection", json=VALID_BODY, headers=HEADERS
    )
    assert response.status_code == 503
    async with db() as session:
        assert await has_asaas_api_key(session, tenant.id) is False


@pytest.mark.parametrize(
    "body",
    [
        {"api_key": "short-secret-value", "webhook_token": WEBHOOK_TOKEN},
        {"api_key": API_KEY, "webhook_token": "too-short-token"},
    ],
)
async def test_a_badly_shaped_key_or_token_is_refused_without_echoing_it(
    client: AsyncClient, db, body
) -> None:
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection", json=body, headers=HEADERS
    )
    assert response.status_code == 422
    for secret in body.values():
        assert secret not in response.text


# --------------------------------------------------------------------------
# Happy path: stores both encrypted, never echoes, round-trips
# --------------------------------------------------------------------------


async def test_stores_both_encrypted_never_echoes_and_round_trips(
    client: AsyncClient, db
) -> None:
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection", json=VALID_BODY, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "asaas_connected": True}
    assert API_KEY not in response.text
    assert WEBHOOK_TOKEN not in response.text

    async with db() as session:
        cred = await session.scalar(
            select(TenantCredentials).where(TenantCredentials.tenant_id == tenant.id)
        )
        assert cred is not None
        # What PERSISTS is Fernet ciphertext, never the plaintext.
        assert cred.asaas_api_key_encrypted != API_KEY
        assert API_KEY not in cred.asaas_api_key_encrypted
        assert cred.asaas_webhook_token_encrypted != WEBHOOK_TOKEN
        assert WEBHOOK_TOKEN not in cred.asaas_webhook_token_encrypted

        # The single decrypt seam returns the original values (legacy dual-write)...
        assert await has_asaas_api_key(session, tenant.id) is True
        assert await get_asaas_api_key(session, tenant.id) == API_KEY
        assert await get_asaas_webhook_token(session, tenant.id) == WEBHOOK_TOKEN
        # ...and the per-provider account carries the same, connected, with its webhook.
        account = await session.scalar(select(ClinicPaymentAccount))
        assert (account.status, account.external_webhook_id) == ("connected", "wh_fake")


async def test_the_webhook_token_is_optional_and_generated_when_absent(
    client: AsyncClient, db
) -> None:
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection",
        json={"api_key": API_KEY},
        headers=HEADERS,
    )
    assert response.status_code == 200
    async with db() as session:
        token = await get_asaas_webhook_token(session, tenant.id)
    assert token is not None and 32 <= len(token) <= 255


async def test_a_webhook_that_cannot_be_registered_is_reported_incomplete(
    client: AsyncClient, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.services.payments.asaas import AsaasError

    fake = FakeAsaasClient(errors={"create_webhook": AsaasError(500)})
    monkeypatch.setattr(asaas_provider, "client_for", lambda api_key: fake)
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection", json=VALID_BODY, headers=HEADERS
    )
    assert response.status_code == 200
    assert response.json() == {"status": "incomplete", "asaas_connected": True}


async def test_reconnect_upserts_same_credentials_row(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)
    await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection", json=VALID_BODY, headers=HEADERS
    )
    second_key = "$aact_prod_ffffffffffffffffffffffffffffffffffffffffff"
    second_token = "whsec-second-token-0123456789-0123456789-01234"
    response = await client.post(
        f"/internal/tenants/{tenant.id}/asaas-connection",
        json={"api_key": second_key, "webhook_token": second_token},
        headers=HEADERS,
    )
    assert response.status_code == 200

    async with db() as session:
        rows = (
            await session.scalars(
                select(TenantCredentials).where(TenantCredentials.tenant_id == tenant.id)
            )
        ).all()
        assert len(rows) == 1  # upsert, not a second row
        assert len((await session.scalars(select(ClinicPaymentAccount))).all()) == 1
        assert await get_asaas_api_key(session, tenant.id) == second_key
        assert await get_asaas_webhook_token(session, tenant.id) == second_token
