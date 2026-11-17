"""TASK-086 T4 (shared base) - the matrix reaching the flow snapshot, the "refused because of the
matrix" helpers every call site uses, and the write-time guard read straight from the database.

Real in-memory SQLite with foreign keys ON (fixture shape of tests/test_insurance_acceptance.py).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import event  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Professional,
    ProfessionalInsurancePlan,
    Service,
    Tenant,
    TenantInsurancePlan,
    Unit,
)
from secretaria.models.insurance_acceptance_exception import (  # noqa: E402
    InsuranceAcceptanceException,
)
from secretaria.services.insurance_acceptance import (  # noqa: E402
    EMPTY,
    AcceptanceExceptions,
    booking_refused,
    booking_refused_by_ids,
    refuses_all_services,
    refuses_everywhere,
    snapshot_exceptions,
)
from secretaria.services.insurance_catalog import load_tenant_insurance  # noqa: E402
from secretaria.services.unit_booking import UnitOption  # noqa: E402
from secretaria.workers.shared.greeting import _flow_tenant_snapshot  # noqa: E402

HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}


def _prof(*unit_ids, service_units=None, closed=()):
    return SimpleNamespace(
        id="p1",
        units=[UnitOption(uid, "U", None, None if uid in closed else HOURS) for uid in unit_ids],
        service_units=service_units or {},
    )


def _bare_tenant():
    return SimpleNamespace(
        initial_flows=None,
        appointment_types=[],
        appointment_duration_min=30,
        business_hours={},
        collect_insurance=True,
        insurances=[],
    )


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_snapshot_exceptions_defaults_to_empty():
    assert snapshot_exceptions(SimpleNamespace()) is EMPTY
    assert snapshot_exceptions(SimpleNamespace(insurance_refused=None)) is EMPTY
    exc = AcceptanceExceptions(clinic=frozenset({("s", None, "p")}))
    assert snapshot_exceptions(SimpleNamespace(insurance_refused=exc)) is exc


def test_refuses_everywhere_is_false_without_exceptions_even_when_units_are_closed():
    ux, sid = uuid4(), uuid4()
    prof = _prof(ux, closed=(ux,))
    assert refuses_everywhere(EMPTY, prof, str(sid), "plan1") is False


def test_refuses_everywhere_with_units():
    ux, uy, sid = uuid4(), uuid4(), uuid4()
    prof = _prof(ux, uy)
    one = AcceptanceExceptions(clinic=frozenset({(str(sid), str(ux), "plan1")}))
    both = AcceptanceExceptions(
        clinic=frozenset({(str(sid), str(ux), "plan1"), (str(sid), str(uy), "plan1")})
    )
    assert refuses_everywhere(one, prof, str(sid), "plan1") is False
    assert refuses_everywhere(both, prof, str(sid), "plan1") is True
    assert refuses_everywhere(both, prof, str(sid), "plan2") is False
    assert refuses_everywhere(both, prof, None, "plan1") is False
    assert refuses_everywhere(both, prof, str(sid), None) is False


def test_refuses_everywhere_follows_service_unit_marks():
    ux, uy, sid = uuid4(), uuid4(), uuid4()
    prof = _prof(ux, uy, service_units={sid: frozenset({ux})})
    exc = AcceptanceExceptions(clinic=frozenset({(str(sid), str(ux), "plan1")}))
    # the service is only done at ux, and ux refuses -> refused everywhere it can be booked
    assert refuses_everywhere(exc, prof, str(sid), "plan1") is True


def test_refuses_everywhere_with_all_units_closed_is_not_the_matrix():
    ux, sid = uuid4(), uuid4()
    prof = _prof(ux, closed=(ux,))
    exc = AcceptanceExceptions(clinic=frozenset({(str(sid), str(ux), "plan1")}))
    assert refuses_everywhere(exc, prof, str(sid), "plan1") is False


def test_refuses_everywhere_without_units_uses_no_unit_slice():
    sid = uuid4()
    prof = _prof()
    exc = AcceptanceExceptions(by_professional={"p1": frozenset({(str(sid), None, "plan1")})})
    assert refuses_everywhere(exc, prof, str(sid), "plan1") is True
    other = SimpleNamespace(id="p2", units=[], service_units={})
    assert refuses_everywhere(exc, other, str(sid), "plan1") is False


def test_refuses_all_services():
    s1, s2 = uuid4(), uuid4()
    prof = _prof()
    services = [{"name": "A", "service_id": str(s1)}, {"name": "B", "service_id": str(s2)}]
    only_one = AcceptanceExceptions(clinic=frozenset({(str(s1), None, "plan1")}))
    both = AcceptanceExceptions(
        clinic=frozenset({(str(s1), None, "plan1"), (str(s2), None, "plan1")})
    )
    assert refuses_all_services(EMPTY, prof, services, "plan1") is False
    assert refuses_all_services(only_one, prof, services, "plan1") is False
    assert refuses_all_services(both, prof, services, "plan1") is True
    assert refuses_all_services(both, prof, [], "plan1") is False
    # a legacy entry without service_id is outside the matrix -> always accepts
    assert refuses_all_services(both, prof, [*services, {"name": "C"}], "plan1") is False


# --------------------------------------------------------------------------
# DB: snapshot plumbing + write-time guard
# --------------------------------------------------------------------------


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _world(session, mode):
    tenant = Tenant(
        id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], insurance_mode=mode
    )
    session.add(tenant)
    await session.flush()
    prof = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    svc = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
    unit = Unit(tenant_id=tenant.id, name="Barra")
    clinic_plan = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano Clinica")
    session.add_all([prof, svc, unit, clinic_plan])
    await session.flush()
    prof_plan = ProfessionalInsurancePlan(
        professional_id=prof.id, tenant_id=tenant.id, custom_name="Plano Ana"
    )
    session.add(prof_plan)
    await session.flush()
    return SimpleNamespace(
        tenant=tenant, prof=prof, svc=svc, unit=unit, clinic_plan=clinic_plan, prof_plan=prof_plan
    )


@pytest.mark.asyncio
async def test_loader_keeps_refused_none_when_matrix_untouched(db):
    async with db() as session:
        w = await _world(session, "shared")
        await session.commit()
        insurance = await load_tenant_insurance(session, w.tenant.id)
    assert insurance.refused is None
    snap = _flow_tenant_snapshot(_bare_tenant(), [], insurance=insurance)
    assert snapshot_exceptions(snap) is EMPTY


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_loader_and_snapshot_carry_the_matrix(db, mode):
    async with db() as session:
        w = await _world(session, mode)
        if mode == "shared":
            row = InsuranceAcceptanceException(
                tenant_id=w.tenant.id,
                service_id=w.svc.id,
                unit_id=w.unit.id,
                tenant_plan_id=w.clinic_plan.id,
            )
        else:
            row = InsuranceAcceptanceException(
                tenant_id=w.tenant.id,
                professional_id=w.prof.id,
                service_id=w.svc.id,
                unit_id=w.unit.id,
                professional_plan_id=w.prof_plan.id,
            )
        session.add(row)
        await session.commit()
        insurance = await load_tenant_insurance(session, w.tenant.id)
    assert isinstance(insurance.refused, AcceptanceExceptions)
    snap = _flow_tenant_snapshot(_bare_tenant(), [], insurance=insurance)
    exc = snapshot_exceptions(snap)
    plan_id = str(w.clinic_plan.id) if mode == "shared" else str(w.prof_plan.id)
    cells = exc.clinic if mode == "shared" else exc.by_professional[str(w.prof.id)]
    assert (str(w.svc.id), str(w.unit.id), plan_id) in cells


@pytest.mark.asyncio
async def test_booking_refused_shared(db):
    async with db() as session:
        w = await _world(session, "shared")
        session.add(
            InsuranceAcceptanceException(
                tenant_id=w.tenant.id,
                service_id=w.svc.id,
                unit_id=w.unit.id,
                tenant_plan_id=w.clinic_plan.id,
            )
        )
        await session.commit()
        common = dict(tenant_id=w.tenant.id, professional_id=w.prof.id, service_id=w.svc.id)
        assert await booking_refused(
            session, unit_id=w.unit.id, insurance="Plano Clinica", **common
        )
        # other slice / Particular / no plan / unknown service: not refused
        assert not await booking_refused(session, unit_id=None, insurance="Plano Clinica", **common)
        assert not await booking_refused(
            session, unit_id=w.unit.id, insurance="Particular", **common
        )
        assert not await booking_refused(session, unit_id=w.unit.id, insurance=None, **common)
        assert not await booking_refused(
            session,
            tenant_id=w.tenant.id,
            professional_id=w.prof.id,
            service_id=None,
            unit_id=w.unit.id,
            insurance="Plano Clinica",
        )


@pytest.mark.asyncio
async def test_booking_refused_independent_and_mode_switch(db):
    async with db() as session:
        w = await _world(session, "independent")
        session.add(
            InsuranceAcceptanceException(
                tenant_id=w.tenant.id,
                professional_id=w.prof.id,
                service_id=w.svc.id,
                unit_id=None,
                professional_plan_id=w.prof_plan.id,
            )
        )
        await session.commit()
        kwargs = dict(
            tenant_id=w.tenant.id,
            professional_id=w.prof.id,
            service_id=w.svc.id,
            unit_id=None,
            insurance="Plano Ana",
        )
        assert await booking_refused(session, **kwargs)
        # the matrix only counts in its own mode
        w.tenant.insurance_mode = "shared"
        await session.commit()
        assert not await booking_refused(session, **kwargs)


@pytest.mark.asyncio
async def test_booking_refused_is_tenant_scoped(db):
    async with db() as session:
        a = await _world(session, "shared")
        b = await _world(session, "shared")
        session.add(
            InsuranceAcceptanceException(
                tenant_id=a.tenant.id,
                service_id=a.svc.id,
                unit_id=a.unit.id,
                tenant_plan_id=a.clinic_plan.id,
            )
        )
        await session.commit()
        # tenant B asking with A's ids never sees A's row
        assert not await booking_refused_by_ids(
            session,
            tenant_id=b.tenant.id,
            professional_id=b.prof.id,
            service_id=a.svc.id,
            unit_id=a.unit.id,
            tenant_plan_id=a.clinic_plan.id,
            professional_plan_id=None,
        )
