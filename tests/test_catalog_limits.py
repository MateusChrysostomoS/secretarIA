"""TASK-082 S4 - catalogue count limits, enforced on SAVE (CONTRACT.md §e).

40 services per doctor, 120 per clinic, 60 convênios per clinic
(`core/catalog_limits.py`). The rule is "no growth past the limit": a save is refused
with 422 {"code": "catalog_limit_exceeded", "limit", "count"} only when the resulting
count is above the limit AND above what is stored. A clinic already above a limit
loads, edits, reorders and deletes freely; it just cannot add.

Real in-memory SQLite, same fixtures as tests/test_list_order_task081.py.
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
from secretaria.core import catalog_limits as cl  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Professional, Tenant  # noqa: E402
from secretaria.models.insurance import TenantInsurancePlan  # noqa: E402
from secretaria.models.service import Service  # noqa: E402
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
    load_tenant_insurance,
)
from secretaria.services.service_catalog import normalize  # noqa: E402


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


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    async with db() as session:
        row = Tenant(
            id=uuid4(),
            clinic_name="Clinica",
            phone_number_id=str(uuid4())[:12],
            insurance_mode="shared",
            appointment_types=[],
        )
        session.add(row)
        await session.commit()
        return row


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


def _types(n: int, prefix: str = "Serviço") -> list[dict]:
    return [{"name": f"{prefix} {i:03d}", "duration_min": 30} for i in range(n)]


def _custom(name: str) -> dict:
    return {"custom_name": name, "custom_payment_note": "Reembolso pelo paciente."}


def _assert_limit(response, *, limit: int, count: int) -> None:
    assert response.status_code == 422, response.text
    assert response.json()["detail"] == {
        "code": "catalog_limit_exceeded",
        "limit": limit,
        "count": count,
    }


async def _catalog(db, tenant, n: int, *, active: bool = True, prefix: str = "Catálogo"):
    rows = []
    async with db() as session:
        for i in range(n):
            name = f"{prefix} {i:03d}"
            row = Service(
                id=uuid4(),
                tenant_id=tenant.id,
                name=name,
                normalized_name=normalize(name),
                sort_order=i,
                is_active=active,
            )
            session.add(row)
            rows.append(row)
        await session.commit()
    return rows


async def _doctor(db, tenant, appointment_types: list[dict] | None) -> Professional:
    async with db() as session:
        row = Professional(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Dra. Ana",
            is_active=True,
            appointment_types=appointment_types,
        )
        session.add(row)
        await session.commit()
        return row


async def _custom_plans(db, tenant, n: int) -> None:
    async with db() as session:
        for i in range(n):
            session.add(TenantInsurancePlan(tenant_id=tenant.id, custom_name=f"Plano {i:03d}"))
        await session.commit()


# --------------------------------------------------------------------------
# The rule itself
# --------------------------------------------------------------------------


def test_the_limits_are_the_owners_numbers():
    assert cl.SERVICES_PER_PROFESSIONAL == 40
    assert cl.SERVICES_PER_CLINIC == 120
    assert cl.INSURANCE_PLANS_PER_CLINIC == 60


@pytest.mark.parametrize(
    ("before", "after", "refused"),
    [
        (0, 40, False),  # at the limit is fine
        (0, 41, True),
        (40, 41, True),
        (45, 45, False),  # already above: editing in place
        (45, 44, False),  # already above: deleting
        (45, 46, True),  # already above: still cannot add
        (10, 5, False),
    ],
)
def test_no_growth_past_the_limit(before, after, refused):
    if refused:
        with pytest.raises(cl.CatalogLimitExceeded) as caught:
            cl.check_growth(limit=40, before=before, after=after)
        assert caught.value.detail() == {
            "code": "catalog_limit_exceeded",
            "limit": 40,
            "count": after,
        }
    else:
        cl.check_growth(limit=40, before=before, after=after)


# --------------------------------------------------------------------------
# 40 services per doctor
# --------------------------------------------------------------------------


async def test_doctor_config_refuses_the_41st_service(client: AsyncClient, db, tenant):
    doctor = await _doctor(db, tenant, [])
    ok = await client.put(
        f"/tenants/me/professionals/{doctor.id}/config", json={"appointment_types": _types(40)}
    )
    assert ok.status_code == 200, ok.text
    refused = await client.put(
        f"/tenants/me/professionals/{doctor.id}/config", json={"appointment_types": _types(41)}
    )
    _assert_limit(refused, limit=40, count=41)
    async with db() as session:
        stored = await session.get(Professional, doctor.id)
        assert len(stored.appointment_types) == 40  # nothing written


async def test_a_doctor_already_above_the_limit_keeps_editing_and_deleting(
    client: AsyncClient, db, tenant
):
    doctor = await _doctor(db, tenant, _types(45))
    edited = _types(45, prefix="Renomeado")
    same = await client.put(
        f"/tenants/me/professionals/{doctor.id}/config", json={"appointment_types": edited}
    )
    assert same.status_code == 200, same.text
    fewer = await client.put(
        f"/tenants/me/professionals/{doctor.id}/config", json={"appointment_types": _types(44)}
    )
    assert fewer.status_code == 200
    more = await client.put(
        f"/tenants/me/professionals/{doctor.id}/config", json={"appointment_types": _types(45)}
    )
    _assert_limit(more, limit=40, count=45)


async def test_going_back_to_inheritance_is_never_counted(client: AsyncClient, db, tenant):
    doctor = await _doctor(db, tenant, _types(45))
    response = await client.put(
        f"/tenants/me/professionals/{doctor.id}/config", json={"appointment_types": None}
    )
    assert response.status_code == 200


async def test_a_doctor_who_inherits_is_measured_against_what_they_offer(
    client: AsyncClient, db, tenant
):
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.appointment_types = _types(50)
        await session.commit()
    doctor = await _doctor(db, tenant, None)
    # Writing the inherited 50 as their own list adds nothing.
    response = await client.put(
        f"/tenants/me/professionals/{doctor.id}/config", json={"appointment_types": _types(50)}
    )
    assert response.status_code == 200, response.text


async def test_legacy_clinic_config_refuses_growth_past_40(client: AsyncClient, db, tenant):
    refused = await client.put("/tenants/me/config", json={"appointment_types": _types(41)})
    _assert_limit(refused, limit=40, count=41)
    ok = await client.put("/tenants/me/config", json={"appointment_types": _types(40)})
    assert ok.status_code == 200


async def test_aggregate_save_refuses_and_rolls_back_both_halves(client: AsyncClient, db, tenant):
    doctor = await _doctor(db, tenant, [])
    response = await client.put(
        "/tenants/me/configuration",
        json={
            "tenant": {"persona_notes": "Nova nota"},
            "professional_id": str(doctor.id),
            "professional": {"appointment_types": _types(41)},
        },
    )
    _assert_limit(response, limit=40, count=41)
    async with db() as session:
        stored = await session.get(Tenant, tenant.id)
        assert stored.persona_notes != "Nova nota"


async def test_creating_and_offering_a_service_to_a_full_doctor_is_refused(
    client: AsyncClient, db, tenant
):
    doctor = await _doctor(db, tenant, _types(40))
    response = await client.post(
        "/tenants/me/services",
        json={
            "name": "Mapeamento de Retina",
            "offer": {"professional_id": str(doctor.id), "duration_min": 30},
        },
    )
    _assert_limit(response, limit=40, count=41)
    listed = await client.get("/tenants/me/services")
    assert listed.json() == []  # the service was not created either


# --------------------------------------------------------------------------
# 120 services per clinic
# --------------------------------------------------------------------------


async def test_the_clinic_catalogue_refuses_the_121st_service(client: AsyncClient, db, tenant):
    await _catalog(db, tenant, 119)
    ok = await client.post("/tenants/me/services", json={"name": "Número 120"})
    assert ok.status_code == 201, ok.text
    refused = await client.post("/tenants/me/services", json={"name": "Número 121"})
    _assert_limit(refused, limit=120, count=121)


async def test_retired_services_do_not_count(client: AsyncClient, db, tenant):
    await _catalog(db, tenant, 119)
    await _catalog(db, tenant, 5, active=False, prefix="Antigo")
    ok = await client.post("/tenants/me/services", json={"name": "Número 120"})
    assert ok.status_code == 201, ok.text


async def test_a_clinic_above_the_limit_edits_retires_but_cannot_reactivate(
    client: AsyncClient, db, tenant
):
    rows = await _catalog(db, tenant, 125)
    edited = await client.patch(f"/tenants/me/services/{rows[0].id}", json={"description": "x"})
    assert edited.status_code == 200, edited.text
    retired = await client.patch(f"/tenants/me/services/{rows[1].id}", json={"is_active": False})
    assert retired.status_code == 200
    back = await client.patch(f"/tenants/me/services/{rows[1].id}", json={"is_active": True})
    _assert_limit(back, limit=120, count=125)
    order = await client.put("/tenants/me/services/order", json={"service_ids": [str(rows[2].id)]})
    assert order.status_code == 200


# --------------------------------------------------------------------------
# 60 convênios per clinic
# --------------------------------------------------------------------------


async def test_the_61st_clinic_convenio_is_refused(client: AsyncClient, db, tenant):
    await _custom_plans(db, tenant, 59)
    ok = await client.post("/tenants/me/insurance-plans/custom", json=_custom("Plano 60"))
    assert ok.status_code == 201, ok.text
    refused = await client.post("/tenants/me/insurance-plans/custom", json=_custom("Plano 61"))
    _assert_limit(refused, limit=60, count=61)
    async with db() as session:
        assert len((await load_tenant_insurance(session, tenant.id)).plans) == 60


async def test_catalogue_plans_count_with_the_custom_ones(client: AsyncClient, db, tenant):
    await _custom_plans(db, tenant, 59)
    unimed, amil = catalog_id_for_slug("unimed"), catalog_id_for_slug("amil")
    refused = await client.put(
        "/tenants/me/insurance-plans",
        json={"plans": [{"catalog_id": str(unimed)}, {"catalog_id": str(amil)}]},
    )
    _assert_limit(refused, limit=60, count=61)
    ok = await client.put(
        "/tenants/me/insurance-plans", json={"plans": [{"catalog_id": str(unimed)}]}
    )
    assert ok.status_code == 200, ok.text


async def test_a_clinic_above_the_convenio_limit_keeps_saving_its_plans(
    client: AsyncClient, db, tenant
):
    await _custom_plans(db, tenant, 65)
    unimed = catalog_id_for_slug("unimed")
    # Keeping no catalogue plan: the count does not grow.
    same = await client.put("/tenants/me/insurance-plans", json={"plans": []})
    assert same.status_code == 200, same.text
    grow = await client.put(
        "/tenants/me/insurance-plans", json={"plans": [{"catalog_id": str(unimed)}]}
    )
    _assert_limit(grow, limit=60, count=66)


async def test_independent_mode_counts_what_the_patient_sees(client: AsyncClient, db, tenant):
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.insurance_mode = "independent"
        await session.commit()
    doctor = await _doctor(db, tenant, [])
    for i in range(60):
        created = await client.post(
            f"/tenants/me/professionals/{doctor.id}/insurance-plans/custom",
            json=_custom(f"Próprio {i:03d}"),
        )
        assert created.status_code == 201, created.text
    refused = await client.post(
        f"/tenants/me/professionals/{doctor.id}/insurance-plans/custom",
        json=_custom("Próprio 061"),
    )
    _assert_limit(refused, limit=60, count=61)
