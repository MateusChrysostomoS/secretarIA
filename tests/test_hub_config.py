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
from secretaria.models import Tenant  # noqa: E402

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
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=None)
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


async def test_put_greeting_only_succeeds_while_disconnected(
    client: AsyncClient, tenant
) -> None:
    """A completely unrelated config field (no address/insurance at all)
    must also never be blocked by the disconnected state."""
    response = await client.put(CONFIG, json={"greeting_message": "Olá! Bem-vindo."})
    assert response.status_code == 200
    assert response.json()["greeting_message"] == "Olá! Bem-vindo."


async def test_put_empty_body_is_a_no_op_success(client: AsyncClient) -> None:
    response = await client.put(CONFIG, json={})
    assert response.status_code == 200


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
            "appointment_types": [
                {"name": "Consulta", "duration_min": 30, "is_active": True}
            ],
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
    response = await client.put(
        CONFIG, json={"insurances": ["  Unimed  ", "", "   ", "Amil"]}
    )
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
