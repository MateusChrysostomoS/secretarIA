# ruff: noqa: E402
"""TASK-085 T2: price/duration per (professional, service, unit) - model + pure resolver.

Rule: a unit exception beats the professional's base value; no row = base; a row with only a
price (or only a duration) inherits the other half from the base. D3: the price is hidden when it
differs across the units where the service is done (never a range).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import delete, event, select
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
    ResolvedTerms,
    UnitTerms,
    load_terms,
    load_terms_for,
    price_differs_across_units,
    resolve_terms,
)

U1, U2, U3 = uuid4(), uuid4(), uuid4()
BASE = {"service_id": "s", "price": "R$ 200,00", "duration_min": 30}


# ---------------------------------------------------------------- pure resolver


def test_unit_beats_base():
    terms = {U1: UnitTerms(price="R$ 250,00", duration_min=45)}
    assert resolve_terms(BASE, terms, U1) == ResolvedTerms("R$ 250,00", 45, own=True)


def test_no_row_for_the_unit_is_the_base():
    terms = {U1: UnitTerms(price="R$ 250,00", duration_min=45)}
    assert resolve_terms(BASE, terms, U2) == ResolvedTerms("R$ 200,00", 30, own=False)


def test_unknown_unit_or_no_terms_at_all_is_the_base():
    assert resolve_terms(BASE, None, None) == ResolvedTerms("R$ 200,00", 30, own=False)
    assert resolve_terms(BASE, {}, U1) == ResolvedTerms("R$ 200,00", 30, own=False)
    terms = {U1: UnitTerms(price="R$ 250,00", duration_min=45)}
    assert resolve_terms(BASE, terms, None) == ResolvedTerms("R$ 200,00", 30, own=False)  # D7


def test_price_only_row_inherits_the_base_duration():
    terms = {U1: UnitTerms(price="R$ 250,00", duration_min=None)}
    assert resolve_terms(BASE, terms, U1) == ResolvedTerms("R$ 250,00", 30, own=True)


def test_duration_only_row_inherits_the_base_price():
    terms = {U1: UnitTerms(price=None, duration_min=45)}
    assert resolve_terms(BASE, terms, U1) == ResolvedTerms("R$ 200,00", 45, own=True)


def test_base_without_price_or_duration_stays_empty():
    entry = {"service_id": "s"}
    assert resolve_terms(entry, {}, U1) == ResolvedTerms(None, None, own=False)
    terms = {U1: UnitTerms(price="R$ 90,00", duration_min=None)}
    assert resolve_terms(entry, terms, U1) == ResolvedTerms("R$ 90,00", None, own=True)


def test_blank_base_price_is_none():
    assert resolve_terms({"price": "  "}, {}, None).price is None


def test_differs_when_two_units_have_different_prices():
    terms = {U1: UnitTerms(price="R$ 250,00", duration_min=None)}
    assert price_differs_across_units(BASE, terms, [U1, U2]) is True


def test_same_price_everywhere_does_not_differ():
    assert price_differs_across_units(BASE, {}, [U1, U2, U3]) is False
    terms = {U1: UnitTerms(price="250", duration_min=None), U2: UnitTerms("R$ 250,00", None)}
    # "250" and "R$ 250,00" are the same money
    assert price_differs_across_units(BASE, terms, [U1, U2]) is False


def test_one_or_zero_units_never_differ():
    terms = {U1: UnitTerms(price="R$ 250,00", duration_min=None)}
    assert price_differs_across_units(BASE, terms, [U1]) is False
    assert price_differs_across_units(BASE, terms, []) is False


def test_duration_only_rows_do_not_make_the_price_differ():
    terms = {U1: UnitTerms(price=None, duration_min=45)}
    assert price_differs_across_units(BASE, terms, [U1, U2]) is False


def test_units_outside_the_considered_set_are_ignored():
    terms = {U3: UnitTerms(price="R$ 999,00", duration_min=None)}
    assert price_differs_across_units(BASE, terms, [U1, U2]) is False


def test_priced_vs_unpriced_differs_and_free_text_compares_casefolded():
    entry = {"price": None}
    terms = {U1: UnitTerms(price="R$ 90,00", duration_min=None)}
    assert price_differs_across_units(entry, terms, [U1, U2]) is True
    e2 = {"price": "A Combinar"}
    t2 = {U1: UnitTerms(price="a combinar", duration_min=None)}
    assert price_differs_across_units(e2, t2, [U1, U2]) is False


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
            id=uuid4(), tenant_id=tenant.id, name="Consulta", normalized_name="consulta"
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
async def test_row_with_price_only_and_duration_only_are_accepted(db):
    w = await _world(db)
    async with db() as s:
        s.add_all([_row(w, w.sede, price="R$ 200,00"), _row(w, w.centro, duration_min=45)])
        await s.commit()


@pytest.mark.asyncio
async def test_check_refuses_a_row_with_neither_price_nor_duration(db):
    w = await _world(db)
    async with db() as s:
        s.add(_row(w, w.sede))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_check_refuses_non_positive_duration(db):
    w = await _world(db)
    async with db() as s:
        s.add(_row(w, w.sede, price="R$ 1,00", duration_min=0))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
async def test_one_row_per_professional_service_unit(db):
    w = await _world(db)
    async with db() as s:
        s.add(_row(w, w.sede, price="R$ 200,00"))
        await s.commit()
    async with db() as s:
        s.add(_row(w, w.sede, price="R$ 300,00"))
        with pytest.raises(IntegrityError):
            await s.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("parent", ["doctor", "service", "sede"])
async def test_deleting_a_parent_cascades_to_the_terms(db, parent):
    w = await _world(db)
    obj = getattr(w, parent)
    cls = type(obj)
    async with db() as s:
        s.add(_row(w, w.sede, price="R$ 200,00"))
        await s.commit()
    async with db() as s:
        await s.execute(delete(cls).where(cls.id == obj.id))
        await s.commit()
    async with db() as s:
        assert (await s.execute(select(ProfessionalServiceUnitTerms))).all() == []


@pytest.mark.asyncio
async def test_deleting_the_tenant_cascades(db):
    w = await _world(db)
    async with db() as s:
        s.add(_row(w, w.sede, price="R$ 200,00"))
        await s.commit()
    async with db() as s:
        # FKs are ON: remove the tenant's other children first, then the tenant itself.
        await s.execute(delete(ProfessionalServiceUnitTerms).where(False))
        await s.execute(delete(Tenant).where(Tenant.id == w.tenant.id))
        await s.commit()
        assert (await s.execute(select(ProfessionalServiceUnitTerms))).all() == []


@pytest.mark.asyncio
async def test_loader_returns_the_nested_map_and_is_tenant_scoped(db):
    w = await _world(db, "A")
    other = await _world(db, "B")
    async with db() as s:
        s.add_all([_row(w, w.sede, price="R$ 200,00"), _row(w, w.centro, duration_min=45)])
        # a row of another tenant's professional must not be visible through tenant A
        s.add(
            ProfessionalServiceUnitTerms(
                tenant_id=other.tenant.id,
                professional_id=other.doctor.id,
                service_id=other.service.id,
                unit_id=other.sede.id,
                price="R$ 999,00",
            )
        )
        await s.commit()
    async with db() as s:
        got = await load_terms_for(s, w.tenant.id, [w.doctor.id, other.doctor.id])
        assert got[w.doctor.id] == {
            w.service.id: {
                w.sede.id: UnitTerms("R$ 200,00", None),
                w.centro.id: UnitTerms(None, 45),
            }
        }
        assert got[other.doctor.id] == {}  # requested, but not this tenant's: empty, not leaked
        single = await load_terms(s, w.tenant.id, w.doctor.id)
        assert single == got[w.doctor.id]
        assert await load_terms(s, w.tenant.id, other.doctor.id) == {}
        assert await load_terms_for(s, w.tenant.id, []) == {}
