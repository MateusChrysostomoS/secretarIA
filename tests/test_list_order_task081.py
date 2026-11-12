"""TASK-081 - the clinic decides the order the patient sees its services in.

(The convênio order is `tenant_insurance_plans.sort_order` / PUT /insurance-plans/order,
tested in tests/test_convenio_catalogo_db.py.)
Services: the CATALOG owns `sort_order` (`resolve_entries`), so one clinic order wins over
whichever doctor's list happens to be read first; `PUT /services/order` writes it.

Real in-memory SQLite, same fixtures as tests/test_convenio_catalogo_db.py.
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
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.models.service import Service  # noqa: E402
from secretaria.services import flow_router  # noqa: E402
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
    set_tenant_plans,
)
from secretaria.services.service_catalog import (  # noqa: E402
    load_service_catalog,
    normalize,
    resolve_entries,
)

UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")
BRADESCO = catalog_id_for_slug("bradesco-saude")


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
    async with maker() as session:
        await ensure_catalog_seeded(session)
        await session.commit()
    yield maker
    await engine.dispose()


async def _new_tenant(db, mode: str | None = "shared") -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinica",
            phone_number_id=str(uuid4())[:12],
            insurance_mode=mode,
            appointment_types=[],
        )
        session.add(tenant)
        await session.flush()
        await set_tenant_plans(session, tenant, [(UNIMED, True), (AMIL, True), (BRADESCO, True)])
        await session.commit()
        return tenant


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    return await _new_tenant(db)


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


# --------------------------------------------------------------------------
# Services
# --------------------------------------------------------------------------


async def _service(db, tenant, name: str, sort_order: int) -> Service:
    async with db() as session:
        row = Service(
            id=uuid4(),
            tenant_id=tenant.id,
            name=name,
            normalized_name=normalize(name),
            sort_order=sort_order,
        )
        session.add(row)
        await session.commit()
        return row


async def test_put_services_order_reorders_and_keeps_the_rest_after(
    client: AsyncClient, db, tenant
):
    a = await _service(db, tenant, "Consulta", 0)
    b = await _service(db, tenant, "Retorno", 1)
    c = await _service(db, tenant, "Exame", 2)
    put = await client.put(
        "/tenants/me/services/order", json={"service_ids": [str(c.id), str(a.id)]}
    )
    assert put.status_code == 200
    assert [row["id"] for row in put.json()] == [str(c.id), str(a.id), str(b.id)]
    assert [row["sort_order"] for row in put.json()] == [0, 1, 2]


async def test_put_services_order_refuses_unknown_and_repeated_ids(client: AsyncClient, db, tenant):
    a = await _service(db, tenant, "Consulta", 0)
    unknown = await client.put("/tenants/me/services/order", json={"service_ids": [str(uuid4())]})
    assert unknown.status_code == 404
    repeated = await client.put(
        "/tenants/me/services/order", json={"service_ids": [str(a.id), str(a.id)]}
    )
    assert repeated.status_code == 422


async def test_another_clinics_service_is_not_found(client: AsyncClient, db):
    other = await _new_tenant(db)
    theirs = await _service(db, other, "Consulta", 0)
    put = await client.put("/tenants/me/services/order", json={"service_ids": [str(theirs.id)]})
    assert put.status_code == 404


async def test_a_new_service_goes_to_the_end_of_the_clinics_order(client: AsyncClient, db, tenant):
    await _service(db, tenant, "Consulta", 0)
    await _service(db, tenant, "Retorno", 5)
    created = await client.post("/tenants/me/services", json={"name": "Mapeamento"})
    assert created.status_code == 201
    assert created.json()["sort_order"] == 6


def test_the_catalog_order_wins_over_each_doctors_own_order():
    """Two doctors stored the same services in opposite orders; the clinic's catalog order
    is what both lists - and therefore the patient's union - come out in."""
    tenant_id = uuid4()
    consulta = Service(
        id=uuid4(), tenant_id=tenant_id, name="Consulta", normalized_name="consulta", sort_order=1
    )
    retorno = Service(
        id=uuid4(), tenant_id=tenant_id, name="Retorno", normalized_name="retorno", sort_order=0
    )
    for service in (consulta, retorno):
        service.is_active = True
        service.description = None
        service.long_description = None
        service.requirements = []
    services = [retorno, consulta]
    ana = [
        {"name": "Consulta", "service_id": str(consulta.id), "sort_order": 0, "is_active": True},
        {"name": "Retorno", "service_id": str(retorno.id), "sort_order": 1, "is_active": True},
    ]
    resolved = resolve_entries(ana, services)
    assert {e["name"]: e["sort_order"] for e in resolved} == {"Consulta": 1, "Retorno": 0}

    tenant = Tenant(id=tenant_id, clinic_name="C", appointment_types=[])
    pros = [
        Professional(id=uuid4(), tenant_id=tenant_id, name="Ana", appointment_types=ana),
        Professional(
            id=uuid4(),
            tenant_id=tenant_id,
            name="Bruno",
            appointment_types=list(reversed(ana)),
        ),
    ]
    union = flow_router._clinic_service_catalog(tenant, pros, services)
    assert [s["name"] for s in union] == ["Retorno", "Consulta"]


async def test_the_doctors_real_list_follows_the_clinic_order_through_the_worker_path(db, tenant):
    """The live path: the worker resolves each doctor through the catalog
    (`_flow_professionals`) and the router lists that doctor's services from it. The doctor
    stored the opposite order; the clinic's order is what the patient's list shows."""
    from secretaria.workers.shared.greeting import _flow_professionals, _flow_tenant_snapshot

    first = await _service(db, tenant, "Retorno", 0)
    second = await _service(db, tenant, "Consulta", 1)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        doctor = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Ana",
            is_active=True,
            appointment_types=[
                {
                    "name": "Consulta",
                    "service_id": str(second.id),
                    "sort_order": 0,
                    "duration_min": 30,
                    "is_active": True,
                },
                {
                    "name": "Retorno",
                    "service_id": str(first.id),
                    "sort_order": 1,
                    "duration_min": 30,
                    "is_active": True,
                },
            ],
        )
        session.add(doctor)
        await session.commit()
        catalog = await load_service_catalog(session, tenant.id)
        pros = _flow_professionals([doctor], catalog)
        snapshot = _flow_tenant_snapshot(row, [doctor], catalog, None)
    result = flow_router._enter_professional_services(pros[0], snapshot)
    titles = [title for _key, title, *_ in result.bubbles[0].rows]
    assert [t for t in titles if "Consulta" in t or "Retorno" in t] == [
        flow_router._service_row_title("Retorno"),
        flow_router._service_row_title("Consulta"),
    ]
