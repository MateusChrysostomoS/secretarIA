"""Tests for api/hub/config.py — PUT /tenants/me/config, focused on the
contract v1 §10 additions: address/insurances/collect_insurance, and the
CRITICAL invariant that a plain config save is NEVER blocked by the
can_activate gate (only an explicit `is_active: true` request is).

Same db/tenant/_override pattern as test_hub_professionals.py.
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

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.core.whatsapp_limits import MAX_INTERACTIVE_BODY_CHARS  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.services.greeting_template import (  # noqa: E402
    PREVIEW_PLACEHOLDER,
    clinic_description_budget,
)

# The seeded clinic name. Named because the greeting budget DEPENDS on it:
# a longer name leaves the clinic fewer characters, so the cap test has to
# compute its bound from this exact value.
TENANT_CLINIC_NAME = "Clinic"

CONFIG = "/tenants/me/config"


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
    """A tenant with NO phone_number_id (not connected) and NO Calendar -
    the exact state the "plain save must still work" regression guards."""
    async with db() as session:
        t = Tenant(id=uuid4(), clinic_name=TENANT_CLINIC_NAME, phone_number_id=None)
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    # update_config MUTATES the `tenant` parameter directly and then calls
    # session.refresh(tenant) - it MUST be the same ORM instance/session as
    # the route's own `Depends(get_session)`, exactly like production (where
    # get_current_tenant's own `Depends(get_session)` is FastAPI-cached to
    # the SAME session within one request). Re-fetching by id through the
    # (overridden) get_session dependency reproduces that guarantee here.
    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


# --------------------------------------------------------------------------
# GET — shape includes the three new fields
# --------------------------------------------------------------------------


async def test_get_config_includes_new_fields_with_defaults(client: AsyncClient) -> None:
    response = await client.get(CONFIG)
    assert response.status_code == 200
    body = response.json()
    assert body["address"] is None
    assert body["insurances"] == []
    assert body["collect_insurance"] is False
    assert body["post_consult_message"] is None
    assert body["post_consult_knowledge"] is None


async def test_get_config_omits_greeting_buttons(client: AsyncClient) -> None:
    """The greeting's buttons are now a fixed, product-defined set
    (workers/tasks.py::_greeting_buttons_for), never hub-editable - the field
    is gone from the response entirely, not just empty. See
    docs/CHECKPOINT_fixed_greeting_buttons.md for the full contract change."""
    response = await client.get(CONFIG)
    assert response.status_code == 200
    assert "greeting_buttons" not in response.json()


# --------------------------------------------------------------------------
# CRITICAL: plain config save is allowed while disconnected/not calendar-ready
# --------------------------------------------------------------------------


async def test_put_address_and_insurances_succeeds_while_disconnected(
    client: AsyncClient, tenant
) -> None:
    """The tenant fixture has phone_number_id=None and no Calendar connected -
    a plain config save (no `is_active` in the body) must still succeed."""
    assert tenant.phone_number_id is None

    response = await client.put(
        CONFIG,
        json={
            "address": {
                "line": "Rua das Flores, 123",
                "city": "São Paulo",
                "state": "SP",
                "postal_code": "01000-000",
            },
            "insurances": ["Unimed", "Amil"],
            "collect_insurance": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["address"]["city"] == "São Paulo"
    assert body["insurances"] == ["Unimed", "Amil"]
    assert body["collect_insurance"] is True
    assert body["is_active"] is False  # untouched


async def test_put_greeting_only_succeeds_while_disconnected(client: AsyncClient, tenant) -> None:
    """A completely unrelated config field (no address/insurance at all)
    must also never be blocked by the disconnected state."""
    response = await client.put(CONFIG, json={"clinic_description": "Oftalmologia geral."})
    assert response.status_code == 200
    assert response.json()["clinic_description"] == "Oftalmologia geral."


async def test_put_empty_body_is_a_no_op_success(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={})
    assert response.status_code == 200


async def test_put_greeting_buttons_is_silently_ignored(client: AsyncClient, db, tenant) -> None:
    """PUT no longer accepts this field at all: an incoming `greeting_buttons`
    is dropped like any other unrecognized key (pydantic's default `extra`
    behaviour on TenantConfigUpdate, which no longer declares the field) -
    never persisted, never echoed back, and the sibling `clinic_description`
    in the same request still saves normally."""
    response = await client.put(
        CONFIG,
        json={
            "clinic_description": "Oftalmologia.",
            "greeting_buttons": ["Isso não deveria ser salvo"],
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["clinic_description"] == "Oftalmologia."
    assert "greeting_buttons" not in body

    async with db() as session:
        stored = await session.get(Tenant, tenant.id)
        assert stored.greeting_buttons == []  # DB column untouched


async def test_put_greeting_message_over_button_cap_rejected(client: AsyncClient) -> None:
    """The greeting is ALWAYS sent with the fixed action buttons attached (no
    more "with buttons" vs "plain text" choice - see
    docs/CHECKPOINT_fixed_greeting_buttons.md), so it must fit WhatsApp's
    1024-char interactive-body cap unconditionally.

    Since the greeting-frame round the FIRST-CONTACT half of that budget is
    spent by the product frame, so the clinic's slot is what gets checked, and
    against a per-clinic budget rather than a flat 1024 - see
    services/greeting_template.py::clinic_description_budget. Asserted against
    the computed budget, never a hardcoded number: the frame's copy will be
    edited, and a literal here would pin a stale budget and pass while
    production overflowed.
    """
    budget = clinic_description_budget(TENANT_CLINIC_NAME)

    response = await client.put(CONFIG, json={"clinic_description": "x" * (budget + 1)})
    assert response.status_code == 422

    response = await client.put(CONFIG, json={"returning_greeting_message": "x" * 1025})
    assert response.status_code == 422

    # Exactly at the budget still succeeds, and the whole rendered greeting
    # lands exactly on WhatsApp's cap rather than one char over it.
    response = await client.put(CONFIG, json={"clinic_description": "x" * budget})
    assert response.status_code == 200
    # The wire carries the frame as a TEMPLATE (the clinic's slot replaced by
    # a token) so the hub can preview what it is typing; rendering the accepted
    # description back into it must land exactly on WhatsApp's cap.
    body = response.json()
    template = body["greeting_preview_template"]
    assert PREVIEW_PLACEHOLDER in template
    rendered = template.replace(PREVIEW_PLACEHOLDER, body["clinic_description"])
    assert len(rendered) == MAX_INTERACTIVE_BODY_CHARS


async def test_put_post_consult_fields_round_trip_while_disconnected(
    client: AsyncClient, tenant
) -> None:
    """Both post-consult fields save and read back correctly on a tenant with
    no phone_number_id and no Calendar connected - same disconnected-save
    invariant as every other plain config field (contract v1 §10), no
    entitlement check anywhere in sight."""
    assert tenant.phone_number_id is None

    response = await client.put(
        CONFIG,
        json={
            "post_consult_message": "Como você está se sentindo após a consulta?",
            "post_consult_knowledge": (
                "Retorno em 7 dias. Resultados de exame saem em 48h pelo portal."
            ),
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["post_consult_message"] == "Como você está se sentindo após a consulta?"
    assert body["post_consult_knowledge"] == (
        "Retorno em 7 dias. Resultados de exame saem em 48h pelo portal."
    )

    follow_up = await client.get(CONFIG)
    follow_up_body = follow_up.json()
    assert follow_up_body["post_consult_message"] == "Como você está se sentindo após a consulta?"
    assert follow_up_body["post_consult_knowledge"] == (
        "Retorno em 7 dias. Resultados de exame saem em 48h pelo portal."
    )


async def test_put_post_consult_message_only_leaves_knowledge_untouched(
    client: AsyncClient,
) -> None:
    """Partial update (exclude_unset): saving only post_consult_message must
    not clobber a previously-saved post_consult_knowledge."""
    seed = await client.put(
        CONFIG,
        json={"post_consult_knowledge": "Levar exames anteriores no retorno."},
    )
    assert seed.status_code == 200

    response = await client.put(
        CONFIG,
        json={"post_consult_message": "Esperamos que tenha corrido tudo bem!"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["post_consult_message"] == "Esperamos que tenha corrido tudo bem!"
    assert body["post_consult_knowledge"] == "Levar exames anteriores no retorno."


# --------------------------------------------------------------------------
# The gate STILL blocks an explicit is_active=true when not ready
# --------------------------------------------------------------------------


async def test_put_explicit_activate_while_not_ready_is_422(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={"is_active": True})
    assert response.status_code == 422

    # And the tenant is genuinely untouched (is_active still False).
    follow_up = await client.get(CONFIG)
    assert follow_up.json()["is_active"] is False


async def test_put_activate_succeeds_once_prerequisites_met(
    client: AsyncClient, db, tenant
) -> None:
    from secretaria.services.tenant_config import set_google_refresh_token

    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "refresh-token")
        await session.commit()

    response = await client.put(
        CONFIG,
        json={
            "business_hours": {"monday": [{"start": "08:00", "end": "12:00"}]},
            "appointment_types": [{"name": "Consulta", "duration_min": 30, "is_active": True}],
            "is_active": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["is_active"] is True


async def test_put_is_active_false_is_never_gated(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={"is_active": False})
    assert response.status_code == 200
    assert response.json()["is_active"] is False


# --------------------------------------------------------------------------
# insurances validation
# --------------------------------------------------------------------------


async def test_put_insurances_trims_and_drops_blank_entries(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={"insurances": ["  Unimed  ", "", "   ", "Amil"]})
    assert response.status_code == 200
    assert response.json()["insurances"] == ["Unimed", "Amil"]


async def test_put_insurances_over_limit_is_422(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={"insurances": [f"Plan {i}" for i in range(51)]})
    assert response.status_code == 422


async def test_put_address_partial_fields_allowed(client: AsyncClient) -> None:
    """A clinic may fill in only what it has - the unset nested fields are
    simply absent from what gets stored (exclude_unset all the way down),
    not present-and-null."""
    response = await client.put(CONFIG, json={"address": {"city": "Recife"}})
    assert response.status_code == 200
    assert response.json()["address"] == {"city": "Recife"}


# --------------------------------------------------------------------------
# appointment_types[].requirements — pre-consult orientations
# --------------------------------------------------------------------------


async def test_put_appointment_type_requirements_round_trip(client: AsyncClient) -> None:
    response = await client.put(
        CONFIG,
        json={
            "appointment_types": [
                {
                    "name": "Consulta",
                    "duration_min": 30,
                    "is_active": True,
                    "requirements": ["Jejum de 8 horas", "Trazer exames anteriores"],
                }
            ]
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["appointment_types"][0]["requirements"] == [
        "Jejum de 8 horas",
        "Trazer exames anteriores",
    ]

    follow_up = await client.get(CONFIG)
    assert follow_up.json()["appointment_types"][0]["requirements"] == [
        "Jejum de 8 horas",
        "Trazer exames anteriores",
    ]


async def test_put_appointment_type_requirements_trims_and_drops_blank_entries(
    client: AsyncClient,
) -> None:
    response = await client.put(
        CONFIG,
        json={
            "appointment_types": [
                {
                    "name": "Consulta",
                    "duration_min": 30,
                    "requirements": ["  Jejum de 8 horas  ", "", "   "],
                }
            ]
        },
    )
    assert response.status_code == 200
    assert response.json()["appointment_types"][0]["requirements"] == ["Jejum de 8 horas"]


async def test_put_appointment_type_requirements_over_limit_is_422(client: AsyncClient) -> None:
    response = await client.put(
        CONFIG,
        json={
            "appointment_types": [
                {
                    "name": "Consulta",
                    "duration_min": 30,
                    "requirements": [f"Item {i}" for i in range(21)],
                }
            ]
        },
    )
    assert response.status_code == 422


async def test_put_appointment_type_requirement_too_long_is_422(client: AsyncClient) -> None:
    response = await client.put(
        CONFIG,
        json={
            "appointment_types": [
                {"name": "Consulta", "duration_min": 30, "requirements": ["x" * 301]}
            ]
        },
    )
    assert response.status_code == 422


# --------------------------------------------------------------------------
# google_calendar_mode (docs/CHECKPOINT_google_calendar_modes.md)
# --------------------------------------------------------------------------


async def test_get_config_defaults_google_calendar_mode_to_per_professional(
    client: AsyncClient,
) -> None:
    response = await client.get(CONFIG)
    assert response.status_code == 200
    assert response.json()["google_calendar_mode"] == "per_professional"


async def test_put_google_calendar_mode_round_trips_to_shared_account(
    client: AsyncClient,
) -> None:
    response = await client.put(CONFIG, json={"google_calendar_mode": "shared_account"})
    assert response.status_code == 200
    assert response.json()["google_calendar_mode"] == "shared_account"

    follow_up = await client.get(CONFIG)
    assert follow_up.json()["google_calendar_mode"] == "shared_account"


async def test_put_google_calendar_mode_invalid_value_is_422(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={"google_calendar_mode": "bogus"})
    assert response.status_code == 422

    # And the value is genuinely untouched.
    follow_up = await client.get(CONFIG)
    assert follow_up.json()["google_calendar_mode"] == "per_professional"


async def test_put_google_calendar_mode_while_disconnected_succeeds(
    client: AsyncClient, tenant
) -> None:
    """Same disconnected-save invariant as every other plain config field -
    switching modes never depends on Calendar being connected or the tenant
    being activatable."""
    assert tenant.phone_number_id is None
    response = await client.put(CONFIG, json={"google_calendar_mode": "shared_account"})
    assert response.status_code == 200
    assert response.json()["google_calendar_mode"] == "shared_account"


async def test_switching_google_calendar_mode_is_non_destructive(
    client: AsyncClient, db, tenant
) -> None:
    """Trocar de modo NÃO mexe em tokens nem em google_calendar_id - só qual
    fluxo a UI oferece (item 6 of docs/CHECKPOINT_google_calendar_modes.md)."""
    from secretaria.services.tenant_config import (
        has_google_refresh_token,
        set_google_refresh_token,
    )

    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "clinic-refresh-token")
        row = await session.get(Tenant, tenant.id)
        row.google_calendar_id = "clinic-primary-cal"
        await session.commit()

    response = await client.put(CONFIG, json={"google_calendar_mode": "shared_account"})
    assert response.status_code == 200
    body = response.json()
    assert body["google_calendar_mode"] == "shared_account"
    assert body["google_calendar_id"] == "clinic-primary-cal"  # untouched
    assert body["calendar_connected"] is True  # token untouched

    async with db() as session:
        assert await has_google_refresh_token(session, tenant.id) is True


async def test_get_config_appointment_type_without_requirements_key_still_reads(
    client: AsyncClient, db, tenant
) -> None:
    """Old rows stored before this field existed have no `requirements` key at
    all - GET must not choke on that (raw JSON passthrough)."""
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.appointment_types = [{"name": "Consulta", "duration_min": 30, "is_active": True}]
        await session.commit()

    response = await client.get(CONFIG)
    assert response.status_code == 200
    body = response.json()
    assert body["appointment_types"][0]["name"] == "Consulta"
    assert "requirements" not in body["appointment_types"][0]
