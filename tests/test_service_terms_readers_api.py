# ruff: noqa: F811
"""TASK-085 T4-0: the per-unit exceptions reach every reader (snapshot field + helpers)."""

import os

os.environ.setdefault("APP_ENV", "test")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402

from secretaria.models import (  # noqa: E402
    ProfessionalServiceUnitTerms,
    ProfessionalUnit,
    Service,
    Unit,
)
from secretaria.services import (
    flow_units as fu,  # noqa: E402
    service_terms as st,  # noqa: E402
)
from secretaria.services.service_terms import UnitTerms  # noqa: E402
from secretaria.services.unit_booking import (  # noqa: E402
    UnitOption,
    attach_unit_options,
    professional_terms_view,
)
from tests._local_agenda import seed_professional, seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

OPEN = {"monday": [{"start": "08:00", "end": "12:00"}]}
SID = uuid4()
ENTRY = {"service_id": str(SID), "price": "R$ 200,00", "duration_min": 30}
U1, U2 = uuid4(), uuid4()


def _unit(name="A") -> UnitOption:
    return UnitOption(id=uuid4(), name=name, address=None, business_hours=OPEN)


def _doc(units, terms=None, marks=None):
    snap = SimpleNamespace(id=uuid4(), name="Dra. Ana", units=units, service_units=marks or {})
    if terms is not None:
        snap.service_unit_terms = terms
    return snap


class _NoQuery:
    async def execute(self, *a, **k):
        raise AssertionError("no query expected")


# ------------------------------------------------------------- terms_for_entry


def test_terms_for_entry():
    terms = {SID: {U1: UnitTerms("R$ 250,00", None)}}
    assert st.terms_for_entry(terms, ENTRY) == terms[SID]
    assert st.terms_for_entry(terms, {"price": "1"}) is None
    assert st.terms_for_entry(terms, {"service_id": "nope"}) is None
    assert st.terms_for_entry(terms, {"service_id": str(uuid4())}) is None
    assert st.terms_for_entry(None, ENTRY) is None
    assert st.terms_for_entry({}, ENTRY) is None


# ------------------------------------------------------- resolve_entry_at_unit


async def _world(db):
    tenant = await seed_tenant(db)
    doc = await seed_professional(db, tenant, "Dra. Ana")
    async with db() as session:
        service = Service(
            id=uuid4(), tenant_id=tenant.id, name="Limpeza", normalized_name="limpeza"
        )
        sede = Unit(id=uuid4(), tenant_id=tenant.id, name="Sede")
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro")
        session.add_all([service, sede, centro])
        await session.flush()
        session.add(
            ProfessionalServiceUnitTerms(
                tenant_id=tenant.id,
                professional_id=doc.id,
                service_id=service.id,
                unit_id=sede.id,
                price="R$ 300,00",
            )
        )
        await session.commit()
    entry = {"service_id": str(service.id), "price": "R$ 200,00", "duration_min": 30}
    return SimpleNamespace(
        tenant=tenant, doc=doc, service=service, sede=sede, centro=centro, entry=entry
    )


@pytest.mark.asyncio
async def test_resolve_entry_at_unit_without_unit_is_base_and_no_query():
    for pid, unit, entry in [
        (uuid4(), None, ENTRY),
        (None, U1, ENTRY),
        (uuid4(), U1, {"price": "R$ 1,00"}),
    ]:
        got = await st.resolve_entry_at_unit(_NoQuery(), uuid4(), pid, entry, unit)
        assert got == st.resolve_terms(entry, None, None)


@pytest.mark.asyncio
async def test_resolve_entry_at_unit_exception_wins_and_inherits(db):
    w = await _world(db)
    async with db() as s:
        at_sede = await st.resolve_entry_at_unit(s, w.tenant.id, w.doc.id, w.entry, w.sede.id)
        at_centro = await st.resolve_entry_at_unit(s, w.tenant.id, w.doc.id, w.entry, w.centro.id)
    assert (at_sede.price, at_sede.duration_min, at_sede.own) == ("R$ 300,00", 30, True)
    assert (at_centro.price, at_centro.duration_min, at_centro.own) == ("R$ 200,00", 30, False)


@pytest.mark.asyncio
async def test_resolve_entry_at_unit_other_tenant_is_base(db):
    w = await _world(db)
    async with db() as s:
        got = await st.resolve_entry_at_unit(s, uuid4(), w.doc.id, w.entry, w.sede.id)
    assert got.own is False and got.price == "R$ 200,00"


# ----------------------------------------------- attach_unit_options / view


