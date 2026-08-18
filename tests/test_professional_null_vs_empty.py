"""NULL versus EMPTY on a professional's own hours/services — the whole table.

WHAT THIS PINS DOWN
-------------------
`Professional.business_hours` / `Professional.appointment_types` have three
states, and the resolvers in `services/tenant_config.py` used to collapse the
first two:

    NULL      -> inherit the tenant's legacy single-professional column
    {} / []   -> an OWN override that is empty. Inherits NOTHING.
    non-empty -> an OWN override with content.

Under the old truthiness test (`professional.business_hours or
tenant.business_hours`) a clinic that closed every day, or removed every
service, silently got the tenant's OLD hours and catalog back on the
patient-facing path — while the hub screen showed an empty form. The bot offered
appointments nobody had agreed to offer.

Every consumer that reads those two columns is covered here, because the failure
mode was that ONE of them disagreed with the others:

  - the two resolvers themselves, as a table (null / empty / own / inactive)
  - completeness + the activation gate (empty must block, honestly)
  - the deterministic flow snapshot (workers/tasks.py)
  - the LLM's runtime config (load_tenant_config -> ai/prompts.py)
  - the Calendar chain (resolve_professional_calendar ->
    CalendarService.for_professional, where a second falsy-fallback lived)
  - the Pix deposit price lookup (services/payments/deposit_lifecycle.py)
  - the hub wire: PUT/GET round-tripping all four states, plus the additive
    `*_inherited` flags and old-client compatibility

Two tenants and two professionals appear throughout, so "no fallback across
tenants or across professionals" is proven rather than assumed.

Synthetic data only: clinic names, professional names and service names are
invented, and there is no phone number, patient or real id anywhere.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402
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
from structlog.testing import capture_logs  # noqa: E402

from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.api.hub import professionals as professionals_api  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Appointment, Professional, Tenant  # noqa: E402
from secretaria.services.calendar import CalendarService  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.payments import deposit_lifecycle  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    can_activate_professional_aware,
    load_tenant_config,
    professional_appointment_types,
    professional_business_hours,
    professional_completeness,
    professional_completeness_item,
    professional_inherits_appointment_types,
    professional_inherits_business_hours,
    resolve_professional_calendar,
    set_google_refresh_token,
)
from secretaria.workers.tasks import _flow_tenant_snapshot  # noqa: E402

# --- synthetic config ------------------------------------------------------

TENANT_HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}
TENANT_TYPES = [{"name": "Consulta da clinica", "duration_min": 30, "is_active": True}]

OWN_HOURS = {"tuesday": [{"start": "14:00", "end": "18:00"}]}
OWN_TYPES = [{"name": "Retorno proprio", "duration_min": 45, "is_active": True}]

ENDPOINT = "/tenants/me/professionals"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


async def make_tenant(session, name="Clinica A", **overrides) -> Tenant:
    fields = dict(
        id=uuid4(),
        clinic_name=name,
        phone_number_id=str(uuid4())[:12],
        business_hours=TENANT_HOURS,
        appointment_types=TENANT_TYPES,
    )
    fields.update(overrides)
    tenant = Tenant(**fields)
    session.add(tenant)
    await session.flush()
    return tenant


async def make_professional(session, tenant: Tenant, name="Dra. Ana", **overrides) -> Professional:
    """NOTE the defaults: both JSON columns are absent, i.e. NULL = inherit.

    Every test that wants an EMPTY override passes it explicitly, so the
    difference between the two is never accidental in this file.
    """
    fields = dict(id=uuid4(), tenant_id=tenant.id, name=name, is_active=True)
    fields.update(overrides)
    professional = Professional(**fields)
    session.add(professional)
    await session.flush()
    return professional


# ---------------------------------------------------------------------------
# 1. The resolvers, as a table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "own_hours,own_types,expected_hours,expected_types",
    [
        # NULL inherits the clinic's legacy columns.
        (None, None, TENANT_HOURS, TENANT_TYPES),
        # EMPTY is an own override and inherits nothing.
        ({}, [], {}, []),
        # Own content wins outright.
        (OWN_HOURS, OWN_TYPES, OWN_HOURS, OWN_TYPES),
        # Mixed: one field inherits, the other is explicitly empty. The two
        # columns are independent and must not drag each other along.
        (None, [], TENANT_HOURS, []),
        ({}, None, {}, TENANT_TYPES),
    ],
    ids=["null-inherits", "empty-stays-empty", "own-wins", "null+empty", "empty+null"],
)
async def test_resolution_table(session, own_hours, own_types, expected_hours, expected_types):
    tenant = await make_tenant(session)
    professional = await make_professional(
        session, tenant, business_hours=own_hours, appointment_types=own_types
    )

    assert professional_business_hours(professional, tenant) == expected_hours
    assert professional_appointment_types(professional, tenant) == expected_types


@pytest.mark.parametrize(
    "hours_value,types_value,inherits",
    [(None, None, True), ({}, [], False), (OWN_HOURS, OWN_TYPES, False)],
    ids=["null", "empty", "own"],
)
async def test_inherits_predicates_are_is_none_and_nothing_else(
    session, hours_value, types_value, inherits
):
    """The predicates the API flags and the resolvers share. `{}` is NOT inheritance."""
    tenant = await make_tenant(session)
    professional = await make_professional(
        session, tenant, business_hours=hours_value, appointment_types=types_value
    )

    assert professional_inherits_business_hours(professional) is inherits
    assert professional_inherits_appointment_types(professional) is inherits


async def test_empty_weekday_lists_in_an_own_override_still_count_as_own(session):
    """`{"monday": []}` is an own override that resolves to no hours.

    Active-window filtering drops the closed day, so the RESOLVED value is `{}`
    — but the column is not NULL, so nothing is inherited. This is the exact
    shape the UI produces when the user turns every day off one by one instead
    of choosing "inherit".
    """
    tenant = await make_tenant(session)
    professional = await make_professional(
        session, tenant, business_hours={"monday": [], "tuesday": []}
    )

    assert professional_business_hours(professional, tenant) == {}
    assert professional_inherits_business_hours(professional) is False


async def test_inactive_entries_are_filtered_out_of_an_own_catalog(session):
    tenant = await make_tenant(session)
    professional = await make_professional(
        session,
        tenant,
        appointment_types=[
            {"name": "Visivel", "duration_min": 30, "is_active": True, "sort_order": 1},
            {"name": "Escondido", "duration_min": 30, "is_active": False, "sort_order": 0},
        ],
    )

    assert [t["name"] for t in professional_appointment_types(professional, tenant)] == ["Visivel"]


async def test_all_entries_inactive_resolves_empty_and_never_inherits(session):
    """An own catalog whose every entry is inactive offers nothing.

    The clinic did not remove the rows, it switched them all off — same
    intention, and the same result: no fallback to the tenant's legacy catalog.
    """
    tenant = await make_tenant(session)
    professional = await make_professional(
        session,
        tenant,
        appointment_types=[{"name": "Desativado", "duration_min": 30, "is_active": False}],
    )

    assert professional_appointment_types(professional, tenant) == []


# ---------------------------------------------------------------------------
# 2. Isolation: two tenants, two professionals
# ---------------------------------------------------------------------------


async def test_no_fallback_across_tenants_or_professionals(session):
    """Nobody inherits sideways: not from another clinic, not from a colleague."""
    tenant_a = await make_tenant(session, name="Clinica A")
    tenant_b = await make_tenant(
        session,
        name="Clinica B",
        business_hours={"friday": [{"start": "07:00", "end": "09:00"}]},
        appointment_types=[{"name": "Servico da B", "duration_min": 15, "is_active": True}],
    )

    # A1 has its own content, A2 is explicitly empty.
    a1 = await make_professional(
        session, tenant_a, name="A1", business_hours=OWN_HOURS, appointment_types=OWN_TYPES
    )
    a2 = await make_professional(
        session, tenant_a, name="A2", business_hours={}, appointment_types=[]
    )
    # B1 inherits ITS OWN tenant, never A's.
    b1 = await make_professional(session, tenant_b, name="B1")

    assert professional_business_hours(a1, tenant_a) == OWN_HOURS
    # A2 gets neither A1's hours nor tenant A's — it gets nothing.
    assert professional_business_hours(a2, tenant_a) == {}
    assert professional_appointment_types(a2, tenant_a) == []
    # B1 inherits tenant B's legacy columns, and only tenant B's.
    assert professional_business_hours(b1, tenant_b) == tenant_b.business_hours
    assert [t["name"] for t in professional_appointment_types(b1, tenant_b)] == ["Servico da B"]


# ---------------------------------------------------------------------------
# 3. Completeness and the activation gate — empty must block, out loud
# ---------------------------------------------------------------------------


async def test_completeness_empty_override_reports_missing_hours_and_services(session):
    tenant = await make_tenant(session)
    professional = await make_professional(
        session, tenant, business_hours={}, appointment_types=[]
    )
    await set_google_refresh_token(session, tenant.id, "1//synthetic-refresh-token")

    item = await professional_completeness_item(session, professional, tenant)

    assert item.has_calendar is True  # the clinic-level connection still counts
    assert item.has_hours is False
    assert item.has_services is False
    assert item.complete is False


async def test_completeness_null_uses_the_legacy_tenant_config(session):
    tenant = await make_tenant(session)
    professional = await make_professional(session, tenant)  # both columns NULL
    await set_google_refresh_token(session, tenant.id, "1//synthetic-refresh-token")

    item = await professional_completeness_item(session, professional, tenant)

    assert (item.has_hours, item.has_services, item.complete) == (True, True, True)


async def test_activation_is_refused_for_an_emptied_professional_with_a_reason(session):
    """Emptying the config blocks go-live, and says which half is missing."""
    tenant = await make_tenant(session)
    await make_professional(session, tenant, business_hours={}, appointment_types=[])
    await set_google_refresh_token(session, tenant.id, "1//synthetic-refresh-token")

    ok, reasons = await can_activate_professional_aware(session, tenant)

    assert ok is False
    joined = " ".join(reasons)
    assert "appointment type" in joined
    assert "availability window" in joined


async def test_activation_still_allowed_when_the_professional_inherits(session):
    tenant = await make_tenant(session)
    await make_professional(session, tenant)  # NULL/NULL -> inherits
    await set_google_refresh_token(session, tenant.id, "1//synthetic-refresh-token")

    ok, reasons = await can_activate_professional_aware(session, tenant)

    assert (ok, reasons) == (True, [])


async def test_completeness_alert_is_categorical_and_carries_no_identifiers(session):
    """The empty-config warning may carry counts and flags — nothing else.

    Asserted, not just documented: a log line is an artifact that leaves the
    building, so it must not contain a tenant id, a professional id, a clinic
    name, a weekday or a service name. `capture_logs` inspects the event dict
    BEFORE any renderer, so this checks what the call site actually passed
    rather than what a formatter happened to keep.
    """
    tenant = await make_tenant(session, name="Clinica Secreta")
    await make_professional(session, tenant, name="Dra. Sigilosa", business_hours={})

    with capture_logs() as captured:
        await professional_completeness(session, tenant)

    events = [e for e in captured if e.get("event") == "professional_config_resolves_empty"]
    assert len(events) == 1, "an active professional with no hours must be reported, once"
    event = events[0]
    assert event["without_hours"] == 1
    assert event["inheriting_hours"] == 0
    # Only counts, flags and the log level itself — no identifier of any kind.
    assert set(event) == {
        "event",
        "log_level",
        "total_active",
        "without_hours",
        "without_services",
        "inheriting_hours",
        "inheriting_services",
        "tenant_is_active",
    }
    blob = " ".join(str(value) for value in event.values())
    for forbidden in (
        str(tenant.id),
        "Clinica Secreta",
        "Dra. Sigilosa",
        "monday",
        "Consulta da clinica",
    ):
        assert forbidden not in blob


async def test_no_alert_when_every_active_professional_resolves_to_something(session):
    """The alert must stay quiet on a healthy clinic, or it is noise nobody reads."""
    tenant = await make_tenant(session)
    await make_professional(session, tenant)  # inherits a populated tenant

    with capture_logs() as captured:
        await professional_completeness(session, tenant)

    assert [e for e in captured if e.get("event") == "professional_config_resolves_empty"] == []


# ---------------------------------------------------------------------------
# 4. The runtime consumers must agree on the SAME effective config
# ---------------------------------------------------------------------------


async def test_llm_runtime_config_honours_an_empty_override(session):
    tenant = await make_tenant(session)
    await make_professional(session, tenant, business_hours={}, appointment_types=[])

    config = await load_tenant_config(session, tenant)

    assert config.business_hours == {}
    assert config.appointment_types == []
    # And the rendered prompt must not name a service the clinic stopped
    # offering through this professional.
    assert "Consulta da clinica" not in secretary_system_prompt(config)


async def test_llm_runtime_config_inherits_on_null(session):
    tenant = await make_tenant(session)
    await make_professional(session, tenant)

    config = await load_tenant_config(session, tenant)

    assert config.business_hours == TENANT_HOURS
    assert [t.name for t in config.appointment_types] == ["Consulta da clinica"]


async def test_every_consumer_resolves_the_same_effective_catalog(session):
    """Deterministic flow, LLM config and Pix must not disagree.

    They each resolve through `professional_appointment_types`; this asserts
    that they actually land on the same answer for an EMPTY override, which is
    the case where they used to diverge from the hub screen.
    """
    tenant = await make_tenant(session)
    professional = await make_professional(
        session, tenant, business_hours={}, appointment_types=[]
    )

    helper = professional_appointment_types(professional, tenant)
    flow = _flow_tenant_snapshot(tenant, [professional]).appointment_types
    runtime = await load_tenant_config(session, tenant)

    assert helper == []
    assert flow == []
    assert [t.name for t in runtime.appointment_types] == []


async def test_pix_price_lookup_does_not_fall_back_to_the_tenant_catalog(session):
    """A priced clinic service must not price a booking the professional dropped."""
    tenant = await make_tenant(
        session,
        appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 450"}
        ],
    )
    professional = await make_professional(session, tenant, appointment_types=[])
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        professional_id=professional.id,
        appointment_type="Consulta",
        # Synthetic, non-NULL: the column is required, and no real Google event
        # id (or anything else from a real calendar) belongs in a fixture.
        google_event_id="synthetic-event-id",
        start_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        end_at=datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
    )
    session.add(appointment)
    await session.flush()

    _name, price = await deposit_lifecycle._resolve_service_and_price(session, tenant, appointment)

    assert price is None


async def test_pix_price_lookup_still_works_when_the_professional_inherits(session):
    tenant = await make_tenant(
        session,
        appointment_types=[
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 450"}
        ],
    )
    professional = await make_professional(session, tenant)  # NULL -> inherits
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        professional_id=professional.id,
        appointment_type="Consulta",
        # Synthetic, non-NULL: the column is required, and no real Google event
        # id (or anything else from a real calendar) belongs in a fixture.
        google_event_id="synthetic-event-id",
        start_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        end_at=datetime(2026, 9, 1, 12, 30, tzinfo=UTC),
    )
    session.add(appointment)
    await session.flush()

    name, price = await deposit_lifecycle._resolve_service_and_price(session, tenant, appointment)

    assert (name, price) == ("Consulta", "R$ 450")


# ---------------------------------------------------------------------------
# 5. The Calendar chain — the second falsy-fallback that lived downstream
# ---------------------------------------------------------------------------


async def test_calendar_does_not_substitute_tenant_hours_for_an_empty_override(session):
    """`CalendarService.for_professional` used to treat `{}` as "no opinion".

    That made the fix above invisible on the one path that actually offers slots
    to a patient: the resolver correctly returned no hours, and the Calendar
    layer put the clinic's hours straight back.
    """
    tenant = await make_tenant(session)
    professional = await make_professional(session, tenant, business_hours={})
    captured: dict = {}

    def factory(**overrides):
        captured.update(overrides)
        return SimpleNamespace(**overrides)

    await resolve_professional_calendar(session, tenant, professional, calendar_factory=factory)
    assert captured["business_hours"] == {}

    calendar = await resolve_professional_calendar(session, tenant, professional)
    assert calendar._business_hours == {}


async def test_calendar_uses_tenant_hours_when_the_professional_inherits(session):
    tenant = await make_tenant(session)
    professional = await make_professional(session, tenant)

    calendar = await resolve_professional_calendar(session, tenant, professional)

    assert calendar._business_hours == TENANT_HOURS


async def test_calendar_for_professional_keeps_tenant_hours_on_none(session):
    """`None` still means "no opinion" — the tenantless caller depends on it."""
    tenant = await make_tenant(session)
    professional = await make_professional(session, tenant)
    config = await load_tenant_config(session, tenant)

    kept = CalendarService.for_professional(config, business_hours=None)
    honoured = CalendarService.for_professional(config, business_hours={})

    assert kept._business_hours == TENANT_HOURS
    assert honoured._business_hours == {}
    # And the tenantless path (no tenant row to resolve against) must not be
    # mistaken for "resolved to no hours".
    tenantless = await resolve_professional_calendar(
        session, None, professional, tenant_config=config
    )
    assert tenantless._business_hours == TENANT_HOURS


# ---------------------------------------------------------------------------
# 6. The hub wire — four states, additive flags, old-client compatibility
# ---------------------------------------------------------------------------

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
async def api_tenant(db) -> Tenant:
    async with db() as s:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinica API",
            phone_number_id=str(uuid4())[:12],
            business_hours=TENANT_HOURS,
            appointment_types=TENANT_TYPES,
        )
        s.add(tenant)
        await s.commit()
        await s.refresh(tenant)
        return tenant


@pytest.fixture
def _override_api_deps(db, api_tenant):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as s:
            yield s

    async def _fake_get_current_tenant():
        return api_tenant

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


@pytest.fixture
def _no_entitlement_calls(monkeypatch: pytest.MonkeyPatch):
    """A config save is never gated, so no brain-api call may happen at all."""

    async def _fake(tenant_id, redis=None):
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

    monkeypatch.setattr(professionals_api, "get_entitlements", _fake)


@pytest.fixture
def api(client, _override_api_deps, _no_entitlement_calls) -> AsyncClient:
    """The HTTP client with tenant/session overrides and entitlements stubbed.

    Bundled into one fixture so the wire tests below declare a single dependency
    and the unit tests above never pay for the FastAPI wiring.
    """
    return client


async def _seed_api_professional(db, tenant: Tenant, **overrides) -> Professional:
    fields = dict(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    fields.update(overrides)
    async with db() as s:
        professional = Professional(**fields)
        s.add(professional)
        await s.commit()
        await s.refresh(professional)
        return professional


async def _stored(db, professional_id) -> Professional:
    async with db() as s:
        return await s.get(Professional, professional_id)


async def test_get_distinguishes_inheritance_from_an_empty_override(
    api: AsyncClient, db, api_tenant
) -> None:
    inheriting = await _seed_api_professional(db, api_tenant, name="Herda")
    emptied = await _seed_api_professional(
        db, api_tenant, name="Vazia", business_hours={}, appointment_types=[]
    )

    rows = {row["name"]: row for row in (await api.get(ENDPOINT)).json()}

    # Both look identical in the legacy fields — that was the whole problem.
    assert rows["Herda"]["business_hours"] == rows["Vazia"]["business_hours"] == {}
    assert rows["Herda"]["appointment_types"] == rows["Vazia"]["appointment_types"] == []
    # The additive flags are what tells them apart.
    assert rows["Herda"]["business_hours_inherited"] is True
    assert rows["Herda"]["appointment_types_inherited"] is True
    assert rows["Vazia"]["business_hours_inherited"] is False
    assert rows["Vazia"]["appointment_types_inherited"] is False
    # ...and completeness agrees with the runtime, not with the flat fields.
    assert rows["Herda"]["has_hours"] is True
    assert rows["Vazia"]["has_hours"] is False
    assert inheriting.id != emptied.id


@pytest.mark.parametrize(
    "body,expect_hours_col,expect_inherited",
    [
        ({"business_hours": None}, None, True),
        ({"business_hours": {}}, {}, False),
        ({"business_hours": OWN_HOURS}, OWN_HOURS, False),
    ],
    ids=["explicit-null-goes-back-to-inheriting", "empty-override", "own-override"],
)
async def test_put_round_trips_each_state(
    api: AsyncClient, db, api_tenant, body, expect_hours_col, expect_inherited
) -> None:
    professional = await _seed_api_professional(db, api_tenant, business_hours=OWN_HOURS)

    response = await api.put(f"{ENDPOINT}/{professional.id}/config", json=body)

    assert response.status_code == 200
    assert response.json()["business_hours_inherited"] is expect_inherited
    assert (await _stored(db, professional.id)).business_hours == expect_hours_col


async def test_put_omitting_a_field_leaves_it_untouched(api: AsyncClient, db, api_tenant) -> None:
    """The fourth state: absent means "don't touch", not "set to null".

    This is what protects an inheriting professional from a save that only meant
    to change the specialty.
    """
    professional = await _seed_api_professional(db, api_tenant)  # NULL/NULL

    response = await api.put(
        f"{ENDPOINT}/{professional.id}/config", json={"specialty": "Cardiologia"}
    )

    assert response.status_code == 200
    stored = await _stored(db, professional.id)
    assert stored.specialty == "Cardiologia"
    assert stored.business_hours is None
    assert stored.appointment_types is None
    assert response.json()["business_hours_inherited"] is True


async def test_explicit_null_is_a_deliberate_return_to_inheriting(
    api: AsyncClient, db, api_tenant
) -> None:
    professional = await _seed_api_professional(
        db, api_tenant, business_hours={}, appointment_types=[]
    )

    response = await api.put(
        f"{ENDPOINT}/{professional.id}/config",
        json={"business_hours": None, "appointment_types": None},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["business_hours_inherited"] is True
    assert body["appointment_types_inherited"] is True
    # And the legacy clinic config is live again for this professional.
    assert body["has_hours"] is True
    assert body["has_services"] is True


async def test_put_is_idempotent_for_both_null_and_empty(
    api: AsyncClient, db, api_tenant
) -> None:
    """A retried save must land on the same state, not oscillate."""
    professional = await _seed_api_professional(db, api_tenant, business_hours=OWN_HOURS)

    for body in ({"business_hours": None}, {"business_hours": {}}):
        first = (await api.put(f"{ENDPOINT}/{professional.id}/config", json=body)).json()
        second = (await api.put(f"{ENDPOINT}/{professional.id}/config", json=body)).json()
        assert first["business_hours_inherited"] == second["business_hours_inherited"]
        assert first["business_hours"] == second["business_hours"]


async def test_old_client_payload_and_response_still_work(
    api: AsyncClient, db, api_tenant
) -> None:
    """Compatibility both ways, on the wire.

    An OLD client sends the pre-flag body shape and reads the pre-flag key set;
    it must still get a 200 and still find every key it knows. Its `{}` now
    means "empty override" rather than "inherit" — that is the deliberate
    semantic change, and the assertion below states it rather than hiding it.
    """
    professional = await _seed_api_professional(db, api_tenant)

    legacy_body = {
        "business_hours": {},
        "appointment_types": [],
        "specialty": None,
        "about": None,
        "context_doctor_message": None,
    }
    response = await api.put(f"{ENDPOINT}/{professional.id}/config", json=legacy_body)

    assert response.status_code == 200
    body = response.json()
    # Every key an old client already parsed is still present, same types.
    for key in (
        "id",
        "name",
        "google_calendar_id",
        "is_active",
        "created_at",
        "specialty",
        "about",
        "context_doctor_message",
        "business_hours",
        "appointment_types",
        "has_calendar",
        "has_hours",
        "has_services",
        "complete",
    ):
        assert key in body
    # The documented behaviour change, asserted out loud.
    assert body["business_hours_inherited"] is False
    assert body["has_hours"] is False


async def test_a_save_cannot_reach_another_tenants_professional(
    api: AsyncClient, db, api_tenant
) -> None:
    """Ownership is server-side, so "no fallback across tenants" holds on the wire too."""
    async with db() as s:
        other = Tenant(id=uuid4(), clinic_name="Outra Clinica", phone_number_id=str(uuid4())[:12])
        s.add(other)
        await s.flush()
        foreign = Professional(id=uuid4(), tenant_id=other.id, name="Alheio", is_active=True)
        s.add(foreign)
        await s.commit()
        foreign_id = foreign.id

    response = await api.put(f"{ENDPOINT}/{foreign_id}/config", json={"business_hours": {}})

    assert response.status_code == 404
    async with db() as s:
        assert (await s.get(Professional, foreign_id)).business_hours is None
