"""TASK-086 T2: the exceptions table's schema guarantees on REAL Postgres 16.

SQLite already covers the logic (tests/test_insurance_acceptance.py); what only Postgres proves is
the migration's own DDL: the partial unique indexes treat NULL unit/professional correctly, both
CHECKs hold, and ON DELETE CASCADE removes the rows (the tests build the schema with
`alembic upgrade head`, not `create_all`).

Skipped without ``T086_PG_URL`` (see tests/postgres/README.md). Throwaway database only: a loopback
host AND a database named t086*; every row a test creates is deleted again.
"""

import os
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.models import (
    InsuranceAcceptanceException,
    Professional,
    ProfessionalInsurancePlan,
    Service,
    Tenant,
    TenantInsurancePlan,
    Unit,
)
from secretaria.services.insurance_acceptance import is_accepted, load_acceptance_exceptions

PG_URL = os.environ.get("T086_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="T086_PG_URL not set: real-Postgres schema proof (tests/postgres/README.md)"
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


async def _world(session, tenant_ids, mode="shared"):
    tenant = Tenant(
        id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], insurance_mode=mode
    )
    session.add(tenant)
    await session.flush()
    tenant_ids.append(tenant.id)
    prof = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    svc = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
    unit = Unit(tenant_id=tenant.id, name="Barra")
    plan = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano")
    session.add_all([prof, svc, unit, plan])
    await session.flush()
    pplan = ProfessionalInsurancePlan(
        professional_id=prof.id, tenant_id=tenant.id, custom_name="Plano da Ana"
    )
    session.add(pplan)
    await session.flush()
    return tenant, prof, svc, unit, plan, pplan


def _row(tenant, svc, *, unit=None, prof=None, plan=None, pplan=None):
    return InsuranceAcceptanceException(
        tenant_id=tenant.id,
        professional_id=prof.id if prof else None,
        service_id=svc.id,
        unit_id=unit.id if unit else None,
        tenant_plan_id=plan.id if plan else None,
        professional_plan_id=pplan.id if pplan else None,
    )


async def _count(session):
    return await session.scalar(select(func.count()).select_from(InsuranceAcceptanceException))


async def test_checks_hold_in_postgres(pg):
    factory, ids = pg
    async with factory() as session:
        tenant, prof, svc, unit, plan, pplan = await _world(session, ids)
        bad_rows = [
            _row(tenant, svc, unit=unit),  # no plan
            _row(tenant, svc, prof=prof, plan=plan, pplan=pplan),  # both plans
            _row(tenant, svc, pplan=pplan),  # clinic matrix with a doctor plan
            _row(tenant, svc, prof=prof, plan=plan),  # doctor matrix with a clinic plan
        ]
        for bad in bad_rows:
            async with session.begin_nested():
                session.add(bad)
                with pytest.raises(IntegrityError):
                    await session.flush()
        await session.rollback()


@pytest.mark.parametrize("shape", ["clinic_unit", "clinic_no_unit", "prof_unit", "prof_no_unit"])
async def test_partial_unique_indexes_cover_null_shapes(pg, shape):
    factory, ids = pg
    async with factory() as session:
        tenant, prof, svc, unit, plan, pplan = await _world(session, ids)

        def build():
            kwargs = {"unit": unit if shape.endswith("_unit") and "no" not in shape else None}
            if shape.startswith("clinic"):
                return _row(tenant, svc, plan=plan, **kwargs)
            return _row(tenant, svc, prof=prof, pplan=pplan, **kwargs)

        session.add(build())
        await session.flush()
        async with session.begin_nested():
            session.add(build())
            with pytest.raises(IntegrityError):
                await session.flush()
        await session.rollback()


async def test_cascade_on_delete_in_postgres(pg):
    factory, ids = pg
    async with factory() as session:
        tenant, prof, svc, unit, plan, pplan = await _world(session, ids, "independent")
        session.add_all(
            [
                _row(tenant, svc, unit=unit, plan=plan),
                _row(tenant, svc, unit=unit, prof=prof, pplan=pplan),
            ]
        )
        await session.flush()
        assert await _count(session) == 2
        await session.delete(unit)  # both rows point at the unit
        await session.flush()
        await session.refresh(tenant)
        assert await _count(session) == 0
        # plan / service / professional / tenant cascades, one at a time
        for victim_kind in ("plan", "pplan", "service", "professional"):
            session.add_all(
                [
                    _row(tenant, svc, plan=plan),
                    _row(tenant, svc, prof=prof, pplan=pplan),
                ]
            )
            await session.flush()
            expected_left = {"plan": 1, "pplan": 1, "service": 0, "professional": 1}[victim_kind]
            victim = {"plan": plan, "pplan": pplan, "service": svc, "professional": prof}[
                victim_kind
            ]
            await session.delete(victim)
            await session.flush()
            assert await _count(session) == expected_left, victim_kind
            await session.execute(delete(InsuranceAcceptanceException))
            # rebuild what the loop just removed
            if victim_kind == "plan":
                plan = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano 2")
                session.add(plan)
            elif victim_kind == "pplan":
                pplan = ProfessionalInsurancePlan(
                    professional_id=prof.id, tenant_id=tenant.id, custom_name="Plano da Ana 2"
                )
                session.add(pplan)
            elif victim_kind == "service":
                svc = Service(tenant_id=tenant.id, name="Retorno", normalized_name="retorno")
                session.add(svc)
            await session.flush()
        await session.rollback()


async def test_loader_on_postgres_is_tenant_isolated(pg):
    factory, ids = pg
    async with factory() as session:
        t1, _p1, s1, u1, plan1, _ = await _world(session, ids)
        t2, _p2, s2, u2, plan2, _ = await _world(session, ids)
        session.add_all([_row(t1, s1, unit=u1, plan=plan1), _row(t2, s2, unit=u2, plan=plan2)])
        await session.flush()
        e1 = await load_acceptance_exceptions(session, t1.id, "shared")
        assert is_accepted(e1, "x", s1.id, u1.id, plan1.id) is False
        assert is_accepted(e1, "x", s2.id, u2.id, plan2.id) is True  # the other tenant's cell
        await session.rollback()
