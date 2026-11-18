"""TASK-086 T3 on REAL Postgres 16: replacing a slice twice and saving a convênio list keep the matrix.

Complements tests/test_hub_insurance_acceptance_api.py / tests/test_insurance_save_keeps_matrix.py
(SQLite): here the partial unique indexes, FOR UPDATE and ON DELETE CASCADE are the real ones.
Skipped without ``T086_PG_URL``; throwaway loopback database named t086* only (same rules as
test_insurance_acceptance_pg.py); every tenant a test creates is deleted again.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.api.hub.insurance_acceptance import _replace_slice
from secretaria.models import (
    InsuranceAcceptanceException,
    Professional,
    ProfessionalInsurancePlan,
    Service,
    Tenant,
    Unit,
)
from secretaria.services.insurance_catalog import (
    catalog_id_for_slug,
    ensure_catalog_seeded,
    set_professional_independent_plans,
)

PG_URL = os.environ.get("T086_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="T086_PG_URL not set: real-Postgres proof (tests/postgres/README.md)"
)


@pytest.fixture
async def pg():
    parsed = make_url(PG_URL)
    if parsed.host not in {"127.0.0.1", "localhost", "::1"} or not (
        parsed.database or ""
    ).startswith("t086"):
        pytest.fail("T086_PG_URL must be a loopback throwaway database named t086*", pytrace=False)
    engine = create_async_engine(PG_URL)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    tenant_ids: list = []
    try:
        yield factory, tenant_ids
    finally:
        async with factory() as session, session.begin():
            if tenant_ids:
                await session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        await engine.dispose()


async def _world(session, tenant_ids):
    await ensure_catalog_seeded(session)
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=str(uuid4())[:12],
        insurance_mode="independent",
    )
    session.add(tenant)
    await session.flush()
    tenant_ids.append(tenant.id)
    prof = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    svc = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
    unit = Unit(tenant_id=tenant.id, name="Barra")
    session.add_all([prof, svc, unit])
    await session.flush()
    return tenant, prof, svc, unit


async def test_replacing_a_slice_with_the_same_cells_twice_is_clean(pg):
    factory, tenant_ids = pg
    async with factory() as session:
        tenant, prof, svc, unit = await _world(session, tenant_ids)
        plan = ProfessionalInsurancePlan(
            professional_id=prof.id, tenant_id=tenant.id, custom_name="Plano"
        )
        session.add(plan)
        await session.flush()
        cells = {(svc.id, plan.id)}
        for _ in range(2):  # delete + insert of the SAME key in one go (partial unique index)
            await _replace_slice(session, tenant.id, prof, unit.id, cells)
            await session.commit()
        rows = list(await session.scalars(select(InsuranceAcceptanceException)))
        assert len([r for r in rows if r.tenant_id == tenant.id]) == 1
        await _replace_slice(session, tenant.id, prof, unit.id, set())
        await session.commit()


async def test_saving_the_doctors_list_keeps_and_cascades_the_right_exceptions(pg):
    factory, tenant_ids = pg
    unimed, amil = str(catalog_id_for_slug("unimed")), str(catalog_id_for_slug("amil"))
    async with factory() as session:
        tenant, prof, svc, unit = await _world(session, tenant_ids)
        await set_professional_independent_plans(session, prof, [unimed, amil])
        picks = {
            p.catalog_id: p
            for p in await session.scalars(
                select(ProfessionalInsurancePlan).where(
                    ProfessionalInsurancePlan.professional_id == prof.id
                )
            )
        }
        for pick in picks.values():
            session.add(
                InsuranceAcceptanceException(
                    tenant_id=tenant.id,
                    professional_id=prof.id,
                    service_id=svc.id,
                    unit_id=unit.id,
                    professional_plan_id=pick.id,
                )
            )
        await session.commit()
        kept = picks[catalog_id_for_slug("amil")]
        await set_professional_independent_plans(session, prof, [amil])
        await session.commit()
        left = list(
            await session.scalars(
                select(InsuranceAcceptanceException).where(
                    InsuranceAcceptanceException.tenant_id == tenant.id
                )
            )
        )
        assert [r.professional_plan_id for r in left] == [kept.id]
