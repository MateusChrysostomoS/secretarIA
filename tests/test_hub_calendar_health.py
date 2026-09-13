"""Tests for GET /tenants/me/calendar/health (api/hub/oauth.py) and the service
behind it, services/tenant_config.py::calendar_credential_health.

The failure this pins was live in production on 2026-09-12: a clinic in
`shared_account` mode whose Google refresh token had been expired/revoked
(`invalid_grant`). Every stored-token flag (`calendar_connected`, `has_calendar`,
`calendar_source`) stayed true, so the hub said "Conectado", no professional row
had anything to press (that mode has no per-row action), and no patient could
book with any doctor. Only a live call can tell "stored" from "still works".

Same harness as test_hub_oauth.py: a real in-memory sqlite DB behind
`get_session`, a canned Tenant behind `get_current_tenant`, and a fake
CalendarService monkeypatched onto services.tenant_config - no test here ever
reaches Google.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import asyncio  # noqa: E402
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

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.services import tenant_config as tc  # noqa: E402
from secretaria.services.calendar import (  # noqa: E402
    CalendarUnavailableError,
    GoogleTokenRevokedError,
)
from secretaria.services.tenant_config import (  # noqa: E402
    set_google_refresh_token,
    set_professional_google_refresh_token,
)

ENDPOINT = "/tenants/me/calendar/health"
CLINIC_TOKEN = "clinic-refresh-token"


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
async def tenant(db) -> Tenant:
    async with db() as session:
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant():
        return tenant

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


class _FakeCalendar:
    """Stands in for CalendarService. Answers each probe by the refresh token it
    was built with, and records (token, calendar_id) so a test can assert WHICH
    credential was paired with WHICH calendar."""

    outcomes: dict[str, BaseException] = {}
    delay_s: float = 0.0
    probed: list[tuple[str | None, str]] = []

    def __init__(self, config) -> None:
        self._config = config

    async def check_availability(self, start, end) -> list[dict]:
        token = self._config.google_refresh_token
        _FakeCalendar.probed.append((token, self._config.google_calendar_id))
        if _FakeCalendar.delay_s:
            await asyncio.sleep(_FakeCalendar.delay_s)
        outcome = _FakeCalendar.outcomes.get(token)
        if outcome is not None:
            raise outcome
        return []


class _FakeCalendarFactory:
    @staticmethod
    def from_tenant_config(config) -> _FakeCalendar:
        return _FakeCalendar(config)


@pytest.fixture(autouse=True)
def fake_google(monkeypatch: pytest.MonkeyPatch) -> type[_FakeCalendar]:
    _FakeCalendar.outcomes = {}
    _FakeCalendar.delay_s = 0.0
    _FakeCalendar.probed = []
    monkeypatch.setattr(tc, "CalendarService", _FakeCalendarFactory)
    return _FakeCalendar


async def _connect_clinic(db, tenant: Tenant, token: str = CLINIC_TOKEN) -> None:
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, token)
        await session.commit()


async def _seed_professional(
    db, tenant: Tenant, *, own_token: str | None = None, **fields
) -> Professional:
    values = dict(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    values.update(fields)
    async with db() as session:
        professional = Professional(**values)
        session.add(professional)
        await session.commit()
        await session.refresh(professional)
        if own_token:
            await set_professional_google_refresh_token(session, professional.id, own_token)
            await session.commit()
        return professional


async def _set_mode(db, tenant: Tenant, mode: str) -> None:
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.google_calendar_mode = mode
        await session.commit()
    # get_current_tenant is overridden to hand the route THIS object.
    tenant.google_calendar_mode = mode


# --------------------------------------------------------------------------
# The clinic's own account
# --------------------------------------------------------------------------


async def test_no_clinic_token_is_disconnected_without_calling_google(
    client: AsyncClient, fake_google
) -> None:
    response = await client.get(ENDPOINT)

    assert response.status_code == 200
    assert response.json() == {"clinic": "disconnected", "professionals": []}
    assert fake_google.probed == []


async def test_accepted_clinic_token_is_ok_and_probes_the_clinic_calendar(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    await _connect_clinic(db, tenant)

    response = await client.get(ENDPOINT)

    assert response.json() == {"clinic": "ok", "professionals": []}
    assert fake_google.probed == [(CLINIC_TOKEN, tenant.google_calendar_id)]


async def test_revoked_clinic_token_is_reconnect_required(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    """The production state: shared_account, clinic token rejected by Google."""
    await _set_mode(db, tenant, "shared_account")
    await _connect_clinic(db, tenant)
    await _seed_professional(db, tenant, google_calendar_id="ana@group.calendar.google.com")
    fake_google.outcomes[CLINIC_TOKEN] = GoogleTokenRevokedError(
        "Google Calendar refresh token rejected"
    )

    response = await client.get(ENDPOINT)

    assert response.status_code == 200
    assert response.json() == {"clinic": "reconnect_required", "professionals": []}


async def test_google_outage_is_unavailable_never_reconnect_required(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    """A 5xx or a network blip passes on its own; asking the clinic to reconnect
    over it would send them to redo a connection that is fine."""
    await _connect_clinic(db, tenant)
    fake_google.outcomes[CLINIC_TOKEN] = CalendarUnavailableError(
        "Google Calendar returned HTTP 503"
    )

    assert (await client.get(ENDPOINT)).json()["clinic"] == "unavailable"


async def test_unexpected_probe_failure_is_unavailable_not_a_500(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    await _connect_clinic(db, tenant)
    fake_google.outcomes[CLINIC_TOKEN] = RuntimeError("Google Calendar credentials missing")

    response = await client.get(ENDPOINT)

    assert response.status_code == 200
    assert response.json()["clinic"] == "unavailable"


async def test_a_probe_past_the_deadline_reports_unavailable_not_ok(
    client: AsyncClient, db, tenant, fake_google, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect_clinic(db, tenant)
    fake_google.delay_s = 5.0
    monkeypatch.setattr(tc, "_HEALTH_PROBE_TIMEOUT_S", 0.05)

    assert (await client.get(ENDPOINT)).json()["clinic"] == "unavailable"


async def test_clinic_status_is_never_resolved_through_a_sole_professional(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    """load_tenant_config resolves a single-professional clinic's credential
    THROUGH that professional. The clinic status must still report the clinic's
    own account, or a working doctor token would hide a dead clinic one."""
    await _connect_clinic(db, tenant)
    await _seed_professional(
        db, tenant, own_token="own-token", google_calendar_id="own@group.calendar.google.com"
    )
    fake_google.outcomes[CLINIC_TOKEN] = GoogleTokenRevokedError("rejected")

    body = (await client.get(ENDPOINT)).json()

    assert body["clinic"] == "reconnect_required"
    assert [item["status"] for item in body["professionals"]] == ["ok"]
    assert (CLINIC_TOKEN, tenant.google_calendar_id) in fake_google.probed


# --------------------------------------------------------------------------
# Professionals' own accounts
# --------------------------------------------------------------------------


async def test_shared_account_never_probes_an_own_token(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    """In shared_account every booking uses the clinic's token
    (_professional_credential), so an own token is never what a patient uses."""
    await _set_mode(db, tenant, "shared_account")
    await _connect_clinic(db, tenant)
    await _seed_professional(
        db, tenant, own_token="own-token", google_calendar_id="ana@group.calendar.google.com"
    )

    body = (await client.get(ENDPOINT)).json()

    assert body == {"clinic": "ok", "professionals": []}
    assert [token for token, _calendar in fake_google.probed] == [CLINIC_TOKEN]


async def test_per_professional_reports_own_tokens_paired_like_the_booking_path(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    await _connect_clinic(db, tenant)
    ana = await _seed_professional(
        db,
        tenant,
        name="Dra. Ana",
        own_token="ana-token",
        google_calendar_id="ana@group.calendar.google.com",
    )
    bruno = await _seed_professional(db, tenant, name="Dr. Bruno", own_token="bruno-token")
    # No own token: covered by the clinic's credential, which `clinic` reports.
    await _seed_professional(db, tenant, name="Dra. Carla")
    fake_google.outcomes["bruno-token"] = GoogleTokenRevokedError("rejected")

    body = (await client.get(ENDPOINT)).json()

    assert body["clinic"] == "ok"
    assert {item["professional_id"]: item["status"] for item in body["professionals"]} == {
        str(ana.id): "ok",
        str(bruno.id): "reconnect_required",
    }
    # Own calendar id when set, else the clinic's (resolve_professional_calendar).
    assert set(fake_google.probed) == {
        (CLINIC_TOKEN, tenant.google_calendar_id),
        ("ana-token", "ana@group.calendar.google.com"),
        ("bruno-token", tenant.google_calendar_id),
    }


async def test_inactive_professional_is_not_probed(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    await _seed_professional(db, tenant, own_token="retired-token", is_active=False)

    body = (await client.get(ENDPOINT)).json()

    assert body == {"clinic": "disconnected", "professionals": []}
    assert fake_google.probed == []


async def test_response_carries_categories_only(
    client: AsyncClient, db, tenant, fake_google
) -> None:
    await _connect_clinic(db, tenant)
    await _seed_professional(
        db, tenant, own_token="ana-token", google_calendar_id="ana@group.calendar.google.com"
    )
    fake_google.outcomes["ana-token"] = GoogleTokenRevokedError(
        "invalid_grant: Token has been expired or revoked."
    )

    response = await client.get(ENDPOINT)

    for leaked in (CLINIC_TOKEN, "ana-token", "ana@group.calendar.google.com", "invalid_grant"):
        assert leaked not in response.text
