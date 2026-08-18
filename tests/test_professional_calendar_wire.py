"""The professional Calendar STATUS contract — and what must never ride on it.

WHY THIS FILE EXISTS
--------------------
The frontends declared `ProfessionalWire.calendar_connected` as a required
property. The backend has never sent a key by that name: the response model
carries `has_calendar`. TypeScript happily type-checked reads of a property
that is `undefined` at runtime, so `/doctor/perfil` rendered "Agenda não
conectada" — and offered a Connect button — for a doctor whose agenda the
backend considered perfectly available.

Nothing caught it because nothing asserted the contract. So:

  1. the key set of the list response is pinned in test_hub_professionals.py
     (`test_list_shape_is_whitelisted`), which is what stops a frontend type
     from inventing a property; and
  2. this file pins the MEANING of the two calendar keys across all three
     credential states, plus the rule that no secret may ever be on the wire.

`calendar_source` is additive next to `has_calendar` rather than a redefinition
of it, because "is a Calendar available?" and "did THIS doctor connect one?" are
different questions and one boolean cannot answer both honestly.

Synthetic values only: the refresh tokens below are invented strings, not
credentials, and there is no real clinic, calendar or Google account anywhere.
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
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub import professionals as professionals_api  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    professional_calendar_source,
    professional_completeness_item,
    set_google_refresh_token,
    set_professional_google_refresh_token,
)

CLINIC_TOKEN = "1//synthetic-clinic-refresh-token"
OWN_TOKEN = "1//synthetic-professional-refresh-token"

ENDPOINT = "/tenants/me/professionals"

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}


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


@pytest_asyncio.fixture
async def session(db):
    async with db() as s:
        yield s


async def _make(session, **tenant_overrides):
    fields = dict(id=uuid4(), clinic_name="Clinica Wire", phone_number_id=str(uuid4())[:12])
    fields.update(tenant_overrides)
    tenant = Tenant(**fields)
    session.add(tenant)
    await session.flush()
    professional = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    session.add(professional)
    await session.flush()
    return tenant, professional


# ---------------------------------------------------------------------------
# The three credential states
# ---------------------------------------------------------------------------


async def test_source_is_none_when_nobody_connected(session):
    tenant, professional = await _make(session)

    assert await professional_calendar_source(session, professional, tenant) == "none"


async def test_source_is_tenant_when_only_the_clinic_connected(session):
    """The state that made the old UI lie: available, but not the doctor's own."""
    tenant, professional = await _make(session)
    await set_google_refresh_token(session, tenant.id, CLINIC_TOKEN)

    assert await professional_calendar_source(session, professional, tenant) == "tenant"


async def test_source_is_professional_when_the_doctor_connected_their_own(session):
    tenant, professional = await _make(session)
    await set_professional_google_refresh_token(session, professional.id, OWN_TOKEN)

    assert await professional_calendar_source(session, professional, tenant) == "professional"


async def test_own_credential_wins_over_the_clinic_fallback(session):
    tenant, professional = await _make(session)
    await set_google_refresh_token(session, tenant.id, CLINIC_TOKEN)
    await set_professional_google_refresh_token(session, professional.id, OWN_TOKEN)

    assert await professional_calendar_source(session, professional, tenant) == "professional"


@pytest.mark.parametrize(
    "clinic,own",
    [(False, False), (True, False), (False, True), (True, True)],
    ids=["neither", "clinic-only", "own-only", "both"],
)
async def test_has_calendar_and_source_can_never_contradict_each_other(session, clinic, own):
    """The invariant that makes shipping both fields safe.

    If these two ever disagree, a screen reading one and a gate reading the
    other would tell the clinic two different things about the same agenda.
    """
    tenant, professional = await _make(session)
    if clinic:
        await set_google_refresh_token(session, tenant.id, CLINIC_TOKEN)
    if own:
        await set_professional_google_refresh_token(session, professional.id, OWN_TOKEN)

    source = await professional_calendar_source(session, professional, tenant)
    item = await professional_completeness_item(session, professional, tenant)

    assert item.has_calendar is (source != "none")


