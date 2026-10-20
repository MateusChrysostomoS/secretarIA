"""A one-doctor clinic configured ONLY through the hub API can take a booking.

Production, 2026-10-20: a brand-new clinic with one doctor ticked its working
days in the console, saved twice (200, 200), and its patients still read "No
momento não há serviços disponíveis para agendamento." Three gaps lined up:

  1. The console sent `{"monday": [], ...}` for a day turned on without a time
     range; the API stored it as given, so the schedule looked configured and
     offered nothing. The shared validator now drops empty weekdays (one
     canonical shape; the console blocks the save and names the day).
  2. A service created in the catalog is offered by nobody until a professional
     ticks it, and the console only ticked it locally. POST /tenants/me/services
     now takes `offer` and writes the professional's entry in the same commit.
  3. GET /tenants/me/context-completeness said "Pelo menos um serviço: feito"
     for a catalog row nobody offered, and read the clinic's hours instead of
     the doctor's. It now asks the booking flow's own question, roster-aware.

The last block drives the real router and a real CalendarService (fake Google)
from data written exclusively by those endpoints, exactly as the console saves
it for a one-doctor clinic: clinic hours set, the doctor inheriting them
(`business_hours: null`), and one service created with `offer`.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai.formatter import SlotsBubble  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import FlowState, Tenant  # noqa: E402
from secretaria.models.professional import Professional  # noqa: E402
from secretaria.models.service import Service  # noqa: E402
from secretaria.services.calendar import CalendarService  # noqa: E402
from secretaria.services.context_completeness import compute_completeness  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    LABEL_ATTENDEE_SELF,
    LABEL_BOOK,
    STEP_AWAITING_DAY,
    STEP_AWAITING_SERVICE_CONFIRM,
    route,
)
from secretaria.services.service_catalog import load_service_catalog  # noqa: E402
from secretaria.services.tenant_config import load_tenant_config  # noqa: E402
from secretaria.workers.shared.greeting import (  # noqa: E402
    _flow_professionals,
    _flow_tenant_snapshot,
)

CONFIGURATION = "/tenants/me/configuration"
SERVICES = "/tenants/me/services"
PROFESSIONALS = "/tenants/me/professionals"
COMPLETENESS = "/tenants/me/context-completeness"

MORNING = {"start": "08:00", "end": "12:00"}
AFTERNOON = {"start": "14:00", "end": "18:00"}
# What the console sends once a one-doctor clinic opens Monday with the default
# ranges: the clinic's week, and the doctor following it (`null`).
CLINIC_WEEK = {"monday": [MORNING, AFTERNOON]}
_TZ = ZoneInfo("America/Sao_Paulo")


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
        t = Tenant(
            id=uuid4(),
            clinic_name="Clínica Demonstração",
            phone_number_id=None,
            timezone="America/Sao_Paulo",
            appointment_duration_min=30,
            initial_flows={},
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


async def _add_professional(db, tenant_id, **kw) -> Professional:
    async with db() as session:
        p = Professional(
            id=uuid4(), tenant_id=tenant_id, name=kw.pop("name", "Dr. Lucas"), is_active=True, **kw
        )
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p


@pytest_asyncio.fixture
async def doctor(db, tenant) -> Professional:
    """A freshly provisioned doctor: no hours and no services of their own."""
    return await _add_professional(db, tenant.id)


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _reload(db, tenant_id, professional_id=None):
    async with db() as session:
        t = await session.get(Tenant, tenant_id)
        p = await session.get(Professional, professional_id) if professional_id else None
        return t, p


async def _service_count(db, tenant_id) -> int:
    async with db() as session:
        return await session.scalar(
            select(func.count()).select_from(Service).where(Service.tenant_id == tenant_id)
        )


# --------------------------------------------------------------------------
# 1. An open day with no range is never stored as a schedule
# --------------------------------------------------------------------------


async def test_a_weekday_with_no_window_is_dropped_on_both_scopes(
    client: AsyncClient, db, tenant, doctor
) -> None:
    """The exact production payload: days "on" with no range, clinic and doctor."""
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"business_hours": {"monday": [], "tuesday": [MORNING]}},
            "professional_id": str(doctor.id),
            "professional": {"business_hours": {"monday": [], "tuesday": []}},
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["tenant"]["business_hours"] == {"tuesday": [MORNING]}
    # Still the doctor's OWN (empty) schedule — never silently turned into inheritance.
    assert body["professional"]["business_hours"] == {}
    assert body["professional"]["business_hours_inherited"] is False
    assert body["professional"]["has_hours"] is False

    t, p = await _reload(db, tenant.id, doctor.id)
    assert t.business_hours == {"tuesday": [MORNING]}
    assert p.business_hours == {}


async def test_hours_round_trip_put_then_get(client: AsyncClient, db, tenant, doctor) -> None:
    """What a save sends is what a reload reads, on both GETs the console uses."""
    put = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"business_hours": CLINIC_WEEK, "appointment_duration_min": 40},
            "professional_id": str(doctor.id),
            "professional": {"business_hours": None},
        },
    )
    assert put.status_code == 200

    config = (await client.get("/tenants/me/config")).json()
    assert config["business_hours"] == CLINIC_WEEK
    assert config["appointment_duration_min"] == 40
    [row] = (await client.get(PROFESSIONALS)).json()
    assert row["business_hours_inherited"] is True
    assert row["business_hours"] == {}
    # Inheriting the clinic's week is a real schedule for the doctor.
    assert row["has_hours"] is True


async def test_a_one_doctor_save_repairs_the_empty_own_week(
    client: AsyncClient, db, tenant
) -> None:
    """The test clinic's stored state: an own week whose every day is `[]`.

    The console's one-schedule save (clinic week + doctor `null`) is what takes
    the doctor back onto the clinic's hours.
    """
    broken = await _add_professional(db, tenant.id, business_hours={"monday": [], "tuesday": []})
    response = await client.put(
        CONFIGURATION,
        json={
            "tenant": {"business_hours": CLINIC_WEEK},
            "professional_id": str(broken.id),
            "professional": {"business_hours": None},
        },
    )
    assert response.status_code == 200
    assert response.json()["professional"]["has_hours"] is True
    _t, p = await _reload(db, tenant.id, broken.id)
    assert p.business_hours is None


async def test_an_empty_day_is_dropped_from_a_simulation_draft_too(
    client: AsyncClient, tenant, doctor
) -> None:
    """The simulation validates the same schema, so the draft cannot disagree with a save."""
    from secretaria.schemas.config import TenantConfigUpdate

    draft = TenantConfigUpdate(business_hours={"monday": [], "friday": [AFTERNOON]})
    assert draft.model_dump(exclude_unset=True)["business_hours"] == {"friday": [AFTERNOON]}


# --------------------------------------------------------------------------
# 2. Creating a service can offer it in the same commit
# --------------------------------------------------------------------------


async def test_create_with_offer_makes_the_service_bookable(
    client: AsyncClient, db, tenant, doctor
) -> None:
    response = await client.post(
        SERVICES,
        json={
            "name": "Consulta",
            "description": "Avaliação",
            "requirements": ["Jejum de 8 horas"],
            "offer": {"professional_id": str(doctor.id), "duration_min": 40, "price": " R$ 300 "},
        },
    )

    assert response.status_code == 201
    body = response.json()
    assert body["professional_ids"] == [str(doctor.id)]
    professional = body["professional"]
    assert professional["id"] == str(doctor.id)
    assert professional["has_services"] is True
    assert professional["appointment_types_inherited"] is False
    [entry] = professional["appointment_types"]
    assert entry == {
        "name": "Consulta",
        "service_id": body["id"],
        "description": None,
        "duration_min": 40,
        "is_active": True,
        "sort_order": 0,
        "price": "R$ 300",
        "long_description": None,
        # The catalog owns the orientations of a linked entry.
        "requirements": [],
    }
    _t, p = await _reload(db, tenant.id, doctor.id)
    assert p.appointment_types == [entry]


async def test_create_without_offer_is_unchanged(client: AsyncClient, db, tenant, doctor) -> None:
    response = await client.post(SERVICES, json={"name": "Consulta"})

    assert response.status_code == 201
    assert response.json()["professional_ids"] == []
    assert response.json()["professional"] is None
    _t, p = await _reload(db, tenant.id, doctor.id)
    assert p.appointment_types is None


async def test_offer_keeps_what_an_inheriting_doctor_already_offered(
    client: AsyncClient, db, tenant
) -> None:
    async with db() as session:
        t = await session.get(Tenant, tenant.id)
        t.appointment_types = [
            {"name": "Retorno", "duration_min": 20, "is_active": True, "sort_order": 0}
        ]
        await session.commit()
    inheriting = await _add_professional(db, tenant.id, appointment_types=None)

    response = await client.post(
        SERVICES,
        json={
            "name": "Consulta",
            "offer": {"professional_id": str(inheriting.id), "duration_min": 30},
        },
    )

    assert response.status_code == 201
    _t, p = await _reload(db, tenant.id, inheriting.id)
    assert [e["name"] for e in p.appointment_types] == ["Retorno", "Consulta"]
    assert p.appointment_types[1]["price"] is None


@pytest.mark.parametrize("foreign", [False, True])
async def test_an_unknown_professional_creates_nothing(
    client: AsyncClient, db, tenant, foreign
) -> None:
    target = str(uuid4())
    if foreign:
        other = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id=None)
        async with db() as session:
            session.add(other)
            await session.commit()
        target = str((await _add_professional(db, other.id, name="Dr. Outro")).id)

    response = await client.post(
        SERVICES,
        json={"name": "Consulta", "offer": {"professional_id": target, "duration_min": 30}},
    )

    assert response.status_code == 404
    assert await _service_count(db, tenant.id) == 0


async def test_a_duplicate_name_with_offer_changes_nothing(
    client: AsyncClient, db, tenant, doctor
) -> None:
    await client.post(SERVICES, json={"name": "Consulta"})

    response = await client.post(
        SERVICES,
        json={
            "name": " CONSULTA ",
            "offer": {"professional_id": str(doctor.id), "duration_min": 30},
        },
    )

    assert response.status_code == 409
    _t, p = await _reload(db, tenant.id, doctor.id)
    assert p.appointment_types is None


@pytest.mark.parametrize("duration", [0, 601])
async def test_offer_duration_is_validated(client: AsyncClient, db, tenant, doctor, duration):
    response = await client.post(
        SERVICES,
        json={
            "name": "Consulta",
            "offer": {"professional_id": str(doctor.id), "duration_min": duration},
        },
    )
    assert response.status_code == 422
    assert await _service_count(db, tenant.id) == 0
