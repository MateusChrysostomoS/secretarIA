# ruff: noqa: F811
"""Service-aware unit choice (TASK-083 follow-up T3): the pure decision and the roster snapshot.

`unit_choice(professional, service_id)` answers free | none | implied | confirm | list from the
snapshot alone (no I/O). "Bookable" is the existing notion (linked, active, with hours); a marked
unit that is closed never counts, and an all-closed restriction is `none`, never "all".
"""

import os

os.environ.setdefault("APP_ENV", "test")

from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import event  # noqa: E402

from secretaria.models import ProfessionalServiceUnit, ProfessionalUnit, Service, Unit  # noqa: E402
from secretaria.services import flow_units as fu  # noqa: E402
from secretaria.services.unit_booking import UnitOption, attach_unit_options  # noqa: E402
from tests._local_agenda import seed_professional, seed_tenant  # noqa: E402
from tests._reminder_fixtures import db  # noqa: E402,F401

OPEN = {"monday": [{"start": "08:00", "end": "12:00"}]}


def _unit(name: str, hours=OPEN) -> UnitOption:
    return UnitOption(id=uuid4(), name=name, address=f"Rua {name}", business_hours=hours)


def _doctor(units: list[UnitOption], marks=None):
    return SimpleNamespace(id=uuid4(), name="Dra. Ana", units=units, service_units=marks or {})


def _conversation(unit_id=None):
    return SimpleNamespace(flow_selected_unit_id=unit_id)


def test_free_when_no_links():
    doctor = _doctor([])
    choice = fu.unit_choice(doctor, uuid4())
    assert choice.kind == "free"
    assert choice.units == []
    assert fu.unit_choice(None, None).kind == "free"


def test_list_when_two_allowed():
    a, b, c = _unit("A"), _unit("B"), _unit("C")
    service = uuid4()
    doctor = _doctor([a, b, c], {service: frozenset({a.id, c.id})})
    choice = fu.unit_choice(doctor, service)
    assert choice.kind == "list"
    assert [u.id for u in choice.units] == [a.id, c.id]


def test_confirm_when_filter_leaves_one_of_two():
    a, b = _unit("A"), _unit("B")
    service = uuid4()
    doctor = _doctor([a, b], {service: frozenset({b.id})})
    choice = fu.unit_choice(doctor, service)
    assert choice.kind == "confirm"
    assert [u.id for u in choice.units] == [b.id]


def test_implied_when_doctor_has_single_unit():
    a = _unit("A")
    service = uuid4()
    for marks in ({}, {service: frozenset({a.id})}):
        choice = fu.unit_choice(_doctor([a], marks), service)
        assert choice.kind == "implied"
        assert [u.id for u in choice.units] == [a.id]


def test_none_when_restricted_units_closed():
    open_unit, closed = _unit("A"), _unit("B", hours={})
    service = uuid4()
    doctor = _doctor([open_unit, closed], {service: frozenset({closed.id})})
    choice = fu.unit_choice(doctor, service)
    assert choice.kind == "none"
    assert choice.units == []
    # Never a fallback to "all": the open unit is not offered for this service.
    assert fu.units_for_service(doctor, service) == []
    # Another service of the same doctor is unaffected.
    assert fu.unit_choice(doctor, uuid4()).kind == "implied"


def test_confirm_counts_only_bookable_units():
    # Two links but one closed: the doctor really has a single bookable unit -> implied.
    open_unit, closed = _unit("A"), _unit("B", hours={})
    service = uuid4()
    doctor = _doctor([open_unit, closed], {service: frozenset({open_unit.id})})
    assert fu.unit_choice(doctor, service).kind == "implied"


def test_service_without_marks_allows_all():
    a, b = _unit("A"), _unit("B")
    doctor = _doctor([a, b], {uuid4(): frozenset({a.id})})
    choice = fu.unit_choice(doctor, uuid4())
    assert choice.kind == "list"
    assert [u.id for u in choice.units] == [a.id, b.id]


def test_service_id_none_equals_today():
    a, b = _unit("A"), _unit("B")
    service = uuid4()
    doctor = _doctor([a, b], {service: frozenset({a.id})})
    assert [u.id for u in fu.units_for_service(doctor, None)] == [a.id, b.id]
    assert fu.unit_choice(doctor, None).kind == "list"
    assert fu.unit_choice_needed(doctor) is True
    assert fu.unit_choice_needed(doctor, service) is False
    assert fu.effective_unit(_conversation(), doctor) is None
    assert fu.effective_unit(_conversation(), doctor, service) is None  # confirm: not decided yet
    assert fu.effective_unit(_conversation(b.id), doctor).id == b.id


