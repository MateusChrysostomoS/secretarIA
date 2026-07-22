"""Tests for the admin fleet endpoints (GET /admin/tenants + calendar health).

These never reach Postgres or Google: the page loader and the live probe are
monkeypatched, and the status-mapping logic is unit-tested by stubbing
CalendarService. The guard (X-Admin-Token) reuses the same require_admin
dependency exercised by test_admin.py.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

# Set the admin token BEFORE secretaria imports so Settings picks it up.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ["ADMIN_TOKEN"] = "test-admin-token"

import pytest  # noqa: E402
from httpx import AsyncClient  # noqa: E402

from secretaria.api.admin import tenants as tenants_api  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.schemas.admin import CalendarHealth, CalendarHealthStatus  # noqa: E402
from secretaria.services.calendar import CalendarUnavailableError  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402

GOOD_TOKEN = "test-admin-token"
ENDPOINT = "/admin/tenants"

# A sentinel that must NEVER appear in any response body. The WABA token no longer
# lives on the Tenant row (it is ciphertext in tenant_credentials), so the leak check
# now guards the serialization path rather than a model column.
SECRET_WA_TOKEN = "SUPER-SECRET-WA-TOKEN"


def _make_tenant(**overrides: object) -> Tenant:
    """A Tenant ORM instance (config only — no secret columns exist on it)."""
    fields: dict[str, object] = {
        "id": uuid4(),
        "clinic_name": "Clinic A",
        "phone_number_id": "111",
        "contact_email": "a@example.com",
        "language": "pt-BR",
        "timezone": "America/Sao_Paulo",
        "is_active": True,
        "precheck_enabled": False,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "google_calendar_id": "primary",
    }
    fields.update(overrides)
    return Tenant(**fields)


def _make_config(refresh_token: str | None) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=uuid4(),
        clinic_name="Clinic",
        greeting_message=None,
        persona_notes=None,
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="primary",
        google_refresh_token=refresh_token,
    )


# --------------------------------------------------------------------------
# Auth guard
# --------------------------------------------------------------------------


async def test_missing_token_is_forbidden(client: AsyncClient) -> None:
    response = await client.get(ENDPOINT)
    assert response.status_code == 403


async def test_wrong_token_is_forbidden(client: AsyncClient) -> None:
    response = await client.get(ENDPOINT, headers={"X-Admin-Token": "wrong-token"})
    assert response.status_code == 403


async def test_unconfigured_token_returns_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.config import get_settings

    monkeypatch.setenv("ADMIN_TOKEN", "")
    get_settings.cache_clear()
    try:
        response = await client.get(ENDPOINT, headers={"X-Admin-Token": "anything"})
        assert response.status_code == 503
    finally:
        monkeypatch.setenv("ADMIN_TOKEN", GOOD_TOKEN)
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# List shape + secret safety
# --------------------------------------------------------------------------


async def test_list_shape_and_no_secrets(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    connected = _make_tenant(clinic_name="Connected", phone_number_id="111")
    bare = _make_tenant(clinic_name="Bare", phone_number_id="222", contact_email=None)

    async def _fake_list(session: object, *, limit: int, offset: int):
        return [(connected, True), (bare, False)], 2

    monkeypatch.setattr(tenants_api, "_list_tenants_page", _fake_list)

    response = await client.get(ENDPOINT, headers={"X-Admin-Token": GOOD_TOKEN})
    assert response.status_code == 200
    body = response.json()

    assert body["total"] == 2
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert [item["clinic_name"] for item in body["items"]] == ["Connected", "Bare"]

    first, second = body["items"]
    assert first["calendar"]["connected"] is True
    assert first["calendar"]["status"] == "connected"
    assert first["calendar"]["last_checked_at"] is None  # not probed
    assert second["calendar"]["status"] == "not_connected"

    # No secret leaks anywhere in the serialized payload.
    assert SECRET_WA_TOKEN not in response.text
    assert "access_token" not in response.text


async def test_empty_fleet(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_list(session: object, *, limit: int, offset: int):
        return [], 0

    monkeypatch.setattr(tenants_api, "_list_tenants_page", _fake_list)

    response = await client.get(ENDPOINT, headers={"X-Admin-Token": GOOD_TOKEN})
    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0, "limit": 50, "offset": 0}


@pytest.mark.parametrize("bad_limit", [0, 10000])
async def test_pagination_bounds_are_rejected(
    client: AsyncClient, bad_limit: int
) -> None:
    response = await client.get(
        f"{ENDPOINT}?limit={bad_limit}", headers={"X-Admin-Token": GOOD_TOKEN}
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# Probe wiring (list ?probe) + status mapping (_probe_config)
# --------------------------------------------------------------------------


async def test_probe_false_does_not_probe(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_list(session: object, *, limit: int, offset: int):
        return [(_make_tenant(), True)], 1

    async def _boom(*args: object, **kwargs: object):
        raise AssertionError("probe must not run when probe=false")

    monkeypatch.setattr(tenants_api, "_list_tenants_page", _fake_list)
    monkeypatch.setattr(tenants_api, "_probe_page", _boom)

    response = await client.get(ENDPOINT, headers={"X-Admin-Token": GOOD_TOKEN})
    assert response.status_code == 200


async def test_probe_true_invokes_probe_page(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, int] = {}

    async def _fake_list(session: object, *, limit: int, offset: int):
        return [(_make_tenant(), True)], 1

    async def _fake_probe_page(session: object, rows: list):
        seen["rows"] = len(rows)
        return [
            tenants_api._summary(
                tenant,
                CalendarHealth(
                    connected=True,
                    calendar_id="primary",
                    status=CalendarHealthStatus.connected,
                    last_checked_at=datetime.now(UTC),
                    detail=None,
                ),
            )
            for tenant, _ in rows
        ]

    monkeypatch.setattr(tenants_api, "_list_tenants_page", _fake_list)
    monkeypatch.setattr(tenants_api, "_probe_page", _fake_probe_page)

    response = await client.get(
        f"{ENDPOINT}?probe=true", headers={"X-Admin-Token": GOOD_TOKEN}
    )
    assert response.status_code == 200
    assert seen["rows"] == 1
    assert response.json()["items"][0]["calendar"]["last_checked_at"] is not None


async def test_probe_config_not_connected_skips_network() -> None:
    health = await tenants_api._probe_config(_make_config(None), "primary")
    assert health.status == CalendarHealthStatus.not_connected
    assert health.connected is False


async def test_probe_config_connected(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeService:
        async def check_availability(self, start: object, end: object) -> list:
            return []

    monkeypatch.setattr(
        tenants_api.CalendarService,
        "from_tenant_config",
        classmethod(lambda cls, config: _FakeService()),
    )
    health = await tenants_api._probe_config(_make_config("tok"), "primary")
    assert health.status == CalendarHealthStatus.connected
    assert health.connected is True
    assert health.last_checked_at is not None


async def test_probe_config_error_on_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _FakeService:
        async def check_availability(self, start: object, end: object) -> list:
            raise CalendarUnavailableError("Google Calendar returned HTTP 401")

    monkeypatch.setattr(
        tenants_api.CalendarService,
        "from_tenant_config",
        classmethod(lambda cls, config: _FakeService()),
    )
    health = await tenants_api._probe_config(_make_config("tok"), "primary")
    assert health.status == CalendarHealthStatus.error
    assert health.connected is False
    assert "401" in (health.detail or "")


# --------------------------------------------------------------------------
# Per-tenant health endpoint + OpenAPI
# --------------------------------------------------------------------------


async def test_calendar_health_unknown_tenant_is_404(client: AsyncClient) -> None:
    from secretaria.core.database import get_session
    from secretaria.main import app

    class _FakeSession:
        async def get(self, *args: object, **kwargs: object) -> None:
            return None

    async def _fake_get_session():
        yield _FakeSession()

    # Override the session dependency so session.get(Tenant, id) -> None without
    # touching Postgres; the route should 404 before any calendar probe.
    app.dependency_overrides[get_session] = _fake_get_session
    try:
        response = await client.get(
            f"{ENDPOINT}/{uuid4()}/calendar/health",
            headers={"X-Admin-Token": GOOD_TOKEN},
        )
        assert response.status_code == 404
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_endpoints_in_openapi_schema(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    schema = response.json()

    assert ENDPOINT in schema["paths"]
    get = schema["paths"][ENDPOINT]["get"]
    assert "admin-tenants" in get["tags"]
    assert f"{ENDPOINT}/{{tenant_id}}/calendar/health" in schema["paths"]
    # The single-tenant delete route is registered on the same collection path.
    assert "delete" in schema["paths"][f"{ENDPOINT}/{{tenant_id}}"]


# --------------------------------------------------------------------------
# Single-tenant delete (DELETE /admin/tenants/{id})
# --------------------------------------------------------------------------
#
# Like the calendar-health 404 test, these override get_session with a fake so
# nothing touches Postgres. The fake records what delete_tenant's service reads
# (session.get + session.scalar counts) and does (session.delete + commit).


class _FakeSession:
    """Minimal AsyncSession stand-in for the delete path.

    `tenant` is what `get` returns; `counts` are returned by successive `scalar`
    calls (conversations, patients, appointments — in that order). Records the
    deleted object and whether commit ran so tests can assert the effect.
    """

    def __init__(self, tenant: object, counts: tuple[int, ...] = (0, 0, 0)) -> None:
        self._tenant = tenant
        self._counts = list(counts)
        self.deleted: object = None
        self.committed = False

    async def get(self, *args: object, **kwargs: object) -> object:
        return self._tenant

    async def scalar(self, *args: object, **kwargs: object) -> int:
        return self._counts.pop(0)

    async def delete(self, obj: object) -> None:
        self.deleted = obj

    async def commit(self) -> None:
        self.committed = True


def _override_session(session: _FakeSession):
    """Context-managing helper: install/remove a get_session override."""
    from secretaria.core.database import get_session
    from secretaria.main import app

    async def _fake_get_session():
        yield session

    app.dependency_overrides[get_session] = _fake_get_session
    return get_session, app


async def test_delete_tenant_missing_is_404(client: AsyncClient) -> None:
    session = _FakeSession(tenant=None)
    get_session, app = _override_session(session)
    try:
        response = await client.delete(
            f"{ENDPOINT}/{uuid4()}", headers={"X-Admin-Token": GOOD_TOKEN}
        )
        assert response.status_code == 404
        assert session.committed is False
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_delete_tenant_with_conversations_is_409(client: AsyncClient) -> None:
    # A tenant that still has conversation history must be REFUSED, never wiped.
    session = _FakeSession(tenant=_make_tenant(), counts=(3, 0, 0))
    get_session, app = _override_session(session)
    try:
        response = await client.delete(
            f"{ENDPOINT}/{uuid4()}", headers={"X-Admin-Token": GOOD_TOKEN}
        )
        assert response.status_code == 409
        assert "tenant_has_conversation_data" in response.text
        assert session.deleted is None  # nothing deleted
        assert session.committed is False
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_delete_tenant_clean_succeeds(client: AsyncClient) -> None:
    tenant = _make_tenant()
    session = _FakeSession(tenant=tenant, counts=(0, 0, 0))
    get_session, app = _override_session(session)
    try:
        response = await client.delete(
            f"{ENDPOINT}/{tenant.id}", headers={"X-Admin-Token": GOOD_TOKEN}
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["deleted"] is True
        assert body["tenant_id"] == str(tenant.id)
        assert session.deleted is tenant  # the tenant row was removed
        assert session.committed is True
    finally:
        app.dependency_overrides.pop(get_session, None)


async def test_delete_tenant_requires_admin_token(client: AsyncClient) -> None:
    response = await client.delete(f"{ENDPOINT}/{uuid4()}")
    assert response.status_code == 403
