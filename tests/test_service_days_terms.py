# ruff: noqa: E402
"""TASK-087 T1: blocked weekdays per (professional, service, unit) - model + pure resolver.

Rule (SPEC 5.1): the BASE is the `blocked_weekdays` list on the professional's entry; a unit row is
an EXCEPTION: NULL = inherit the base, [] = own (nothing blocked there), list = own. A row that
carries only days is valid and still resolves price/duration to the base.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.core.database import Base
from secretaria.models import (
    Professional,
    ProfessionalServiceUnitTerms,
    Service,
    Tenant,
    Unit,
)
from secretaria.services.service_terms import (
    WEEKDAY_KEYS,
    ResolvedTerms,
    UnitTerms,
    blocked_weekdays_everywhere,
    blocked_weekdays_for,
    load_terms,
    resolve_terms,
)

U1, U2 = uuid4(), uuid4()


def test_weekday_keys_follow_business_hours_names():
    assert WEEKDAY_KEYS == (
        "monday",
        "tuesday",
        "wednesday",
        "thursday",
        "friday",
        "saturday",
        "sunday",
    )


# ---------------------------------------------------------------- pure resolver


def test_base_only():
    entry = {"service_id": "s", "blocked_weekdays": ["monday"]}
    assert blocked_weekdays_for(entry, None, U1) == frozenset({"monday"})


def test_no_base_and_no_unit_row_blocks_nothing():
    assert blocked_weekdays_for({"service_id": "s"}, None, U1) == frozenset()
    assert (
        blocked_weekdays_for({"service_id": "s", "blocked_weekdays": None}, {}, U1) == frozenset()
    )
    assert (
        blocked_weekdays_for({"service_id": "s", "blocked_weekdays": []}, {}, None) == frozenset()
    )


def test_unit_own_overrides_base():
    entry = {"service_id": "s", "blocked_weekdays": ["monday"]}
    terms = {U1: UnitTerms(None, None, frozenset({"friday"}))}
    assert blocked_weekdays_for(entry, terms, U1) == frozenset({"friday"})
    assert blocked_weekdays_for(entry, terms, U2) == frozenset({"monday"})


def test_unit_empty_list_unblocks():
    entry = {"service_id": "s", "blocked_weekdays": ["monday"]}
    terms = {U1: UnitTerms(None, None, frozenset())}
    assert blocked_weekdays_for(entry, terms, U1) == frozenset()


def test_unit_row_without_days_inherits_the_base():
    entry = {"service_id": "s", "blocked_weekdays": ["monday"]}
    terms = {U1: UnitTerms("R$ 250,00", None)}  # blocked_weekdays defaults to None = inherit
    assert blocked_weekdays_for(entry, terms, U1) == frozenset({"monday"})


def test_unit_id_none_is_the_base_even_with_unit_rows():
    entry = {"service_id": "s", "blocked_weekdays": ["monday"]}
    terms = {U1: UnitTerms(None, None, frozenset({"friday"}))}
    assert blocked_weekdays_for(entry, terms, None) == frozenset({"monday"})


def test_everywhere_is_intersection():
    entry = {"service_id": "s", "blocked_weekdays": ["monday", "friday"]}
    terms = {U1: UnitTerms(None, None, frozenset({"monday"}))}
    # U1 blocks {monday}, U2 inherits {monday, friday}: only monday is blocked in every unit (R1)
    assert blocked_weekdays_everywhere(entry, terms, [U1, U2]) == frozenset({"monday"})


def test_everywhere_with_a_unit_that_unblocks_everything_is_empty():
    entry = {"service_id": "s", "blocked_weekdays": ["monday"]}
    terms = {U2: UnitTerms(None, None, frozenset())}
    assert blocked_weekdays_everywhere(entry, terms, [U1, U2]) == frozenset()


def test_everywhere_without_units_is_the_base():
    entry = {"service_id": "s", "blocked_weekdays": ["monday", "friday"]}
    assert blocked_weekdays_everywhere(entry, None, []) == frozenset({"monday", "friday"})
    assert blocked_weekdays_everywhere(entry, {}, ()) == frozenset({"monday", "friday"})


def test_days_only_row_keeps_base_price():
    entry = {"service_id": "s", "price": "200", "duration_min": 30}
    terms = {U1: UnitTerms(None, None, frozenset({"friday"}))}
    assert resolve_terms(entry, terms, U1) == ResolvedTerms("200", 30, own=False)


# ---------------------------------------------------------------- model on SQLite (FKs ON)


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


async def _world(db, name="Clinica"):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name=name, phone_number_id=None, is_active=True)
        session.add(tenant)
        await session.flush()
        service = Service(
            id=uuid4(), tenant_id=tenant.id, name="Cirurgia", normalized_name="cirurgia"
        )
        doctor = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        sede = Unit(id=uuid4(), tenant_id=tenant.id, name="Sede")
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro")
        session.add_all([service, doctor, sede, centro])
        await session.commit()
    return SimpleNamespace(tenant=tenant, service=service, doctor=doctor, sede=sede, centro=centro)


def _row(w, unit, **kw):
    return ProfessionalServiceUnitTerms(
        tenant_id=w.tenant.id,
        professional_id=w.doctor.id,
        service_id=w.service.id,
        unit_id=unit.id,
        **kw,
    )


@pytest.mark.asyncio
async def test_row_with_only_days_is_accepted_even_if_empty_list(db):
    w = await _world(db)
    async with db() as s:
        s.add_all(
            [_row(w, w.sede, blocked_weekdays=["friday"]), _row(w, w.centro, blocked_weekdays=[])]
        )
        await s.commit()


@pytest.mark.asyncio
async def test_check_still_refuses_a_row_with_nothing_set(db):
    w = await _world(db)
    async with db() as s:
        s.add(_row(w, w.sede))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_load_terms_reads_blocked(db):
    w = await _world(db)
    async with db() as s:
        s.add_all(
            [
                _row(w, w.sede, blocked_weekdays=["tuesday"]),
                _row(w, w.centro, price="R$ 200,00"),
            ]
        )
        await s.commit()
    async with db() as s:
        got = await load_terms(s, w.tenant.id, w.doctor.id)
    assert got == {
        w.service.id: {
            w.sede.id: UnitTerms(None, None, frozenset({"tuesday"})),
            w.centro.id: UnitTerms("R$ 200,00", None, None),
        }
    }
    assert got[w.service.id][w.sede.id].blocked_weekdays == frozenset({"tuesday"})
    assert got[w.service.id][w.centro.id].blocked_weekdays is None


@pytest.mark.asyncio
async def test_load_terms_distinguishes_empty_list_from_null(db):
    w = await _world(db)
    async with db() as s:
        s.add(_row(w, w.sede, blocked_weekdays=[]))
        await s.commit()
    async with db() as s:
        got = await load_terms(s, w.tenant.id, w.doctor.id)
    assert got[w.service.id][w.sede.id].blocked_weekdays == frozenset()


@pytest.mark.asyncio
async def test_python_none_everywhere_is_sql_null_so_the_check_refuses_it(db):
    """D2: a JSON column would store the literal `null` and slip past the CHECK."""
    w = await _world(db)
    async with db() as s:
        s.add(_row(w, w.sede, price=None, duration_min=None, blocked_weekdays=None))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_none_days_with_a_price_reads_back_as_none_inherit(db):
    from sqlalchemy import select

    w = await _world(db)
    async with db() as s:
        s.add(_row(w, w.sede, price="R$ 10,00", blocked_weekdays=None))
        await s.commit()
    async with db() as s:
        row = await s.scalar(select(ProfessionalServiceUnitTerms))
        assert row.blocked_weekdays is None