def test_effective_unit_ignores_pick_outside_filter():
    a, b = _unit("A"), _unit("B")
    service = uuid4()
    doctor = _doctor([a, b], {service: frozenset({a.id})})
    assert fu.effective_unit(_conversation(b.id), doctor, service) is None
    assert fu.effective_unit(_conversation(a.id), doctor, service).id == a.id
    assert fu.find_unit(doctor, b.id, service) is None
    assert fu.find_unit(doctor, b.id).id == b.id


def test_effective_unit_implied_returns_the_single_unit():
    a = _unit("A")
    service = uuid4()
    doctor = _doctor([a], {service: frozenset({a.id})})
    assert fu.effective_unit(_conversation(), doctor, service).id == a.id


def test_service_units_of_defaults_to_empty():
    assert fu.service_units_of(None) == {}
    assert fu.service_units_of(SimpleNamespace(units=[])) == {}
    marks = {uuid4(): frozenset({uuid4()})}
    assert fu.service_units_of(SimpleNamespace(service_units=marks)) == marks


@pytest.mark.asyncio
async def test_attach_unit_options_sets_service_units_one_query(db):
    tenant = await seed_tenant(db)
    ana = await seed_professional(db, tenant, "Dra. Ana")
    bia = await seed_professional(db, tenant, "Dra. Bia")
    async with db() as session:
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua A")
        sul = Unit(id=uuid4(), tenant_id=tenant.id, name="Sul", address="Rua B")
        limpeza = Service(
            id=uuid4(),
            tenant_id=tenant.id,
            name="Limpeza",
            normalized_name="limpeza",
            is_active=True,
        )
        session.add_all([centro, sul, limpeza])
        await session.flush()
        for doctor in (ana, bia):
            for unit in (centro, sul):
                session.add(ProfessionalUnit(professional_id=doctor.id, unit_id=unit.id))
        await session.flush()
        session.add(
            ProfessionalServiceUnit(professional_id=ana.id, service_id=limpeza.id, unit_id=sul.id)
        )
        await session.commit()

    snapshots = [
        SimpleNamespace(id=ana.id, business_hours=None),
        SimpleNamespace(id=bia.id, business_hours=None),
    ]
    async with db() as session:
        statements: list[str] = []

        def _record(conn, cursor, statement, *_):
            statements.append(statement)

        engine = session.bind.sync_engine
        event.listen(engine, "before_cursor_execute", _record)
        try:
            await attach_unit_options(session, tenant, [ana, bia], snapshots)
        finally:
            event.remove(engine, "before_cursor_execute", _record)

    mark_reads = [s for s in statements if "professional_service_units" in s]
    assert len(mark_reads) == 1
    assert snapshots[0].service_units == {limpeza.id: frozenset({sul.id})}
    assert snapshots[1].service_units == {}
    assert len(snapshots[0].units) == 2
    assert fu.unit_choice(snapshots[0], limpeza.id).kind == "confirm"
    assert fu.unit_choice(snapshots[1], limpeza.id).kind == "list"


# --- Final review (finding 1): the unit that follows a doctor change respects the service ----


def test_unit_after_doctor_change_skips_a_kept_unit_where_the_service_is_not_done():
    a, b = _unit("A"), _unit("B")
    service = uuid4()
    doctor = _doctor([a, b], {service: frozenset({b.id})})
    assert fu.unit_after_doctor_change(doctor, a.id, None, service) == b.id
    # Without the service (or an entry without service_id) it is today's rule: A is kept.
    assert fu.unit_after_doctor_change(doctor, a.id, None) == a.id
    assert fu.unit_after_doctor_change(doctor, a.id, None, None) == a.id


def test_unit_after_doctor_change_with_no_open_unit_for_the_service_is_none():
    a, closed = _unit("A"), _unit("Fechada", hours={})
    service = uuid4()
    doctor = _doctor([a, closed], {service: frozenset({closed.id})})
    assert fu.unit_after_doctor_change(doctor, None, None, service) is None


def test_unit_refused_only_for_a_restricted_service():
    a, b, closed = _unit("A"), _unit("B"), _unit("Fechada", hours={})
    service, other = uuid4(), uuid4()
    doctor = _doctor([a, b, closed], {service: frozenset({b.id}), other: frozenset({closed.id})})
    assert fu.unit_refused(doctor, a.id, service) is True
    assert fu.unit_refused(doctor, b.id, service) is False
    assert fu.unit_refused(doctor, None, service) is False  # B is open
    assert fu.unit_refused(doctor, None, other) is True  # "0 unidades"
    assert fu.unit_refused(doctor, a.id, uuid4()) is False  # no marks
    assert fu.unit_refused(doctor, a.id, None) is False  # entry without service_id
    assert fu.unit_refused(_doctor([]), None, service) is False  # no links
