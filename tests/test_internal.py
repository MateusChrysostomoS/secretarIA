"""Tests for the internal service-to-service API (/internal/*) and its X-Internal-Api-Key guard.

The auth tests touch no DB — `require_internal_api_key` rejects before the handler runs.
The data tests override `get_session` with a fake (no Postgres), so they exercise the route
shape, the lean projection (internal Google ids must NOT leak), and that the query binds the
path tenant_id (scoping). The guard mirrors the require_admin tests in test_admin.py.
"""

import os
from datetime import UTC, datetime
from uuid import uuid4

# Set the internal key BEFORE secretaria imports so Settings picks it up.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

import pytest  # noqa: E402
from httpx import AsyncClient  # noqa: E402

from secretaria.models import Appointment, AppointmentStatus, Patient  # noqa: E402

GOOD_KEY = "test-internal-key"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
APPTS = f"/internal/tenants/{TENANT_ID}/appointments"
PATIENTS = f"/internal/tenants/{TENANT_ID}/patients"

# Sentinels that must NEVER appear in an /internal response.
GOOGLE_EVENT_ID = "GOOGLE-EVENT-SHOULD-NOT-LEAK"
GOOGLE_EVENT_LINK = "https://calendar.google.com/SHOULD-NOT-LEAK"


def _make_appointment(**overrides: object) -> Appointment:
    fields: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "patient_id": uuid4(),
        "conversation_id": uuid4(),
        "google_event_id": GOOGLE_EVENT_ID,
        "google_event_link": GOOGLE_EVENT_LINK,
        "appointment_type": "Primeira consulta",
        "start_at": datetime(2026, 6, 1, 14, 0, tzinfo=UTC),
        "end_at": datetime(2026, 6, 1, 14, 30, tzinfo=UTC),
        "phone": "+5511999999999",
        "status": AppointmentStatus.SCHEDULED,
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return Appointment(**fields)


def _make_patient(**overrides: object) -> Patient:
    fields: dict[str, object] = {
        "id": uuid4(),
        "tenant_id": uuid4(),
        "wa_id": "5511999999999",
        "name": "Maria Souza",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    fields.update(overrides)
    return Patient(**fields)


class _FakeResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list[object]:
        return list(self._rows)


class _FakeSession:
    """Records executed statements and returns canned rows (never touches Postgres)."""

    def __init__(self, rows: list[object]) -> None:
        self._rows = rows
        self.statements: list[object] = []

    async def execute(self, stmt: object) -> _FakeResult:
        self.statements.append(stmt)
        return _FakeResult(self._rows)


def _override_session(rows: list[object]) -> _FakeSession:
    from secretaria.core.database import get_session
    from secretaria.main import app

    session = _FakeSession(rows)

    async def _fake_get_session():
        yield session

    app.dependency_overrides[get_session] = _fake_get_session
    return session


def _clear_override() -> None:
    from secretaria.core.database import get_session
    from secretaria.main import app

    app.dependency_overrides.pop(get_session, None)


# --------------------------------------------------------------------------
# Auth guard (no DB — the dependency rejects before the handler)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("endpoint", [APPTS, PATIENTS])
async def test_missing_key_is_unauthorized(client: AsyncClient, endpoint: str) -> None:
    """No X-Internal-Api-Key => 401 (no anonymous access)."""
    response = await client.get(endpoint)
    assert response.status_code == 401


async def test_wrong_key_is_unauthorized(client: AsyncClient) -> None:
    response = await client.get(APPTS, headers={"X-Internal-Api-Key": "wrong-key"})
    assert response.status_code == 401


async def test_unconfigured_key_is_forbidden(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Server key unset => 403 (fail closed; never accept-all)."""
    from secretaria.config import get_settings

    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = await client.get(APPTS, headers={"X-Internal-Api-Key": "anything"})
        assert response.status_code == 403
    finally:
        monkeypatch.setenv("INTERNAL_API_KEY", GOOD_KEY)
        get_settings.cache_clear()


# --------------------------------------------------------------------------
# Data path: shape, lean projection (no Google ids), tenant scoping
# --------------------------------------------------------------------------


async def test_appointments_shape_and_no_google_ids(client: AsyncClient) -> None:
    _override_session([_make_appointment()])
    try:
        response = await client.get(APPTS, headers={"X-Internal-Api-Key": GOOD_KEY})
        assert response.status_code == 200
        body = response.json()
        assert set(body.keys()) == {"data"}
        row = body["data"][0]
        assert set(row.keys()) == {
            "id",
            "patient_id",
            "appointment_type",
            "start_at",
            "end_at",
            "status",
            "phone",
        }
        assert row["status"] == "scheduled"
        assert row["appointment_type"] == "Primeira consulta"
        # Internal Google identifiers / conversation id must NOT cross the boundary.
        assert GOOGLE_EVENT_ID not in response.text
        assert GOOGLE_EVENT_LINK not in response.text
        assert "google_event_id" not in response.text
        assert "google_event_link" not in response.text
        assert "conversation_id" not in response.text
    finally:
        _clear_override()


async def test_appointments_query_is_tenant_scoped(client: AsyncClient) -> None:
    """The executed SELECT must bind the path tenant_id (cross-tenant reads impossible)."""
    session = _override_session([])
    try:
        response = await client.get(APPTS, headers={"X-Internal-Api-Key": GOOD_KEY})
        assert response.status_code == 200
        assert response.json() == {"data": []}
        assert session.statements, "handler did not execute a query"
        bound = {str(v) for v in session.statements[0].compile().params.values()}
        assert TENANT_ID in bound
    finally:
        _clear_override()


async def test_patients_shape(client: AsyncClient) -> None:
    _override_session([_make_patient()])
    try:
        response = await client.get(PATIENTS, headers={"X-Internal-Api-Key": GOOD_KEY})
        assert response.status_code == 200
        row = response.json()["data"][0]
        assert set(row.keys()) == {"id", "name", "wa_id", "created_at"}
        assert row["wa_id"] == "5511999999999"
        assert row["name"] == "Maria Souza"
    finally:
        _clear_override()


async def test_patients_query_is_tenant_scoped(client: AsyncClient) -> None:
    session = _override_session([])
    try:
        response = await client.get(PATIENTS, headers={"X-Internal-Api-Key": GOOD_KEY})
        assert response.status_code == 200
        bound = {str(v) for v in session.statements[0].compile().params.values()}
        assert TENANT_ID in bound
    finally:
        _clear_override()


@pytest.mark.parametrize("bad_limit", [0, 10000])
async def test_pagination_bounds_are_rejected(client: AsyncClient, bad_limit: int) -> None:
    response = await client.get(
        f"{APPTS}?limit={bad_limit}", headers={"X-Internal-Api-Key": GOOD_KEY}
    )
    assert response.status_code == 422


async def test_internal_routes_in_openapi(client: AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/internal/tenants/{tenant_id}/appointments" in paths
    assert "/internal/tenants/{tenant_id}/patients" in paths
    get = paths["/internal/tenants/{tenant_id}/appointments"]["get"]
    assert "internal" in get["tags"]
