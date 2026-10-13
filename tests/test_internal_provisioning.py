"""Tests for the internal onboarding/provisioning API (`/internal/*`, contract v1 §4).

Auth-guard tests mirror test_internal.py (no DB touched — the dependency
rejects before the handler runs). Data-path tests use a REAL in-memory
sqlite DB with `get_session` overridden on the app (test_hub_professionals.py's
pattern), since these handlers actually read/write rows — unlike
test_internal.py's read-only stub-session tests.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from uuid import UUID, uuid4  # noqa: E402

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
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    set_google_refresh_token,
    set_professional_google_refresh_token,
)

GOOD_KEY = "test-internal-key"
HEADERS = {"X-Internal-Api-Key": GOOD_KEY}


async def test_interactive_professional_creation_cannot_bypass_capacity(client, db, monkeypatch):
    from fastapi import HTTPException

    from secretaria.services import provisioning

    async def denied(*args, **kwargs):
        raise HTTPException(403, "multi_professional_not_entitled")

    monkeypatch.setattr(provisioning, "require_professional_capacity", denied, raising=False)
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        headers=HEADERS,
        json={"name": "New Doctor", "interactive": True},
    )
    assert response.status_code == 403


async def test_staff_welcome_queue_never_contains_plain_credentials(client, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from secretaria.main import app

    pool = _FakeArqPool()
    monkeypatch.setattr(app.state, "arq_pool", pool, raising=False)
    response = await client.post(
        "/internal/notifications/email",
        headers=HEADERS,
        json={
            "to": "staff@example.com",
            "template": "staff_temporary_password",
            "variables": {
                "name": "Staff",
                "clinic_name": "Test Clinic",
                "temporary_password": "TemporaryTest7",
                "login_url": "https://portal.example.com/",
                "tenant_id": str(uuid4()),
                "user_id": str(uuid4()),
                "credential_version": 0,
                "expires_at": (datetime.now(UTC) + timedelta(hours=72)).isoformat(),
            },
        },
    )
    assert response.status_code == 200
    assert response.json() == {"queued": True}
    name, args, kwargs = pool.calls[0]
    assert name == "send_staff_welcome_email"
    assert args == ()
    assert "payload_encrypted" in kwargs
    assert "TemporaryTest7" not in repr(args)
    assert "staff@example.com" not in repr(args)
    assert "TemporaryTest7" not in repr(kwargs)
    assert "staff@example.com" not in repr(kwargs)


async def test_internal_roster_includes_inactive_without_changing_completeness(client, db):
    tenant = await _seed_tenant(db)
    own = await _seed_professional(db, tenant, is_active=False)
    other_tenant = await _seed_tenant(db)
    await _seed_professional(db, other_tenant)
    response = await client.get(f"/internal/tenants/{tenant.id}/professionals", headers=HEADERS)
    assert response.status_code == 200
    assert [row["id"] for row in response.json()] == [str(own.id)]
    assert response.json()[0]["is_active"] is False


class _FakeArqPool:
    """Records every enqueue_job call; installed on app.state.arq_pool."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs) -> None:
        self.calls.append((name, args, kwargs))


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


async def _seed_tenant(db, **overrides) -> Tenant:
    fields = dict(id=uuid4(), clinic_name="Clinic", phone_number_id=None, is_active=False)
    fields.update(overrides)
    async with db() as session:
        tenant = Tenant(**fields)
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _seed_professional(db, tenant: Tenant, **overrides) -> Professional:
    fields = dict(tenant_id=tenant.id, name="Dr. Existing", is_active=True)
    fields.update(overrides)
    async with db() as session:
        prof = Professional(**fields)
        session.add(prof)
        await session.commit()
        await session.refresh(prof)
        return prof


# --------------------------------------------------------------------------
# Auth guard — no DB touched
# --------------------------------------------------------------------------

_TENANT_ID = str(uuid4())
_AUTH_CASES = [
    ("post", "/internal/tenants", {"tenant_id": _TENANT_ID, "clinic_name": "X"}),
    (
        "post",
        f"/internal/tenants/{_TENANT_ID}/whatsapp-connection",
        {"phone_number_id": "123"},
    ),
    ("get", f"/internal/tenants/{_TENANT_ID}/config-status", None),
    ("post", f"/internal/tenants/{_TENANT_ID}/professionals", {"name": "Dr. X"}),
    ("post", f"/internal/tenants/{_TENANT_ID}/activate", None),
    (
        "post",
        "/internal/notifications/email",
        {"to": "a@b.com", "template": "connection_success", "variables": {}},
    ),
]