@pytest.mark.asyncio
async def test_attach_unit_options_carries_terms_in_one_query(db):
    w = await _world(db)
    other = await seed_professional(db, w.tenant, "Dra. Bia")
    snaps = [
        SimpleNamespace(id=w.doc.id, business_hours=None),
        SimpleNamespace(id=other.id, business_hours=None),
    ]
    async with db() as session:
        statements: list[str] = []

        def _rec(conn, cursor, statement, *_):
            statements.append(statement)

        engine = session.bind.sync_engine
        event.listen(engine, "before_cursor_execute", _rec)
        try:
            await attach_unit_options(session, w.tenant, [w.doc, other], snaps)
        finally:
            event.remove(engine, "before_cursor_execute", _rec)
    assert len([s for s in statements if "professional_service_unit_terms" in s]) == 1
    assert snaps[0].service_unit_terms == {w.service.id: {w.sede.id: UnitTerms("R$ 300,00", None)}}
    assert snaps[1].service_unit_terms == {}


@pytest.mark.asyncio
async def test_attach_never_attaches_another_tenants_rows(db):
    w = await _world(db)
    stranger = await seed_tenant(db, name="Outra")
    snap = SimpleNamespace(id=w.doc.id, business_hours=None)
    async with db() as session:
        await attach_unit_options(session, stranger, [w.doc], [snap])
    assert snap.service_unit_terms == {}


@pytest.mark.asyncio
async def test_professional_terms_view(db):
    w = await _world(db)
    async with db() as session:
        session.add(ProfessionalUnit(professional_id=w.doc.id, unit_id=w.sede.id))
        session.add(ProfessionalUnit(professional_id=w.doc.id, unit_id=w.centro.id))
        await session.commit()
    async with db() as session:
        view = await professional_terms_view(session, w.tenant, w.doc)
    assert view.id == w.doc.id and view.name == "Dra. Ana"
    assert {u.id for u in view.units} == {w.sede.id, w.centro.id}
    assert view.service_units == {}
    assert view.service_unit_terms[w.service.id][w.sede.id].price == "R$ 300,00"


# ------------------------------------------------------------------ terms_at


def test_terms_at():
    doc = _doc([_unit()], {SID: {U1: UnitTerms("R$ 250,00", 45)}})
    assert fu.terms_at(doc, ENTRY, U1).price == "R$ 250,00"
    assert fu.terms_at(doc, ENTRY, U2).price == "R$ 200,00"
    assert fu.terms_at(doc, ENTRY, None).own is False
    assert fu.service_unit_terms_of(None) == {}


# --------------------------------------------------------------- shown_terms


def test_rule1_unit_given():
    a, b = _unit("A"), _unit("B")
    doc = _doc([a, b], {SID: {a.id: UnitTerms("R$ 250,00", 45)}})
    assert fu.shown_terms(doc, ENTRY, a.id) == fu.ShownTerms("R$ 250,00", 45, False, a.id)
    assert fu.shown_terms(doc, ENTRY, b.id) == fu.ShownTerms("R$ 200,00", 30, False, b.id)


def test_rule2_single_unit_exception_counts():
    a = _unit("A")
    doc = _doc([a], {SID: {a.id: UnitTerms("R$ 250,00", 45)}})
    assert fu.shown_terms(doc, ENTRY) == fu.ShownTerms("R$ 250,00", 45, False, a.id)


def test_rule3_no_units_is_base():
    assert fu.shown_terms(_doc([]), ENTRY) == fu.ShownTerms("R$ 200,00", 30, False, None)
    assert fu.shown_terms(None, ENTRY) == fu.ShownTerms("R$ 200,00", 30, False, None)


def test_rule4_price_differs_hides_price_keeps_base_duration():
    a, b = _unit("A"), _unit("B")
    doc = _doc([a, b], {SID: {a.id: UnitTerms("R$ 250,00", 45)}})
    assert fu.shown_terms(doc, ENTRY) == fu.ShownTerms(None, 30, True, None)


def test_rule5_same_price_everywhere_base_duration():
    a, b = _unit("A"), _unit("B")
    doc = _doc([a, b], {SID: {a.id: UnitTerms("R$ 200,00", 45)}})
    assert fu.shown_terms(doc, ENTRY) == fu.ShownTerms("R$ 200,00", 30, False, None)
    same = _doc([a, b], {SID: {a.id: UnitTerms("250", None), b.id: UnitTerms("R$ 250,00", None)}})
    got = fu.shown_terms(same, ENTRY)
    assert got.price == "250" and not got.price_hidden


def test_restricted_service_at_one_of_two_units_is_rule2():
    a, b = _unit("A"), _unit("B")
    doc = _doc([a, b], {SID: {b.id: UnitTerms("R$ 300,00", None)}}, marks={SID: frozenset({b.id})})
    assert fu.shown_terms(doc, ENTRY) == fu.ShownTerms("R$ 300,00", 30, False, b.id)


def test_snapshot_without_field_and_entry_without_service_id_are_base():
    a, b = _unit("A"), _unit("B")
    assert fu.shown_terms(_doc([a, b]), ENTRY) == fu.ShownTerms("R$ 200,00", 30, False, None)
    doc = _doc([a, b], {SID: {a.id: UnitTerms("R$ 250,00", None)}})
    no_sid = {"price": "R$ 200,00", "duration_min": 30}
    assert fu.shown_terms(doc, no_sid) == fu.ShownTerms("R$ 200,00", 30, False, None)
