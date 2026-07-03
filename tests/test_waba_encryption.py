"""WABA-token encryption at rest (tenant-secrets-encryption skill).

Covers the four properties the round must guarantee:
1. The Tenant model carries NO plaintext secret column at all.
2. The credential helpers round-trip through Fernet ciphertext — what persists is
   ciphertext, what returns is the original value, decrypted at the single seam.
3. WhatsAppClient receives the token by INJECTION (caller decrypts via the seam);
   `None` falls back to the single-tenant META_ACCESS_TOKEN env scaffold.
4. The structlog redactor blanks secret-bearing keys before any renderer runs.

Uses in-memory aiosqlite; ENCRYPTION_KEY comes from conftest's deterministic env.
"""

from uuid import uuid4

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.core.database import Base
from secretaria.core.logging import redact_secrets
from secretaria.models import Tenant
from secretaria.models.tenant_credentials import TenantCredentials
from secretaria.services.tenant_config import (
    clear_waba_token,
    get_waba_token,
    has_waba_token,
    set_waba_token,
)
from secretaria.services.whatsapp import WhatsAppClient

SECRET = "EAAG-super-secret-waba-token"


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _make_tenant(session) -> Tenant:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    session.add(tenant)
    await session.flush()
    return tenant


def test_tenant_model_has_no_plaintext_secret_column():
    """The load-bearing guarantee: no `access_token` (or any token) column on tenants."""
    assert "access_token" not in Tenant.__table__.columns
    secretish = [c for c in Tenant.__table__.columns.keys() if "token" in c or "secret" in c]
    assert secretish == []


async def test_waba_token_roundtrip_is_ciphertext_at_rest(session):
    tenant = await _make_tenant(session)
    assert await has_waba_token(session, tenant.id) is False
    assert await get_waba_token(session, tenant.id) is None

    await set_waba_token(session, tenant.id, SECRET)
    await session.commit()

    # What PERSISTS is Fernet ciphertext, never the plaintext.
    cred = await session.scalar(
        select(TenantCredentials).where(TenantCredentials.tenant_id == tenant.id)
    )
    assert cred.waba_token_encrypted != SECRET
    assert SECRET not in cred.waba_token_encrypted
    # The single seam returns the original value.
    assert await has_waba_token(session, tenant.id) is True
    assert await get_waba_token(session, tenant.id) == SECRET

    await clear_waba_token(session, tenant.id)
    await session.commit()
    assert await get_waba_token(session, tenant.id) is None


async def test_waba_upsert_shares_row_with_google_credential(session):
    """One credentials row per tenant: setting the WABA token must not duplicate it."""
    from secretaria.services.tenant_config import set_google_refresh_token

    tenant = await _make_tenant(session)
    await set_google_refresh_token(session, tenant.id, "g-refresh")
    await set_waba_token(session, tenant.id, SECRET)
    await session.commit()

    rows = (
        await session.scalars(
            select(TenantCredentials).where(TenantCredentials.tenant_id == tenant.id)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].google_refresh_token_encrypted is not None
    assert rows[0].waba_token_encrypted is not None


async def test_whatsapp_client_token_is_injected(session):
    tenant = await _make_tenant(session)
    await set_waba_token(session, tenant.id, SECRET)
    await session.commit()

    token = await get_waba_token(session, tenant.id)
    client = WhatsAppClient.for_tenant(tenant, token)
    assert client._access_token == SECRET
    assert client._phone_number_id == tenant.phone_number_id

    # None -> single-tenant env scaffold (conftest sets META_ACCESS_TOKEN).
    fallback = WhatsAppClient.for_tenant(tenant, None)
    assert fallback._access_token == "test-access-token"


def test_redactor_blanks_secret_bearing_keys():
    event = redact_secrets(
        None,
        "info",
        {
            "event": "calendar_connected",  # the event NAME survives
            "tenant_id": "abc",
            "waba_token_encrypted": "ciphertext",
            "access_token": SECRET,
            "refresh_token": "r",
            "authorization": "Bearer x",
            "api_key": "k",
            "count": 3,
        },
    )
    assert event["event"] == "calendar_connected"
    assert event["tenant_id"] == "abc"
    assert event["count"] == 3
    redacted_keys = (
        "waba_token_encrypted",
        "access_token",
        "refresh_token",
        "authorization",
        "api_key",
    )
    for key in redacted_keys:
        assert event[key] == "***REDACTED***"
    assert SECRET not in str(event)