async def test_source_does_not_leak_across_professionals(session):
    """One doctor's connection must not make a colleague look connected."""
    tenant, connected = await _make(session)
    other = Professional(id=uuid4(), tenant_id=tenant.id, name="Dr. Bruno", is_active=True)
    session.add(other)
    await session.flush()
    await set_professional_google_refresh_token(session, connected.id, OWN_TOKEN)

    assert await professional_calendar_source(session, connected, tenant) == "professional"
    assert await professional_calendar_source(session, other, tenant) == "none"


# ---------------------------------------------------------------------------
# The wire itself
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def api_tenant(db) -> Tenant:
    async with db() as s:
        tenant = Tenant(id=uuid4(), clinic_name="Clinica Wire", phone_number_id=str(uuid4())[:12])
        s.add(tenant)
        await s.commit()
        await s.refresh(tenant)
        return tenant


@pytest.fixture(autouse=True)
def _override_api_deps(db, api_tenant, monkeypatch: pytest.MonkeyPatch):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as s:
            yield s

    async def _fake_get_current_tenant():
        return api_tenant

    async def _fake_entitlements(tenant_id, redis=None):
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="active",
            active=True,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons=dict(_ALL_ADDONS_OFF),
            limits={},
        )

    monkeypatch.setattr(professionals_api, "get_entitlements", _fake_entitlements)
    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _seed(db, tenant: Tenant, **overrides) -> Professional:
    fields = dict(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    fields.update(overrides)
    async with db() as s:
        professional = Professional(**fields)
        s.add(professional)
        await s.commit()
        await s.refresh(professional)
        return professional


async def test_wire_never_ships_calendar_connected(client: AsyncClient, db, api_tenant) -> None:
    """The phantom property, pinned dead.

    A frontend type promising `calendar_connected` was reading `undefined` on
    every row. It is not a key the backend has, and it must not become one by
    accident either — the honest name is `has_calendar`.
    """
    await _seed(db, api_tenant)

    row = (await client.get(ENDPOINT)).json()[0]

    assert "calendar_connected" not in row
    assert row["has_calendar"] is False
    assert row["calendar_source"] == "none"


async def test_wire_reports_the_clinic_fallback_as_such(
    client: AsyncClient, db, api_tenant
) -> None:
    professional = await _seed(db, api_tenant)
    async with db() as s:
        await set_google_refresh_token(s, api_tenant.id, CLINIC_TOKEN)
        await s.commit()

    row = next(r for r in (await client.get(ENDPOINT)).json() if r["id"] == str(professional.id))

    # Available — so the screen must not claim "não conectada" ...
    assert row["has_calendar"] is True
    # ... but it is the CLINIC's, so the screen must not offer "Reconectar" either.
    assert row["calendar_source"] == "tenant"


async def test_wire_reports_an_own_connection_as_such(client: AsyncClient, db, api_tenant) -> None:
    professional = await _seed(db, api_tenant)
    async with db() as s:
        await set_professional_google_refresh_token(s, professional.id, OWN_TOKEN)
        await s.commit()

    row = next(r for r in (await client.get(ENDPOINT)).json() if r["id"] == str(professional.id))

    assert row["has_calendar"] is True
    assert row["calendar_source"] == "professional"


async def test_no_credential_material_reaches_the_wire(client: AsyncClient, db, api_tenant) -> None:
    """Status is a category. The credential behind it stays server-side.

    Checked against the whole serialized body, not field by field, so a future
    field that happens to carry a token fails here too.
    """
    professional = await _seed(db, api_tenant)
    async with db() as s:
        await set_google_refresh_token(s, api_tenant.id, CLINIC_TOKEN)
        await set_professional_google_refresh_token(s, professional.id, OWN_TOKEN)
        await s.commit()

    body = (await client.get(ENDPOINT)).text.lower()

    for secret in (CLINIC_TOKEN.lower(), OWN_TOKEN.lower(), "1//"):
        assert secret not in body
    for forbidden_key in ("refresh_token", "access_token", "encrypted", "authorization", "scope"):
        assert forbidden_key not in body
