"""TASK-086 T2 - the per-(professional, service, unit) convênio exceptions and the pure
acceptance rule on top of them.

Real in-memory SQLite (StaticPool + create_all), same pattern as tests/test_convenio_catalogo_db.py.
Foreign keys are switched ON for the connection (PRAGMA) so the CASCADE tests run for real here;
tests/postgres/test_insurance_acceptance_pg.py repeats the schema proof on Postgres 16.
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
from sqlalchemy import event, func, select  # noqa: E402
from sqlalchemy.exc import IntegrityError  # noqa: E402
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
    AcceptanceExceptions,
    accepting_units,
    accepts_anywhere,
    is_accepted,
    load_acceptance_exceptions,
)
from secretaria.services.unit_booking import UnitOption  # noqa: E402

S1, S2 = str(uuid4()), str(uuid4())
HOURS = {"monday": [{"start": "08:00", "end": "12:00"}]}

# --------------------------------------------------------------------------
# Fixtures + seed helpers
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


class World:
    """One clinic: a doctor, two services, two units, a clinic plan and a doctor plan."""

    def __init__(self, tenant, prof, svc_a, svc_b, unit_x, unit_y, clinic_plan, prof_plan):
        self.tenant = tenant
        self.prof = prof
        self.svc_a = svc_a
        self.svc_b = svc_b
        self.unit_x = unit_x
        self.unit_y = unit_y
        self.clinic_plan = clinic_plan
        self.prof_plan = prof_plan


async def _world(session, mode="shared") -> World:
    tenant = Tenant(
        id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12], insurance_mode=mode
    )
    session.add(tenant)
    await session.flush()
    prof = Professional(tenant_id=tenant.id, name="Dra. Ana", is_active=True)
    svc_a = Service(tenant_id=tenant.id, name="Consulta", normalized_name="consulta")
    svc_b = Service(tenant_id=tenant.id, name="Retorno", normalized_name="retorno")
    unit_x = Unit(tenant_id=tenant.id, name="Barra")
    unit_y = Unit(tenant_id=tenant.id, name="Centro")
    clinic_plan = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano da clinica")
    session.add_all([prof, svc_a, svc_b, unit_x, unit_y, clinic_plan])
    await session.flush()
    prof_plan = ProfessionalInsurancePlan(
        professional_id=prof.id, tenant_id=tenant.id, custom_name="Plano da Ana"
    )
    session.add(prof_plan)
    await session.flush()
    return World(tenant, prof, svc_a, svc_b, unit_x, unit_y, clinic_plan, prof_plan)


def _clinic_exc(w: World, service, unit, plan=None) -> InsuranceAcceptanceException:
    return InsuranceAcceptanceException(
        tenant_id=w.tenant.id,
        professional_id=None,
        service_id=service.id,
        unit_id=unit.id if unit is not None else None,
        tenant_plan_id=(plan or w.clinic_plan).id,
    )


def _prof_exc(w: World, service, unit, plan=None) -> InsuranceAcceptanceException:
    return InsuranceAcceptanceException(
        tenant_id=w.tenant.id,
        professional_id=w.prof.id,
        service_id=service.id,
        unit_id=unit.id if unit is not None else None,
        professional_plan_id=(plan or w.prof_plan).id,
    )


async def _count(session, *where) -> int:
    return await session.scalar(
        select(func.count()).select_from(InsuranceAcceptanceException).where(*where)
    )


# --------------------------------------------------------------------------
# Pure rule: no I/O
# --------------------------------------------------------------------------


def test_no_rows_accepts_everything():
    exc = AcceptanceExceptions()
    assert is_accepted(exc, "p1", "s1", "u1", "plan1") is True
    assert is_accepted(exc, "p1", "s1", None, "plan1") is True


def test_unknown_service_or_plan_is_always_accepted():
    exc = AcceptanceExceptions(clinic=frozenset({("s1", "u1", "plan1")}))
    assert is_accepted(exc, "p1", None, "u1", "plan1") is True  # service not picked yet
    assert is_accepted(exc, "p1", "s1", "u1", None) is True  # Particular / free-text plan


def test_exception_by_service_only_hits_that_service():
    exc = AcceptanceExceptions(clinic=frozenset({("s1", "u1", "plan1")}))
    assert is_accepted(exc, "p1", "s1", "u1", "plan1") is False
    assert is_accepted(exc, "p1", "s2", "u1", "plan1") is True
    assert is_accepted(exc, "p1", "s1", "u1", "plan2") is True


def test_exception_by_unit_only_hits_that_unit():
    exc = AcceptanceExceptions(clinic=frozenset({("s1", "u1", "plan1")}))
    assert is_accepted(exc, "p1", "s1", "u1", "plan1") is False
    assert is_accepted(exc, "p1", "s1", "u2", "plan1") is True
    assert is_accepted(exc, "p1", "s1", None, "plan1") is True  # no-unit slice is its own cell


def test_no_unit_slice_exception():
    exc = AcceptanceExceptions(clinic=frozenset({("s1", None, "plan1")}))
    assert is_accepted(exc, "p1", "s1", None, "plan1") is False
    assert is_accepted(exc, "p1", "s1", "u1", "plan1") is True


def test_mode1_clinic_matrix_applies_to_every_professional():
    exc = AcceptanceExceptions(clinic=frozenset({("s1", "u1", "plan1")}))
    assert is_accepted(exc, "p1", "s1", "u1", "plan1") is False
    assert is_accepted(exc, "p2", "s1", "u1", "plan1") is False


def test_mode2_matrix_is_per_professional():
    exc = AcceptanceExceptions(by_professional={"p1": frozenset({("s1", "u1", "plan1")})})
    assert is_accepted(exc, "p1", "s1", "u1", "plan1") is False
    assert is_accepted(exc, "p2", "s1", "u1", "plan1") is True


def _prof_with_units(*unit_ids, service_units=None):
    return SimpleNamespace(
        id="p1",
        units=[UnitOption(uid, "U", None, HOURS) for uid in unit_ids],
        service_units=service_units or {},
    )


def test_accepting_units_filters_and_keeps_order():
    ux, uy = uuid4(), uuid4()
    prof = _prof_with_units(ux, uy)
    exc = AcceptanceExceptions(clinic=frozenset({(S1, str(ux), "plan1")}))
    assert [u.id for u in accepting_units(exc, prof, S1, "plan1")] == [uy]
    assert accepts_anywhere(exc, prof, S1, "plan1") is True
    both = AcceptanceExceptions(clinic=frozenset({(S1, str(ux), "plan1"), (S1, str(uy), "plan1")}))
    assert accepting_units(both, prof, S1, "plan1") == []
    assert accepts_anywhere(both, prof, S1, "plan1") is False
    # other service / other plan: untouched
    assert len(accepting_units(both, prof, S2, "plan1")) == 2
    assert len(accepting_units(both, prof, S1, "plan2")) == 2


def test_accepting_units_respects_service_unit_marks():
    ux, uy, sid = uuid4(), uuid4(), uuid4()
    prof = _prof_with_units(ux, uy, service_units={sid: frozenset({ux})})
    assert [u.id for u in accepting_units(AcceptanceExceptions(), prof, str(sid), "plan1")] == [ux]


def test_accepts_anywhere_without_units_uses_no_unit_slice():
    prof = _prof_with_units()
    assert accepts_anywhere(AcceptanceExceptions(), prof, "s1", "plan1") is True
    exc = AcceptanceExceptions(clinic=frozenset({("s1", None, "plan1")}))
    assert accepts_anywhere(exc, prof, "s1", "plan1") is False
    assert accepts_anywhere(exc, prof, "s2", "plan1") is True


# --------------------------------------------------------------------------
# Schema: CHECKs, uniqueness, cascade
# --------------------------------------------------------------------------


async def test_check_exactly_one_plan_rejects_none(db):
    async with db() as session:
        w = await _world(session)
        session.add(
            InsuranceAcceptanceException(
                tenant_id=w.tenant.id, service_id=w.svc_a.id, unit_id=w.unit_x.id
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_check_exactly_one_plan_rejects_both(db):
    async with db() as session:
        w = await _world(session)
        session.add(
            InsuranceAcceptanceException(
                tenant_id=w.tenant.id,
                professional_id=w.prof.id,
                service_id=w.svc_a.id,
                tenant_plan_id=w.clinic_plan.id,
                professional_plan_id=w.prof_plan.id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_check_clinic_row_with_doctor_plan_rejected(db):
    async with db() as session:
        w = await _world(session)
        session.add(
            InsuranceAcceptanceException(
                tenant_id=w.tenant.id,
                service_id=w.svc_a.id,
                professional_plan_id=w.prof_plan.id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_check_doctor_row_with_clinic_plan_rejected(db):
    async with db() as session:
        w = await _world(session)
        session.add(
            InsuranceAcceptanceException(
                tenant_id=w.tenant.id,
                professional_id=w.prof.id,
                service_id=w.svc_a.id,
                tenant_plan_id=w.clinic_plan.id,
            )
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.parametrize(
    ("build", "with_unit"),
    [(_clinic_exc, True), (_clinic_exc, False), (_prof_exc, True), (_prof_exc, False)],
)
async def test_unique_cell_including_null_unit(db, build, with_unit):
    async with db() as session:
        w = await _world(session)
        unit = w.unit_x if with_unit else None
        session.add(build(w, w.svc_a, unit))
        await session.flush()
        session.add(build(w, w.svc_a, unit))
        with pytest.raises(IntegrityError):
            await session.flush()


async def test_distinct_cells_do_not_collide(db):
    async with db() as session:
        w = await _world(session)
        session.add_all(
            [
                _clinic_exc(w, w.svc_a, w.unit_x),
                _clinic_exc(w, w.svc_a, w.unit_y),
                _clinic_exc(w, w.svc_b, w.unit_x),
                _clinic_exc(w, w.svc_a, None),
                _prof_exc(w, w.svc_a, w.unit_x),
                _prof_exc(w, w.svc_a, None),
            ]
        )
        await session.flush()
        assert await _count(session) == 6


@pytest.mark.parametrize("victim", ["plan", "service", "unit", "professional", "tenant"])
async def test_cascade_removes_exceptions(db, victim):
    async with db() as session:
        w = await _world(session)
        session.add_all([_clinic_exc(w, w.svc_a, w.unit_x), _prof_exc(w, w.svc_a, w.unit_x)])
        await session.flush()
        assert await _count(session) == 2
        target = {
            "plan": [w.clinic_plan, w.prof_plan],
            "service": [w.svc_a],
            "unit": [w.unit_x],
            "professional": [w.prof],
            "tenant": [w.tenant],
        }[victim]
        for row in target:
            await session.delete(row)
            await session.flush()
        # deleting the professional cascades the doctor row only (the clinic row stays)
        assert await _count(session) == (1 if victim == "professional" else 0)


# --------------------------------------------------------------------------
# Loader: one query, tenant scoped, per-mode id space
# --------------------------------------------------------------------------


async def test_load_shared_returns_clinic_matrix_keyed_by_tenant_plan_id(db):
    async with db() as session:
        w = await _world(session, "shared")
        session.add_all([_clinic_exc(w, w.svc_a, w.unit_x), _clinic_exc(w, w.svc_b, None)])
        await session.flush()
        exc = await load_acceptance_exceptions(session, w.tenant.id, "shared")
        pid = str(w.clinic_plan.id)
        assert exc.clinic == frozenset(
            {(str(w.svc_a.id), str(w.unit_x.id), pid), (str(w.svc_b.id), None, pid)}
        )
        assert dict(exc.by_professional) == {}
        assert is_accepted(exc, str(w.prof.id), str(w.svc_a.id), str(w.unit_x.id), pid) is False


async def test_load_independent_translates_plan_ids_for_catalog_and_custom(db):
    from secretaria.services.insurance_catalog import (
        catalog_id_for_slug,
        ensure_catalog_seeded,
    )

    async with db() as session:
        await ensure_catalog_seeded(session)
        await session.flush()
        w = await _world(session, "independent")
        catalog_id = catalog_id_for_slug("unimed")
        catalog_plan = ProfessionalInsurancePlan(
            professional_id=w.prof.id, tenant_id=w.tenant.id, catalog_id=catalog_id
        )
        session.add(catalog_plan)
        await session.flush()
        session.add_all(
            [
                _prof_exc(w, w.svc_a, w.unit_x, w.prof_plan),
                _prof_exc(w, w.svc_a, w.unit_x, catalog_plan),
                _clinic_exc(w, w.svc_a, w.unit_x),  # clinic matrix is ignored in mode 2
            ]
        )
        await session.flush()
        exc = await load_acceptance_exceptions(session, w.tenant.id, "independent")
        sa, ux = str(w.svc_a.id), str(w.unit_x.id)
        assert exc.clinic == frozenset()
        assert exc.by_professional[str(w.prof.id)] == frozenset(
            {
                (sa, ux, str(w.prof_plan.id)),  # custom "Outro": the row's own id
                (sa, ux, str(catalog_id)),  # catalog pick: the catalog id
            }
        )


@pytest.mark.parametrize("mode", [None, "clinic_with_exceptions"])
async def test_load_other_modes_is_empty(db, mode):
    async with db() as session:
        w = await _world(session, "shared")
        session.add(_clinic_exc(w, w.svc_a, w.unit_x))
        await session.flush()
        exc = await load_acceptance_exceptions(session, w.tenant.id, mode)
        assert exc == AcceptanceExceptions()


async def test_load_is_tenant_isolated(db):
    async with db() as session:
        w1 = await _world(session, "shared")
        w2 = await _world(session, "shared")
        session.add_all([_clinic_exc(w1, w1.svc_a, w1.unit_x), _clinic_exc(w2, w2.svc_b, None)])
        await session.flush()
        e1 = await load_acceptance_exceptions(session, w1.tenant.id, "shared")
        e2 = await load_acceptance_exceptions(session, w2.tenant.id, "shared")
        assert {k[0] for k in e1.clinic} == {str(w1.svc_a.id)}
        assert {k[0] for k in e2.clinic} == {str(w2.svc_b.id)}


@pytest.mark.parametrize("mode", ["shared", "independent"])
async def test_load_is_a_single_query(db, mode):
    async with db() as session:
        w = await _world(session, mode)
        extra = [_clinic_exc, _prof_exc][mode == "independent"]
        session.add_all([extra(w, w.svc_a, w.unit_x), extra(w, w.svc_b, w.unit_y)])
        await session.flush()
        statements: list[str] = []

        def _record(_conn, _cur, statement, *_args):
            statements.append(statement)

        sync_engine = session.bind.sync_engine
        event.listen(sync_engine, "before_cursor_execute", _record)
        try:
            await load_acceptance_exceptions(session, w.tenant.id, mode)
        finally:
            event.remove(sync_engine, "before_cursor_execute", _record)
        assert len(statements) == 1
