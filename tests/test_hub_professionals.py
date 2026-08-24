"""Tests for api/hub/professionals.py — multi_professional CRUD + gating.

Uses the `client` fixture from conftest.py (ASGITransport, no real DB) with
`get_session` overridden to a real in-memory sqlite DB and `get_current_tenant`
overridden to a canned Tenant — the same override-the-FastAPI-dependency
pattern as test_internal.py, but with a REAL session (not a stub) since these
endpoints actually write rows. `get_entitlements` is monkeypatched at the
point professionals.py imported it, so no brain-api call is ever made.
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
from sqlalchemy import select  # noqa: E402
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

ENDPOINT = "/tenants/me/professionals"


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


def _entitled_fake(**overrides):
    async def _fake(tenant_id, redis=None):
        return _summary(**overrides)

    return _fake


async def _never_called_fake(tenant_id, redis=None):
    raise AssertionError("get_entitlements must not be called for this operation")


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


async def _seed_professional(db, tenant: Tenant, **overrides) -> Professional:
    fields = dict(tenant_id=tenant.id, name="Dra. Ana", google_calendar_id=None, is_active=True)
    fields.update(overrides)
    async with db() as session:
        prof = Professional(**fields)
        session.add(prof)
        await session.commit()
        await session.refresh(prof)
        return prof


# --------------------------------------------------------------------------
# GET — always allowed, no entitlement check
# --------------------------------------------------------------------------


async def test_list_is_allowed_without_entitlement_check(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    assert response.json() == []


async def test_list_shape_is_whitelisted(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    await _seed_professional(db, tenant, name="Dra. Ana", google_calendar_id="ana-cal")

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    row = response.json()[0]
    assert set(row.keys()) == {
        "id",
        "name",
        "google_calendar_id",
        "is_active",
        "created_at",
        "specialty",
        "about",
        "context_doctor_message",
        "business_hours",
        # Additive since the null/empty round: says whether the (possibly empty)
        # value above is inheritance or an own override. Asserted here on
        # purpose — this exact-key-set check is what keeps a frontend type from
        # inventing a property the backend never sends.
        "business_hours_inherited",
        "appointment_types",
        "appointment_types_inherited",
        "has_calendar",
        # Additive: whose Calendar credential covers this row. Same anti-drift
        # reason as the two flags above — a frontend type may only declare keys
        # this set contains.
        "calendar_source",
        "has_hours",
        "has_services",
        "complete",
    }
    assert row["name"] == "Dra. Ana"
    assert row["google_calendar_id"] == "ana-cal"


async def test_list_includes_onboarding_completeness(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    await _seed_professional(db, tenant, name="Bare", google_calendar_id=None)

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    row = response.json()[0]
    # No calendar/hours/services configured anywhere -> incomplete on every axis.
    assert row["has_calendar"] is False
    assert row["has_hours"] is False
    assert row["has_services"] is False
    assert row["complete"] is False


async def test_list_completeness_reflects_config(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.services.tenant_config import set_google_refresh_token

    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    await _seed_professional(
        db,
        tenant,
        name="Configured",
        business_hours={"monday": [{"start": "08:00", "end": "12:00"}]},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
    )
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "tenant-refresh-token")
        await session.commit()

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    row = response.json()[0]
    assert row["has_calendar"] is True  # tenant-level token covers it
    assert row["has_hours"] is True
    assert row["has_services"] is True
    assert row["complete"] is True


# --------------------------------------------------------------------------
# POST — entitlement + limit gating
# --------------------------------------------------------------------------


async def test_create_active_professional_succeeds_when_entitled(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        professionals_api,
        "get_entitlements",
        _entitled_fake(
            addons={**_ALL_ADDONS_OFF, "multi_professional": True}, limits={"professionals": 5}
        ),
    )
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana"})
    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Dra. Ana"
    assert body["is_active"] is True


async def test_create_not_entitled_returns_403(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _entitled_fake())  # addon off
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana"})
    assert response.status_code == 403
    assert response.json()["detail"] == "multi_professional_not_entitled"


async def test_create_over_limit_returns_409(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_professional(db, tenant, name="Existing", is_active=True)
    monkeypatch.setattr(
        professionals_api,
        "get_entitlements",
        _entitled_fake(
            addons={**_ALL_ADDONS_OFF, "multi_professional": True}, limits={"professionals": 1}
        ),
    )
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana"})
    assert response.status_code == 409
    assert response.json()["detail"] == "professional_limit_reached"


async def test_create_entitlement_fetch_failure_fails_closed_503(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_none(tenant_id, redis=None):
        return None

    monkeypatch.setattr(professionals_api, "get_entitlements", _fake_none)
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana"})
    assert response.status_code == 503


async def test_create_inactive_professional_skips_entitlement_check(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    response = await client.post(ENDPOINT, json={"name": "Dra. Ana", "is_active": False})
    assert response.status_code == 201
    assert response.json()["is_active"] is False


# --------------------------------------------------------------------------
# PATCH — activation is gated, rename/deactivate are not
# --------------------------------------------------------------------------


async def test_patch_rename_without_activation_skips_entitlement_check(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, name="Old Name", is_active=True)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"name": "New Name"})
    assert response.status_code == 200
    assert response.json()["name"] == "New Name"


async def test_patch_deactivate_skips_entitlement_check(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, is_active=True)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False


async def test_patch_activate_not_entitled_returns_403(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, is_active=False)
    monkeypatch.setattr(professionals_api, "get_entitlements", _entitled_fake())  # addon off

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"is_active": True})
    assert response.status_code == 403
    assert response.json()["detail"] == "multi_professional_not_entitled"


async def test_patch_activate_over_limit_returns_409(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_professional(db, tenant, name="Existing active", is_active=True)
    prof = await _seed_professional(db, tenant, name="To activate", is_active=False)
    monkeypatch.setattr(
        professionals_api,
        "get_entitlements",
        _entitled_fake(
            addons={**_ALL_ADDONS_OFF, "multi_professional": True}, limits={"professionals": 1}
        ),
    )

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"is_active": True})
    assert response.status_code == 409
    assert response.json()["detail"] == "professional_limit_reached"


async def test_patch_activate_within_limit_succeeds(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, is_active=False)
    monkeypatch.setattr(
        professionals_api,
        "get_entitlements",
        _entitled_fake(
            addons={**_ALL_ADDONS_OFF, "multi_professional": True}, limits={"professionals": 5}
        ),
    )

    response = await client.patch(f"{ENDPOINT}/{prof.id}", json={"is_active": True})
    assert response.status_code == 200
    assert response.json()["is_active"] is True


async def test_patch_unknown_id_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    response = await client.patch(f"{ENDPOINT}/{uuid4()}", json={"name": "Ghost"})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# PUT /{professional_id}/config — NEVER entitlement/limit gated
# --------------------------------------------------------------------------


async def test_put_config_updates_fields_and_is_never_entitlement_gated(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.put(
        f"{ENDPOINT}/{prof.id}/config",
        json={
            "business_hours": {"monday": [{"start": "08:00", "end": "12:00"}]},
            "appointment_types": [
                {"name": "Consulta", "duration_min": 30, "is_active": True}
            ],
            "specialty": "Cardiologia",
            "about": "Atende há 10 anos.",
            "context_doctor_message": "Prefere retornos rápidos.",
            "google_calendar_id": "ana-own-cal",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["specialty"] == "Cardiologia"
    assert body["about"] == "Atende há 10 anos."
    assert body["context_doctor_message"] == "Prefere retornos rápidos."
    assert body["google_calendar_id"] == "ana-own-cal"
    assert body["business_hours"] == {"monday": [{"start": "08:00", "end": "12:00"}]}
    assert body["has_hours"] is True
    assert body["has_services"] is True


async def test_put_config_partial_update_leaves_other_fields_untouched(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, name="Original Name")
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.put(f"{ENDPOINT}/{prof.id}/config", json={"specialty": "Pediatria"})
    assert response.status_code == 200
    body = response.json()
    assert body["specialty"] == "Pediatria"
    assert body["name"] == "Original Name"  # PUT /config never touches `name`


async def test_put_config_appointment_type_requirements_round_trip(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same requirements validation/round-trip as the tenant-level
    PUT /tenants/me/config (test_hub_config.py) - schemas/config.py's
    AppointmentType is shared, not reimplemented for professionals."""
    prof = await _seed_professional(db, tenant)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.put(
        f"{ENDPOINT}/{prof.id}/config",
        json={
            "appointment_types": [
                {
                    "name": "Consulta",
                    "duration_min": 30,
                    "is_active": True,
                    "requirements": ["Jejum de 8 horas", "  Trazer exames  ", ""],
                }
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["appointment_types"][0]["requirements"] == ["Jejum de 8 horas", "Trazer exames"]


async def test_put_config_rejects_overlapping_hours(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant)
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)

    response = await client.put(
        f"{ENDPOINT}/{prof.id}/config",
        json={
            "business_hours": {
                "monday": [
                    {"start": "08:00", "end": "12:00"},
                    {"start": "11:00", "end": "14:00"},
                ]
            }
        },
    )
    assert response.status_code == 422


async def test_put_config_unknown_id_is_404(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(professionals_api, "get_entitlements", _never_called_fake)
    response = await client.put(f"{ENDPOINT}/{uuid4()}/config", json={"specialty": "Ghost"})
    assert response.status_code == 404


# --------------------------------------------------------------------------
# POST /{professional_id}/calendar — shared_account secondary Calendar
# creation (docs/CHECKPOINT_google_calendar_modes.md item 3). Never gated by
# entitlements (no monkeypatch of get_entitlements needed/asserted here -
# these tests would fail loudly with a real brain-api call if the endpoint
# ever grew one, same as PUT /config's tests never touching it either).
# --------------------------------------------------------------------------


class _FakeSecondaryCalendar:
    """Stands in for CalendarService so these tests never call real Google
    APIs. Monkeypatched onto services.tenant_config.CalendarService."""

    calls: list[str] = []
    raise_scope_error = False

    def __init__(self, config) -> None:
        self._config = config

    async def create_secondary_calendar(self, summary: str) -> dict:
        from secretaria.services.calendar import GoogleScopeInsufficientError

        _FakeSecondaryCalendar.calls.append(summary)
        if _FakeSecondaryCalendar.raise_scope_error:
            raise GoogleScopeInsufficientError("insufficient scope")
        return {"id": f"secondary-{len(_FakeSecondaryCalendar.calls)}@group.calendar.google.com"}


class _FakeCalendarServiceFactory:
    @staticmethod
    def from_tenant_config(config):
        return _FakeSecondaryCalendar(config)


def _patch_calendar_service(
    monkeypatch: pytest.MonkeyPatch, *, raise_scope_error: bool = False
) -> None:
    from secretaria.services import tenant_config as tc

    _FakeSecondaryCalendar.calls = []
    _FakeSecondaryCalendar.raise_scope_error = raise_scope_error
    monkeypatch.setattr(tc, "CalendarService", _FakeCalendarServiceFactory)


async def test_create_calendar_creates_and_persists_when_clinic_connected(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.services.tenant_config import set_google_refresh_token

    prof = await _seed_professional(db, tenant, name="Dra. Ana", google_calendar_id=None)
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "clinic-refresh-token")
        await session.commit()
    _patch_calendar_service(monkeypatch)

    response = await client.post(f"{ENDPOINT}/{prof.id}/calendar")
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is True
    assert body["professional_id"] == str(prof.id)
    assert body["google_calendar_id"] == "secondary-1@group.calendar.google.com"
    assert _FakeSecondaryCalendar.calls == ["Dra. Ana — Clinic"]

    async with db() as session:
        refreshed = await session.get(Professional, prof.id)
        assert refreshed.google_calendar_id == "secondary-1@group.calendar.google.com"


async def test_create_calendar_is_idempotent_when_already_set(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, google_calendar_id="already-there")
    _patch_calendar_service(monkeypatch)

    response = await client.post(f"{ENDPOINT}/{prof.id}/calendar")
    assert response.status_code == 200
    body = response.json()
    assert body["created"] is False
    assert body["google_calendar_id"] == "already-there"
    assert _FakeSecondaryCalendar.calls == []  # calendars.insert never called


async def test_create_calendar_clinic_not_connected_is_422_structured(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    prof = await _seed_professional(db, tenant, google_calendar_id=None)
    _patch_calendar_service(monkeypatch)

    response = await client.post(f"{ENDPOINT}/{prof.id}/calendar")
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "clinic_calendar_not_connected"
    assert _FakeSecondaryCalendar.calls == []


async def test_create_calendar_scope_insufficient_maps_to_409_reconnect(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.services.tenant_config import set_google_refresh_token

    prof = await _seed_professional(db, tenant, google_calendar_id=None)
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "stale-refresh-token")
        await session.commit()
    _patch_calendar_service(monkeypatch, raise_scope_error=True)

    response = await client.post(f"{ENDPOINT}/{prof.id}/calendar")
    assert response.status_code == 409
    body = response.json()
    assert body["detail"]["code"] == "google_reconnect_required"
    assert "Reconecte" in body["detail"]["message"]

    async with db() as session:
        refreshed = await session.get(Professional, prof.id)
        assert refreshed.google_calendar_id is None  # untouched on failure


async def test_create_calendar_unowned_professional_is_404(client: AsyncClient, db) -> None:
    async with db() as session:
        other_tenant = Tenant(id=uuid4(), clinic_name="Other", phone_number_id="other-num-3")
        session.add(other_tenant)
        await session.commit()
        foreign = Professional(tenant_id=other_tenant.id, name="Foreign Doc", is_active=True)
        session.add(foreign)
        await session.commit()
        await session.refresh(foreign)

    response = await client.post(f"{ENDPOINT}/{foreign.id}/calendar")
    assert response.status_code == 404


async def test_create_calendar_unknown_id_is_404(client: AsyncClient) -> None:
    response = await client.post(f"{ENDPOINT}/{uuid4()}/calendar")
    assert response.status_code == 404


# --------------------------------------------------------------------------
# POST /calendars - the BULK secondary-calendar run (shared_account mode).
#
# The gap these cover: flipping a clinic to "Conta unica" and saving used to
# create nothing at all, because the only trigger was one button per doctor.
# --------------------------------------------------------------------------

BULK_ENDPOINT = f"{ENDPOINT}/calendars"


async def _connect_clinic(db, tenant, token: str = "clinic-refresh-token") -> None:
    from secretaria.services.tenant_config import set_google_refresh_token

    async with db() as session:
        await set_google_refresh_token(session, tenant.id, token)
        await session.commit()


async def test_bulk_creates_one_calendar_per_active_professional(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_professional(db, tenant, name="Dra. Ana")
    await _seed_professional(db, tenant, name="Dr. Bruno")
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)

    response = await client.post(BULK_ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert (body["created"], body["already"], body["failed"]) == (2, 0, 0)
    assert {item["name"] for item in body["items"]} == {"Dra. Ana", "Dr. Bruno"}
    assert all(item["google_calendar_id"] for item in body["items"])
    assert sorted(_FakeSecondaryCalendar.calls) == [
        "Dr. Bruno \u2014 Clinic",
        "Dra. Ana \u2014 Clinic",
    ]

    async with db() as session:
        rows = list(await session.scalars(select(Professional)))
        assert all(row.google_calendar_id for row in rows)


async def test_bulk_skips_inactive_professionals(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_professional(db, tenant, name="Dra. Ana")
    retired = await _seed_professional(db, tenant, name="Dr. Antigo", is_active=False)
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)

    response = await client.post(BULK_ENDPOINT)
    assert response.status_code == 200
    assert response.json()["created"] == 1
    assert _FakeSecondaryCalendar.calls == ["Dra. Ana \u2014 Clinic"]

    async with db() as session:
        assert (await session.get(Professional, retired.id)).google_calendar_id is None


async def test_bulk_is_idempotent_and_only_fills_the_gaps(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Re-running is safe: a doctor who already has one is never sent twice."""
    await _seed_professional(db, tenant, name="Dra. Ana", google_calendar_id="ana-already")
    await _seed_professional(db, tenant, name="Dr. Bruno")
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)

    first = (await client.post(BULK_ENDPOINT)).json()
    assert (first["created"], first["already"], first["failed"]) == (1, 1, 0)
    assert _FakeSecondaryCalendar.calls == ["Dr. Bruno \u2014 Clinic"]

    second = (await client.post(BULK_ENDPOINT)).json()
    assert (second["created"], second["already"], second["failed"]) == (0, 2, 0)
    # No SECOND insert for anybody - the whole point of idempotency.
    assert _FakeSecondaryCalendar.calls == ["Dr. Bruno \u2014 Clinic"]


async def test_bulk_without_a_connected_clinic_is_422_and_creates_nothing(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_professional(db, tenant, name="Dra. Ana")
    _patch_calendar_service(monkeypatch)

    response = await client.post(BULK_ENDPOINT)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "clinic_calendar_not_connected"
    assert _FakeSecondaryCalendar.calls == []

    async with db() as session:
        rows = list(await session.scalars(select(Professional)))
        assert all(row.google_calendar_id is None for row in rows)


async def test_bulk_scope_error_is_409_but_keeps_what_already_succeeded(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A calendar Google already created must not be orphaned by the 409.

    The scope failure is a property of the CLINIC's token, so it aborts the
    run - but only after committing the rows that got a real calendar, which
    exist inside Google whether or not this response is an error.
    """
    await _seed_professional(db, tenant, name="Dra. Ana")
    await _seed_professional(db, tenant, name="Dr. Bruno")
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)

    # Fail on the SECOND professional only.
    original = _FakeSecondaryCalendar.create_secondary_calendar

    async def _fail_after_first(self, summary: str) -> dict:
        from secretaria.services.calendar import GoogleScopeInsufficientError

        if _FakeSecondaryCalendar.calls:
            raise GoogleScopeInsufficientError("insufficient scope")
        return await original(self, summary)

    monkeypatch.setattr(_FakeSecondaryCalendar, "create_secondary_calendar", _fail_after_first)

    response = await client.post(BULK_ENDPOINT)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "google_reconnect_required"

    async with db() as session:
        rows = list(await session.scalars(select(Professional).order_by(Professional.created_at)))
        assert rows[0].google_calendar_id is not None  # committed despite the 409
        assert rows[1].google_calendar_id is None


async def test_bulk_reports_a_single_row_failure_and_keeps_going(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One doctor's outage must not cost the others their calendars."""
    from secretaria.services.calendar import CalendarUnavailableError

    await _seed_professional(db, tenant, name="Dra. Ana")
    await _seed_professional(db, tenant, name="Dr. Bruno")
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)

    original = _FakeSecondaryCalendar.create_secondary_calendar

    async def _fail_for_ana(self, summary: str) -> dict:
        if summary.startswith("Dra. Ana"):
            _FakeSecondaryCalendar.calls.append(summary)
            raise CalendarUnavailableError("Google Calendar unreachable")
        return await original(self, summary)

    monkeypatch.setattr(_FakeSecondaryCalendar, "create_secondary_calendar", _fail_for_ana)

    response = await client.post(BULK_ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert (body["created"], body["failed"]) == (1, 1)
    failed_row = next(item for item in body["items"] if item["name"] == "Dra. Ana")
    assert failed_row["error"] == "calendar_unavailable"
    assert failed_row["google_calendar_id"] is None
    # A CODE, never Google's own message - an error body can carry the
    # clinic's account details.
    assert "unreachable" not in str(body)

    async with db() as session:
        bruno = next(
            row for row in await session.scalars(select(Professional)) if row.name == "Dr. Bruno"
        )
        assert bruno.google_calendar_id is not None


async def test_bulk_with_an_empty_roster_is_a_clean_no_op(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)

    response = await client.post(BULK_ENDPOINT)
    assert response.status_code == 200
    assert response.json() == {"created": 0, "already": 0, "failed": 0, "items": []}


async def test_bulk_route_does_not_shadow_the_per_professional_one(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`/calendars` and `/{id}/calendar` are different routes, not a collision."""
    prof = await _seed_professional(db, tenant, name="Dra. Ana")
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)

    single = await client.post(f"{ENDPOINT}/{prof.id}/calendar")
    assert single.status_code == 200
    assert single.json()["professional_id"] == str(prof.id)


# --------------------------------------------------------------------------
# A professional who JOINS a shared_account clinic gets their agenda too.
# --------------------------------------------------------------------------


async def _shared_account(db, tenant) -> None:
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.google_calendar_mode = "shared_account"
        await session.commit()
    tenant.google_calendar_mode = "shared_account"


def _allow_multi_professional(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        professionals_api, "get_entitlements", _entitled_fake(addons={"multi_professional": True})
    )
    monkeypatch.setattr(professionals_api, "is_entitled", lambda summary, key: True)


async def test_creating_a_professional_in_shared_account_mode_creates_their_calendar(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _shared_account(db, tenant)
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)
    _allow_multi_professional(monkeypatch)

    response = await client.post(ENDPOINT, json={"name": "Dr. Novo"})
    assert response.status_code == 201
    assert _FakeSecondaryCalendar.calls == ["Dr. Novo \u2014 Clinic"]

    async with db() as session:
        created = next(iter(await session.scalars(select(Professional))))
        assert created.google_calendar_id is not None


async def test_creating_a_professional_in_per_professional_mode_creates_no_calendar(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default mode is untouched: nobody's Google account is written to."""
    await _connect_clinic(db, tenant)
    _patch_calendar_service(monkeypatch)
    _allow_multi_professional(monkeypatch)

    response = await client.post(ENDPOINT, json={"name": "Dr. Novo"})
    assert response.status_code == 201
    assert _FakeSecondaryCalendar.calls == []


async def test_a_google_failure_never_blocks_creating_the_professional(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The row is real regardless of Google's mood; the retries are idempotent."""
    await _shared_account(db, tenant)
    # Deliberately NO clinic token: the common mid-onboarding state.
    _patch_calendar_service(monkeypatch)
    _allow_multi_professional(monkeypatch)

    response = await client.post(ENDPOINT, json={"name": "Dr. Novo"})
    assert response.status_code == 201

    async with db() as session:
        created = next(iter(await session.scalars(select(Professional))))
        assert created.name == "Dr. Novo"
        assert created.google_calendar_id is None
