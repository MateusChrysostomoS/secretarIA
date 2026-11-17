"""Saving a convênio list must not wipe the acceptance matrix (TASK-086 T3).

`set_professional_independent_plans` used to delete every catalog pick of the doctor and re-insert
the wanted ones. With the matrix hanging off those rows by CASCADE, that erased the doctor's
exceptions on every save. It is now a diff: kept picks keep their row (and their exceptions), a
dropped pick takes ITS exceptions with it (explicitly, SQLite does not cascade), a new pick starts
accepted everywhere. The clinic's list (`set_tenant_plans`) follows the same rule.
"""

import os
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Professional, Service, Tenant  # noqa: E402
from secretaria.models.insurance import ProfessionalInsurancePlan  # noqa: E402
from secretaria.models.insurance_acceptance_exception import (  # noqa: E402
    InsuranceAcceptanceException,
)
from secretaria.services.insurance_catalog import (  # noqa: E402
    catalog_id_for_slug,
    ensure_catalog_seeded,
    list_tenant_plans,
    set_professional_independent_plans,
    set_tenant_plans,
)

UNIMED = catalog_id_for_slug("unimed")
AMIL = catalog_id_for_slug("amil")
BRADESCO = catalog_id_for_slug("bradesco-saude")


@pytest_asyncio.fixture
async def session():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        await ensure_catalog_seeded(s)
        await s.commit()
        yield s
    await engine.dispose()


async def _setup(session):
    tenant = Tenant(
        id=uuid4(), clinic_name="C", phone_number_id=str(uuid4())[:12], insurance_mode="independent"
    )
    session.add(tenant)
    await session.flush()
    doctor = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    service = Service(
        id=uuid4(), tenant_id=tenant.id, name="Eco", normalized_name="eco", is_active=True
    )
    session.add_all([doctor, service])
    await session.flush()
    return tenant, doctor, service


async def _plan_row(session, doctor, catalog_id):
    return await session.scalar(
        select(ProfessionalInsurancePlan).where(
            ProfessionalInsurancePlan.professional_id == doctor.id,
            ProfessionalInsurancePlan.catalog_id == catalog_id,
        )
    )


async def _exceptions(session):
    return list(await session.scalars(select(InsuranceAcceptanceException)))


async def test_adding_a_plan_keeps_the_doctors_exceptions(session):
    tenant, doctor, service = await _setup(session)
    await set_professional_independent_plans(session, doctor, [str(UNIMED)])
    kept = await _plan_row(session, doctor, UNIMED)
    session.add(
        InsuranceAcceptanceException(
            tenant_id=tenant.id,
            professional_id=doctor.id,
            service_id=service.id,
            unit_id=None,
            professional_plan_id=kept.id,
        )
    )
    await session.commit()

    await set_professional_independent_plans(session, doctor, [str(UNIMED), str(AMIL)])
    await session.commit()

    same = await _plan_row(session, doctor, UNIMED)
    assert same.id == kept.id  # the row survived the save
    assert [e.professional_plan_id for e in await _exceptions(session)] == [kept.id]
    assert await _plan_row(session, doctor, AMIL) is not None


async def test_saving_the_same_list_changes_nothing(session):
    tenant, doctor, service = await _setup(session)
    await set_professional_independent_plans(session, doctor, [str(UNIMED), str(AMIL)])
    row = await _plan_row(session, doctor, AMIL)
    session.add(
        InsuranceAcceptanceException(
            tenant_id=tenant.id,
            professional_id=doctor.id,
            service_id=service.id,
            unit_id=None,
            professional_plan_id=row.id,
        )
    )
    await session.commit()
    result = await set_professional_independent_plans(session, doctor, [str(AMIL), str(UNIMED)])
    await session.commit()
    assert result == sorted([str(AMIL), str(UNIMED)])
    assert len(await _exceptions(session)) == 1


async def test_removing_a_plan_removes_only_its_exceptions(session):
    tenant, doctor, service = await _setup(session)
    await set_professional_independent_plans(session, doctor, [str(UNIMED), str(AMIL)])
    gone, stays = await _plan_row(session, doctor, UNIMED), await _plan_row(session, doctor, AMIL)
    for plan in (gone, stays):
        session.add(
            InsuranceAcceptanceException(
                tenant_id=tenant.id,
                professional_id=doctor.id,
                service_id=service.id,
                unit_id=None,
                professional_plan_id=plan.id,
            )
        )
    await session.commit()

    await set_professional_independent_plans(session, doctor, [str(AMIL)])
    await session.commit()

    assert await _plan_row(session, doctor, UNIMED) is None
    assert [e.professional_plan_id for e in await _exceptions(session)] == [stays.id]


async def test_the_doctors_custom_plan_and_exceptions_survive_a_catalog_save(session):
    tenant, doctor, service = await _setup(session)
    custom = ProfessionalInsurancePlan(
        id=uuid4(), tenant_id=tenant.id, professional_id=doctor.id, custom_name="Outro"
    )
    session.add(custom)
    await session.flush()
    session.add(
        InsuranceAcceptanceException(
            tenant_id=tenant.id,
            professional_id=doctor.id,
            service_id=service.id,
            unit_id=None,
            professional_plan_id=custom.id,
        )
    )
    await session.commit()
    await set_professional_independent_plans(session, doctor, [str(UNIMED)])
    await session.commit()
    assert len(await _exceptions(session)) == 1


async def test_clinic_plan_removal_takes_its_exceptions_and_a_kept_plan_keeps_them(session):
    tenant, _doctor, service = await _setup(session)
    tenant.insurance_mode = "shared"
    await set_tenant_plans(session, tenant, [(UNIMED, True), (BRADESCO, True)])
    plans = {entry.id: plan for plan, entry in await list_tenant_plans(session, tenant.id)}
    from secretaria.models.insurance_acceptance_exception import InsuranceAcceptanceException as E

    for plan in plans.values():
        session.add(
            E(
                tenant_id=tenant.id,
                professional_id=None,
                service_id=service.id,
                unit_id=None,
                tenant_plan_id=plan.id,
            )
        )
    await session.commit()

    await set_tenant_plans(session, tenant, [(UNIMED, False)])  # keeps Unimed, drops Bradesco
    await session.commit()

    assert [e.tenant_plan_id for e in await _exceptions(session)] == [plans[UNIMED].id]