def _request_kwargs(body: dict | None) -> dict:
    """httpx's `.get()` shortcut has no `json=` parameter - only pass it for
    the methods in `_AUTH_CASES` that actually carry a body."""
    return {"json": body} if body is not None else {}


@pytest.mark.parametrize("method,path,body", _AUTH_CASES)
async def test_missing_key_is_unauthorized(client: AsyncClient, method, path, body) -> None:
    response = await getattr(client, method)(path, **_request_kwargs(body))
    assert response.status_code == 401


@pytest.mark.parametrize("method,path,body", _AUTH_CASES)
async def test_wrong_key_is_unauthorized(client: AsyncClient, method, path, body) -> None:
    response = await getattr(client, method)(
        path, headers={"X-Internal-Api-Key": "wrong-key"}, **_request_kwargs(body)
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
            "/internal/tenants",
            json={"tenant_id": _TENANT_ID, "clinic_name": "X"},
            headers={"X-Internal-Api-Key": "anything"},
        )
        assert response.status_code == 403
    finally:
        monkeypatch.setenv("INTERNAL_API_KEY", GOOD_KEY)
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# Endpoint 1: POST /internal/tenants — provision, idempotent
# --------------------------------------------------------------------------


async def test_provision_tenant_creates_with_caller_supplied_id(client: AsyncClient, db) -> None:
    tenant_id = uuid4()
    response = await client.post(
        "/internal/tenants",
        json={
            "tenant_id": str(tenant_id),
            "clinic_name": "Clínica Nova",
            "contact_email": "owner@example.com",
            "timezone": "America/Bahia",
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body == {"tenant_id": str(tenant_id), "created": True}

    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
    assert tenant is not None
    assert tenant.clinic_name == "Clínica Nova"
    assert tenant.contact_email == "owner@example.com"
    assert tenant.timezone == "America/Bahia"
    assert tenant.phone_number_id is None
    assert tenant.is_active is False


async def test_provision_tenant_is_idempotent_no_mutation_on_retry(
    client: AsyncClient, db
) -> None:
    tenant_id = uuid4()
    first = await client.post(
        "/internal/tenants",
        json={"tenant_id": str(tenant_id), "clinic_name": "Original Name"},
        headers=HEADERS,
    )
    assert first.json()["created"] is True

    second = await client.post(
        "/internal/tenants",
        json={"tenant_id": str(tenant_id), "clinic_name": "Different Name On Retry"},
        headers=HEADERS,
    )
    assert second.status_code == 200
    assert second.json() == {"tenant_id": str(tenant_id), "created": False}

    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
    assert tenant.clinic_name == "Original Name"  # untouched by the retry


async def test_provision_tenant_defaults_timezone_when_omitted(client: AsyncClient, db) -> None:
    tenant_id = uuid4()
    response = await client.post(
        "/internal/tenants",
        json={"tenant_id": str(tenant_id), "clinic_name": "Clínica"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
    assert tenant.timezone == "America/Sao_Paulo"


# --------------------------------------------------------------------------
# Endpoint 2: POST /internal/tenants/{id}/whatsapp-connection
# --------------------------------------------------------------------------


async def test_whatsapp_connection_unknown_tenant_is_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/internal/tenants/{uuid4()}/whatsapp-connection",
        json={"phone_number_id": "555"},
        headers=HEADERS,
    )
    assert response.status_code == 404


async def test_whatsapp_connection_connects_and_sets_connected_at(
    client: AsyncClient, db
) -> None:
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/whatsapp-connection",
        json={"phone_number_id": "5511900001111", "waba_id": "waba-1", "access_token": "tok"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {"connected": True}

    async with db() as session:
        refreshed = await session.get(Tenant, tenant.id)
        connected = await session.scalar(
            select(Tenant.connected_at).where(Tenant.id == tenant.id)
        )
    assert refreshed.phone_number_id == "5511900001111"
    assert refreshed.waba_id == "waba-1"
    assert connected is not None


async def test_whatsapp_connection_stores_token_encrypted(client: AsyncClient, db) -> None:
    from secretaria.services.tenant_config import get_waba_token, has_waba_token

    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/whatsapp-connection",
        json={"phone_number_id": "5511900002222", "access_token": "super-secret-token"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert "super-secret-token" not in response.text

    async with db() as session:
        assert await has_waba_token(session, tenant.id) is True
        assert await get_waba_token(session, tenant.id) == "super-secret-token"


async def test_whatsapp_connection_conflict_when_number_taken(client: AsyncClient, db) -> None:
    await _seed_tenant(db, phone_number_id="5511900003333")
    other = await _seed_tenant(db)

    response = await client.post(
        f"/internal/tenants/{other.id}/whatsapp-connection",
        json={"phone_number_id": "5511900003333"},
        headers=HEADERS,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "phone_number_conflict"


async def test_whatsapp_connection_idempotent_for_same_values(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)
    body = {"phone_number_id": "5511900004444", "waba_id": "waba-x"}

    first = await client.post(
        f"/internal/tenants/{tenant.id}/whatsapp-connection", json=body, headers=HEADERS
    )
    assert first.status_code == 200
    async with db() as session:
        first_connected_at = (await session.get(Tenant, tenant.id)).connected_at

    second = await client.post(
        f"/internal/tenants/{tenant.id}/whatsapp-connection", json=body, headers=HEADERS
    )
    assert second.status_code == 200
    async with db() as session:
        refreshed = await session.get(Tenant, tenant.id)
    assert refreshed.connected_at == first_connected_at  # preserved, not bumped


# --------------------------------------------------------------------------
# Endpoint 3: GET /internal/tenants/{id}/config-status
# --------------------------------------------------------------------------


async def test_config_status_unknown_tenant_is_404(client: AsyncClient) -> None:
    response = await client.get(f"/internal/tenants/{uuid4()}/config-status", headers=HEADERS)
    assert response.status_code == 404


async def test_config_status_shape_and_incomplete_by_default(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)
    await _seed_professional(db, tenant, name="Dr. Bare")

    response = await client.get(f"/internal/tenants/{tenant.id}/config-status", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {
        "connected",
        "connected_at",
        "mode_resolved",
        "mode_resolved_at",
        "history_sync_status",
        "config_complete",
        "reasons",
        "complete_count",
        "total_active",
        "professionals",
    }
    assert body["connected"] is False
    assert body["mode_resolved"] is False
    assert body["history_sync_status"] == "none"
    assert body["config_complete"] is False
    assert body["total_active"] == 1
    assert body["complete_count"] == 0
    prof_row = body["professionals"][0]
    assert set(prof_row.keys()) == {
        "id",
        "name",
        "is_active",
        "has_calendar",
        "has_hours",
        "has_services",
        "complete",
    }
    assert prof_row["complete"] is False


async def test_config_status_complete_when_fully_configured(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(
        db,
        phone_number_id="5511900005555",
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
    )
    await _seed_professional(db, tenant, name="Dr. Full")
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "refresh-token")
        await session.commit()

    response = await client.get(f"/internal/tenants/{tenant.id}/config-status", headers=HEADERS)
    body = response.json()
    assert body["connected"] is True
    assert body["config_complete"] is True
    assert body["complete_count"] == 1


# --------------------------------------------------------------------------
# Endpoint 4: POST /internal/tenants/{id}/professionals — attach-by-name
# --------------------------------------------------------------------------


async def test_attach_professional_unknown_tenant_is_404(client: AsyncClient) -> None:
    response = await client.post(
        f"/internal/tenants/{uuid4()}/professionals",
        json={"name": "Dr. X"},
        headers=HEADERS,
    )
    assert response.status_code == 404


async def test_attach_professional_creates_when_no_match(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        json={"name": "Dra. Nova", "specialty": "Dermatologia", "about": "Bio"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True

    async with db() as session:
        prof = await session.get(Professional, UUID(body["professional_id"]))
    assert prof.name == "Dra. Nova"
    assert prof.specialty == "Dermatologia"


async def test_attach_professional_case_insensitive_match_returns_existing(
    client: AsyncClient, db
) -> None:
    tenant = await _seed_tenant(db)
    existing = await _seed_professional(db, tenant, name="Dra. Ana Souza", is_active=True)

    response = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        json={"name": "dra. ana souza"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is False
    assert body["professional_id"] == str(existing.id)


async def test_attach_professional_does_not_match_inactive(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)
    await _seed_professional(db, tenant, name="Dr. Inativo", is_active=False)

    response = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        json={"name": "Dr. Inativo"},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json()["created"] is True  # a NEW (active) row, inactive one not matched


@pytest.mark.parametrize("existing", [False, True])
async def test_invite_or_self_binding_prepares_shared_calendar(client, db, monkeypatch, existing):
    """Both consumers use this internal endpoint; an existing unprepared row is repaired."""
    from secretaria.services import provisioning, tenant_config

    tenant = await _seed_tenant(db, google_calendar_mode="shared_account")
    other = await _seed_tenant(db, google_calendar_mode="shared_account")
    untouched = await _seed_professional(db, other, name="Dr. New")
    if existing:
        await _seed_professional(db, tenant, name="Dr. New")
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "clinic-test-token")
        await session.commit()

    class GoogleCalendar:
        async def create_secondary_calendar(self, summary):
            return {"id": "doctor-test@group.calendar.google.com"}

    async def capacity_allowed(*args):
        pass

    monkeypatch.setattr(provisioning, "require_professional_capacity", capacity_allowed)
    monkeypatch.setattr(
        tenant_config.CalendarService, "from_tenant_config", lambda config: GoogleCalendar()
    )
    response = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        headers=HEADERS,
        json={"name": "Dr. New", "interactive": True},
    )
    assert response.status_code == 200
    assert response.json()["created"] is not existing
    async with db() as session:
        professional = await session.get(Professional, UUID(response.json()["professional_id"]))
        assert professional.google_calendar_id == "doctor-test@group.calendar.google.com"
        assert (await session.get(Professional, untouched.id)).google_calendar_id is None

    # A repeated attach must preserve the linked calendar without writing to Google again.
    unexpected_calls = []

    def unexpected_google(config):
        unexpected_calls.append(config)
        raise AssertionError("An existing linked calendar must be reused")

    monkeypatch.setattr(tenant_config.CalendarService, "from_tenant_config", unexpected_google)
    again = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        headers=HEADERS,
        json={"name": "Dr. New", "interactive": True},
    )
    assert again.status_code == 200
    assert unexpected_calls == []
    assert again.json()["professional_id"] == response.json()["professional_id"]
    async with db() as session:
        professional = await session.get(Professional, UUID(again.json()["professional_id"]))
        assert professional.google_calendar_id == "doctor-test@group.calendar.google.com"


@pytest.mark.parametrize("mode,connected", [("shared_account", False), ("per_professional", True)])
async def test_internal_attach_without_shared_connection_preserves_registration(
    client, db, monkeypatch, mode, connected
):
    from secretaria.services import tenant_config

    tenant = await _seed_tenant(db, google_calendar_mode=mode)
    if connected:
        async with db() as session:
            await set_google_refresh_token(session, tenant.id, "clinic-test-token")
            await session.commit()

    unexpected_calls = []

    def unexpected_google(config):
        unexpected_calls.append(config)
        raise AssertionError("No remote calendar should be created")

    monkeypatch.setattr(tenant_config.CalendarService, "from_tenant_config", unexpected_google)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        headers=HEADERS,
        json={"name": "Dr. New"},
    )
    assert response.status_code == 200
    assert unexpected_calls == []
    async with db() as session:
        professional = await session.get(Professional, UUID(response.json()["professional_id"]))
        assert professional.name == "Dr. New"
        assert professional.google_calendar_id is None


async def test_internal_attach_recovers_calendar_after_google_failure(client, db, monkeypatch):
    from secretaria.services import tenant_config
    from secretaria.services.calendar import CalendarUnavailableError

    tenant = await _seed_tenant(db, google_calendar_mode="shared_account")
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "clinic-test-token")
        await session.commit()

    class GoogleCalendar:
        unavailable = True

        async def create_secondary_calendar(self, summary):
            if self.unavailable:
                raise CalendarUnavailableError("test outage")
            return {"id": "recovered@group.calendar.google.com"}

    google = GoogleCalendar()
    monkeypatch.setattr(tenant_config.CalendarService, "from_tenant_config", lambda config: google)
    response = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        headers=HEADERS,
        json={"name": "Dr. New"},
    )
    assert response.status_code == 200
    async with db() as session:
        professional = await session.get(Professional, UUID(response.json()["professional_id"]))
        assert professional.google_calendar_id is None

    google.unavailable = False
    retry = await client.post(
        f"/internal/tenants/{tenant.id}/professionals",
        headers=HEADERS,
        json={"name": "Dr. New"},
    )
    assert retry.status_code == 200
    assert retry.json()["professional_id"] == response.json()["professional_id"]
    async with db() as session:
        professional = await session.get(Professional, UUID(retry.json()["professional_id"]))
        assert professional.google_calendar_id == "recovered@group.calendar.google.com"


# --------------------------------------------------------------------------
# Endpoint 5: POST /internal/tenants/{id}/activate
# --------------------------------------------------------------------------


async def test_activate_unknown_tenant_is_404(client: AsyncClient) -> None:
    response = await client.post(f"/internal/tenants/{uuid4()}/activate", headers=HEADERS)
    assert response.status_code == 404


async def test_activate_refuses_with_reasons_when_incomplete(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db)  # no phone, no professional config
    await _seed_professional(db, tenant)

    response = await client.post(f"/internal/tenants/{tenant.id}/activate", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["activated"] is False
    assert body["reasons"]  # non-empty, human-readable

    async with db() as session:
        refreshed = await session.get(Tenant, tenant.id)
    assert refreshed.is_active is False


async def test_activate_refuses_when_config_complete_but_not_connected(
    client: AsyncClient, db
) -> None:
    tenant = await _seed_tenant(
        db,
        phone_number_id=None,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
    )
    await _seed_professional(db, tenant)
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "refresh-token")
        await session.commit()

    response = await client.post(f"/internal/tenants/{tenant.id}/activate", headers=HEADERS)
    body = response.json()
    assert body["activated"] is False
    assert any("WhatsApp" in r for r in body["reasons"])


async def test_activate_succeeds_when_fully_ready(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(
        db,
        phone_number_id="5511900006666",
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
    )
    await _seed_professional(db, tenant)
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "refresh-token")
        await session.commit()

    response = await client.post(f"/internal/tenants/{tenant.id}/activate", headers=HEADERS)
    assert response.status_code == 200
    body = response.json()
    assert body["activated"] is True
    assert body["reasons"] == []

    async with db() as session:
        refreshed = await session.get(Tenant, tenant.id)
    assert refreshed.is_active is True


async def test_activate_is_idempotent_once_active(client: AsyncClient, db) -> None:
    tenant = await _seed_tenant(db, phone_number_id="5511900007777", is_active=True)

    response = await client.post(f"/internal/tenants/{tenant.id}/activate", headers=HEADERS)
    assert response.status_code == 200
    assert response.json() == {"activated": True, "reasons": []}


async def test_activate_professional_aware_own_credential_suffices(client: AsyncClient, db) -> None:
    """A professional with its OWN calendar (no tenant-level token) can still activate."""
    tenant = await _seed_tenant(db, phone_number_id="5511900008888")
    prof = await _seed_professional(
        db,
        tenant,
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
    )
    async with db() as session:
        await set_professional_google_refresh_token(session, prof.id, "professional-own-token")
        await session.commit()

    response = await client.post(f"/internal/tenants/{tenant.id}/activate", headers=HEADERS)
    assert response.status_code == 200
    assert response.json()["activated"] is True


# --------------------------------------------------------------------------
# Endpoint 6: POST /internal/notifications/email — enqueue
# --------------------------------------------------------------------------


async def test_notifications_email_enqueues_job(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.main import app as fastapi_app

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    response = await client.post(
        "/internal/notifications/email",
        json={
            "to": "doctor@example.com",
            "template": "connection_success",
            "variables": {"clinic_name": "Clínica Teste"},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {"queued": True}

    assert len(fake_pool.calls) == 1
    name, args, kwargs = fake_pool.calls[0]
    assert name == "send_transactional_email"
    # Positional order MUST match the arq task's signature: (template, to, variables).
    assert args == ("connection_success", "doctor@example.com", {"clinic_name": "Clínica Teste"})
    assert kwargs == {}


async def test_notifications_email_rejects_a_template_this_service_cannot_render(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An id no template exists for is a 422 — and nothing reaches the queue.

    The regression this pins: brain-api asked for `patient_access_otp` months before
    services/email.py registered it, and every one of those patient login codes was
    answered `{"queued": true}` and then dropped by the worker. A caller defect that
    no retry fixes must fail at the door, not in another process's log.
    """
    from secretaria.main import app as fastapi_app

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    response = await client.post(
        "/internal/notifications/email",
        json={"to": "patient@example.com", "template": "no_such_template", "variables": {}},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert fake_pool.calls == []


async def test_notifications_email_accepts_the_patient_login_code_template(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact id brain-api's `/patient-access/request-otp` sends must enqueue."""
    from secretaria.main import app as fastapi_app

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    response = await client.post(
        "/internal/notifications/email",
        json={
            "to": "patient@example.com",
            "template": "patient_access_otp",
            "variables": {"code": "123456", "ttl_minutes": 10},
        },
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {"queued": True}
    assert fake_pool.calls[0][1][0] == "patient_access_otp"


async def test_notifications_email_queued_false_when_pool_unavailable(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.main import app as fastapi_app

    monkeypatch.setattr(fastapi_app.state, "arq_pool", None, raising=False)

    response = await client.post(
        "/internal/notifications/email",
        json={"to": "doctor@example.com", "template": "connection_success", "variables": {}},
        headers=HEADERS,
    )
    assert response.status_code == 200
    assert response.json() == {"queued": False}
