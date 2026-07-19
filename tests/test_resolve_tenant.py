"""workers/tasks.py:_resolve_tenant - nullable phone_number_id + autoprovision gate.

Covers the null-safety sweep for the now-nullable `tenants.phone_number_id`
(cross-service contract v1 §3): a NULL-phone tenant (e.g. provisioned by
onboarding but not yet WhatsApp-connected) must NEVER be matched or adopted
by inbound webhook routing. It also covers the new
`settings.ALLOW_WEBHOOK_AUTOPROVISION` gate (default False) around the MVP
single-tenant scaffold (configured-number fallback + auto-create) that used
to run unconditionally.

Uses the in-memory-sqlite pattern shared by test_handover_echoes.py /
test_hub_professionals.py: a real aiosqlite engine (StaticPool), monkeypatched
in place of the Postgres-backed `secretaria.workers.tasks.async_session_factory`.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import get_settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

# Matches META_PHONE_NUMBER_ID forced above (and in conftest.py) - the
# single-tenant scaffold's "configured" number.
CONFIGURED_PHONE = "1234567890"


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
def _wire_db(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


async def _seed_tenant(db, **overrides) -> Tenant:
    fields = dict(
        id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], is_active=True
    )
    fields.update(overrides)
    async with db() as session:
        tenant = Tenant(**fields)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _all_tenants(db) -> list[Tenant]:
    async with db() as session:
        return list((await session.scalars(select(Tenant))).all())


# --------------------------------------------------------------------------
# Primary (exact-match) path - always on, NULL-safe regardless of the flag
# --------------------------------------------------------------------------


async def test_exact_match_resolves_the_right_tenant(db):
    tenant = await _seed_tenant(db, phone_number_id="known-number")
    async with db() as session:
        resolved = await tasks._resolve_tenant(session, "known-number")
    assert resolved is not None
    assert resolved.id == tenant.id


async def test_null_phone_tenant_never_matches_any_inbound_number(db):
    """A NULL-phone tenant (mid-onboarding) sits in the DB - never adopted."""
    await _seed_tenant(db, phone_number_id=None)

    async with db() as session:
        resolved = await tasks._resolve_tenant(session, "some-inbound-number")

    assert resolved is None


async def test_null_phone_tenant_never_matched_by_a_null_incoming_id(db):
    """Even an event with no phone_number_id at all must not match a NULL row."""
    await _seed_tenant(db, phone_number_id=None)

    async with db() as session:
        resolved = await tasks._resolve_tenant(session, None)

    assert resolved is None


async def test_multiple_null_phone_tenants_coexist_and_none_match(db):
    await _seed_tenant(db, phone_number_id=None)
    await _seed_tenant(db, phone_number_id=None)
    real = await _seed_tenant(db, phone_number_id="real-number")

    async with db() as session:
        resolved = await tasks._resolve_tenant(session, "real-number")
    assert resolved is not None
    assert resolved.id == real.id

    async with db() as session:
        resolved_unknown = await tasks._resolve_tenant(session, "totally-unknown")
    assert resolved_unknown is None


# --------------------------------------------------------------------------
# Autoprovision fallback - OFF by default, opt-in only
# --------------------------------------------------------------------------


async def test_autoprovision_flag_defaults_off():
    assert get_settings().ALLOW_WEBHOOK_AUTOPROVISION is False


async def test_unknown_number_returns_none_when_autoprovision_off(db):
    """Regression guard: this exact call used to silently auto-create a tenant."""
    async with db() as session:
        resolved = await tasks._resolve_tenant(session, CONFIGURED_PHONE)

    assert resolved is None
    assert await _all_tenants(db) == []


async def test_no_phone_number_id_returns_none_when_autoprovision_off(db):
    """No metadata.phone_number_id on the event at all - still dropped, not adopted."""
    async with db() as session:
        resolved = await tasks._resolve_tenant(session, None)

    assert resolved is None
    assert await _all_tenants(db) == []


async def test_unknown_number_autoprovisions_when_flag_on(db, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "ALLOW_WEBHOOK_AUTOPROVISION", True)

    async with db() as session:
        resolved = await tasks._resolve_tenant(session, CONFIGURED_PHONE)

    assert resolved is not None
    assert resolved.phone_number_id == CONFIGURED_PHONE
    assert resolved.is_active is True


async def test_autoprovision_never_adopts_a_null_phone_tenant_present_in_db(db, monkeypatch):
    """A NULL-phone tenant already exists; autoprovision must create/target a real number."""
    null_tenant = await _seed_tenant(db, phone_number_id=None)
    settings = get_settings()
    monkeypatch.setattr(settings, "ALLOW_WEBHOOK_AUTOPROVISION", True)

    async with db() as session:
        resolved = await tasks._resolve_tenant(session, CONFIGURED_PHONE)

    assert resolved is not None
    assert resolved.id != null_tenant.id
    assert resolved.phone_number_id == CONFIGURED_PHONE
    # The NULL-phone tenant is untouched (still NULL, not adopted/overwritten).
    async with db() as session:
        refreshed = await session.get(Tenant, null_tenant.id)
    assert refreshed.phone_number_id is None


async def test_foreign_number_not_adopted_even_with_flag_on(db, monkeypatch):
    """A mismatched inbound id (vs. the configured single-tenant number) is still dropped."""
    settings = get_settings()
    monkeypatch.setattr(settings, "ALLOW_WEBHOOK_AUTOPROVISION", True)

    async with db() as session:
        resolved = await tasks._resolve_tenant(session, "some-other-unrelated-number")

    assert resolved is None
    assert await _all_tenants(db) == []


async def test_existing_tenant_still_matches_via_exact_path_when_flag_on(db, monkeypatch):
    """Turning the flag on must not change the primary exact-match path's behavior."""
    tenant = await _seed_tenant(db, phone_number_id="known-number")
    settings = get_settings()
    monkeypatch.setattr(settings, "ALLOW_WEBHOOK_AUTOPROVISION", True)

    async with db() as session:
        resolved = await tasks._resolve_tenant(session, "known-number")

    assert resolved is not None
    assert resolved.id == tenant.id
    # No second tenant got created for the same number.
    assert len(await _all_tenants(db)) == 1
