# ruff: noqa: F811
"""TASK-087 T3: the ONE place the service's blocked weekdays apply (SPEC 5.2, R1/R5/R6).

`CalendarService.with_blocked_weekdays` makes `_windows_for_day` answer [] on a blocked weekday -
BEFORE the 08-18 fallback of empty hours (R6) - so the day list, the slot list and `is_slot_free`
all drop the day at once. The helpers around it (unit_booking / flow_units / service_terms) pick
the blocked set for a (professional, service, unit) and build the patient's refusal sentence.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.models import Professional, ProfessionalServiceUnitTerms, Service, Unit
from secretaria.services import flow_router, flow_units, service_terms, unit_booking
from secretaria.services.local_calendar import LocalCalendarService
from secretaria.services.service_terms import UnitTerms
from secretaria.services.unit_booking import UnitOption
from tests._local_agenda import (
    agenda,
    seed_tenant,
    wire_local_agenda as _wire_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401

MORNINGS = [{"start": "08:00", "end": "12:00"}]


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):
    _wire_agenda(monkeypatch, db)


def _next(weekday: int) -> datetime:
    """Clinic-local midnight of the next `weekday` (0 = Monday) at least 2 days ahead, in UTC."""
    first = (datetime.now(UTC) - timedelta(hours=3)).date() + timedelta(days=2)
    day = first + timedelta(days=(weekday - first.weekday()) % 7)
    return datetime(day.year, day.month, day.day, 3, 0, tzinfo=UTC)  # 00:00 in São Paulo


def _local(cal, day: datetime) -> datetime:
    return day.astimezone(cal.tzinfo)


async def _calendar(db, hours: dict):
    tenant = await seed_tenant(db)
    return agenda(tenant.id).with_unit(None, None, business_hours=hours)


# --------------------------------------------------------------------------- the calendar filter


async def test_blocked_day_has_no_slots(db):
    cal = await _calendar(db, {"monday": MORNINGS, "tuesday": MORNINGS})
    blocked = cal.with_blocked_weekdays({"monday"})

    assert await blocked.list_free_slots(day=_local(cal, _next(0))) == []
    assert await blocked.list_free_slots(day=_local(cal, _next(1))) != []
    # The original agenda is untouched (a copy, like with_unit).
    assert await cal.list_free_slots(day=_local(cal, _next(0))) != []


async def test_blocked_day_with_empty_hours_fallback(db):
    """Review Focus 2 / R6: `{}` hours fall back to 08-18 every day - the blocked day stays shut."""
    cal = await _calendar(db, {})
    blocked = cal.with_blocked_weekdays({"monday"})

    assert await blocked.list_free_slots(day=_local(cal, _next(0))) == []
    tuesday = await blocked.list_free_slots(day=_local(cal, _next(1)), max_slots=40)
    assert tuesday[0]["label"] == "08:00"
    assert tuesday[-1]["label"] == "17:30"


async def test_is_slot_free_false_on_blocked_day(db):
    cal = await _calendar(db, {"monday": MORNINGS, "tuesday": MORNINGS})
    blocked = cal.with_blocked_weekdays(["monday"])
    # On the clinic's clock, like every caller (`is_slot_free` reads the weekday/hour as given).
    monday_9 = _local(cal, _next(0)) + timedelta(hours=9)
    tuesday_9 = _local(cal, _next(1)) + timedelta(hours=9)

    assert not await blocked.is_slot_free(monday_9, monday_9 + timedelta(minutes=30))
    assert await blocked.is_slot_free(tuesday_9, tuesday_9 + timedelta(minutes=30))
    assert await cal.is_slot_free(monday_9, monday_9 + timedelta(minutes=30))


async def test_list_available_days_skips_blocked(db):
    cal = await _calendar(db, {"monday": MORNINGS, "tuesday": MORNINGS, "friday": MORNINGS})
    blocked = cal.with_blocked_weekdays({"monday", "friday"})
    start = _local(cal, _next(0))

    days = await blocked.list_available_days(start, 7)

    assert [day.weekday() for day in days] == [1]
    assert sorted(d.weekday() for d in await cal.list_available_days(start, 7)) == [0, 1, 4]


async def test_empty_block_changes_nothing(db):
    cal = await _calendar(db, {"monday": MORNINGS})
    same = cal.with_blocked_weekdays(())
    assert await same.list_free_slots(day=_local(cal, _next(0))) != []


async def test_mirroring_keeps_blocked(db):
    cal = await _calendar(db, {"monday": MORNINGS, "tuesday": MORNINGS})
    mirror = LocalCalendarService.mirroring(cal.with_blocked_weekdays({"tuesday"}))

    assert await mirror.list_free_slots(day=_local(cal, _next(1))) == []
    assert await mirror.list_free_slots(day=_local(cal, _next(0))) != []


async def test_mirroring_of_an_unfiltered_agenda_blocks_nothing(db):
    cal = await _calendar(db, {"monday": MORNINGS})
    mirror = LocalCalendarService.mirroring(cal)
    assert await mirror.list_free_slots(day=_local(cal, _next(0))) != []


# --------------------------------------------------------------------------- unit_booking helpers


def test_service_calendar_noop_without_method():
    double = SimpleNamespace(with_unit=lambda *a, **k: None)  # the 9 doubles: no new method
    assert unit_booking.service_calendar(double, {"monday"}) is double
    assert unit_booking.service_calendar(None, {"monday"}) is None


async def test_service_calendar_without_block_returns_same_object(db):
    cal = await _calendar(db, {"monday": MORNINGS})
    assert unit_booking.service_calendar(cal, frozenset()) is cal
    assert unit_booking.service_calendar(cal, None) is cal


async def test_service_calendar_applies_the_block(db):
    cal = await _calendar(db, {"monday": MORNINGS})
    scoped = unit_booking.service_calendar(cal, {"monday"})
    assert scoped is not cal
    assert await scoped.list_free_slots(day=_local(cal, _next(0))) == []


def test_blocked_on():
    monday = _next(0).astimezone(LocalCalendarService(uuid4()).tzinfo)
    assert unit_booking.blocked_on(monday, {"monday"})
    assert unit_booking.blocked_on(monday.date(), {"monday"})
    assert not unit_booking.blocked_on(monday, {"tuesday"})
    assert not unit_booking.blocked_on(monday, ())


def test_weekday_plural_pt():
    assert unit_booking.weekday_plural_pt({"monday"}) == "segundas"
    assert unit_booking.weekday_plural_pt({"friday", "monday"}) == "segundas e sextas"
    assert (
        unit_booking.weekday_plural_pt(["sunday", "wednesday", "tuesday"])
        == "terças, quartas e domingos"
    )
    assert unit_booking.weekday_plural_pt({"saturday", "thursday"}) == "quintas e sábados"
    assert unit_booking.weekday_plural_pt({"funday"}) == ""


def test_weekday_list_pt_gives_each_gender_its_article():
    # TASK-087 review P6: "às" for the feminine weekdays, "aos" for Saturday/Sunday; a mixed list
    # gives every day its own article ("às sextas e aos sábados", never "às sextas e sábados").
    assert unit_booking.weekday_list_pt({"monday"}) == "às segundas"
    assert unit_booking.weekday_list_pt({"friday", "monday"}) == "às segundas e sextas"
    assert unit_booking.weekday_list_pt({"sunday", "saturday"}) == "aos sábados e domingos"
    assert unit_booking.weekday_list_pt({"saturday", "friday"}) == "às sextas e aos sábados"
    assert (
        unit_booking.weekday_list_pt({"saturday", "friday", "monday"})
        == "às segundas, às sextas e aos sábados"
    )
    assert unit_booking.weekday_list_pt({"funday"}) == ""
    assert unit_booking.weekday_list_pt(None) == ""


def test_refusal_text():
    # Review P6: "não está disponível" agrees with any service name ("Consulta", "Retorno").
    assert (
        unit_booking.service_day_refusal("Cirurgia", "Dra. Ana", {"monday", "friday"})
        == "Cirurgia com Dra. Ana não está disponível às segundas e sextas. Posso ver outro dia?"
    )
    assert (
        unit_booking.service_day_refusal("Cirurgia", "Dra. Ana", {"saturday"})
        == "Cirurgia com Dra. Ana não está disponível aos sábados. Posso ver outro dia?"
    )
    assert (
        unit_booking.service_day_refusal("Cirurgia", None, {"monday"})
        == "Cirurgia não está disponível às segundas. Posso ver outro dia?"
    )
    assert (
        unit_booking.service_day_refusal(" Cirurgia ", "  ", {"monday"})
        == "Cirurgia não está disponível às segundas. Posso ver outro dia?"
    )


def test_refusal_mixed_list_gives_each_day_its_article():
    assert (
        unit_booking.service_day_refusal("Exame", "Dr. Leo", {"sunday", "saturday"})
        == "Exame com Dr. Leo não está disponível aos sábados e domingos. Posso ver outro dia?"
    )
    assert (
        unit_booking.service_day_refusal("Exame", "Dr. Leo", {"sunday", "friday"})
        == "Exame com Dr. Leo não está disponível às sextas e aos domingos. Posso ver outro dia?"
    )


def test_refusal_without_known_days_still_reads_well():
    assert (
        unit_booking.service_day_refusal("Exame", "Dr. Leo", ())
        == "Exame com Dr. Leo não está disponível nesse dia. Posso ver outro dia?"
    )


# --------------------------------------------------------------------------- flow_units (snapshot)

SERVICE = uuid4()
U1 = UnitOption(id=uuid4(), name="Centro", address=None, business_hours={"monday": MORNINGS})
U2 = UnitOption(id=uuid4(), name="Sul", address=None, business_hours={"monday": MORNINGS})


def _doctor(*, units=(U1, U2), service_units=None, terms=None):
    return SimpleNamespace(
        id=uuid4(),
        name="Dra. Ana",
        units=list(units),
        service_units=service_units or {},
        service_unit_terms=terms or {},
    )


def _entry(blocked=None, service_id=SERVICE):
    entry = {"service_id": str(service_id), "name": "Cirurgia"}
    if blocked is not None:
        entry["blocked_weekdays"] = blocked
    return entry


def test_blocked_weekdays_of_unit_none_is_intersection():
    """Review Focus 1 / R1: no unit chosen = a day stays when ANY unit of the service allows it."""
    doctor = _doctor(terms={SERVICE: {U1.id: UnitTerms(None, None, frozenset({"monday"}))}})
    entry = _entry(["monday", "friday"])

    assert flow_units.blocked_weekdays_of(doctor, entry, None) == frozenset({"monday"})
    assert flow_units.blocked_weekdays_of(doctor, entry, U1.id) == frozenset({"monday"})
    assert flow_units.blocked_weekdays_of(doctor, entry, U2.id) == frozenset({"monday", "friday"})


def test_blocked_weekdays_of_unit_none_only_counts_units_of_the_service():
    """U2 does not do the service: its open Friday does not reopen Friday."""
    doctor = _doctor(
        service_units={SERVICE: frozenset({U1.id})},
        terms={SERVICE: {U2.id: UnitTerms(None, None, frozenset())}},
    )
    entry = _entry(["friday"])

    assert flow_units.blocked_weekdays_of(doctor, entry, None) == frozenset({"friday"})


def _open_on(*days: str, name: str = "Unidade") -> UnitOption:
    return UnitOption(
        id=uuid4(), name=name, address=None, business_hours={day: MORNINGS for day in days}
    )


MON_FRI = ("monday", "tuesday", "wednesday", "thursday", "friday")


def test_blocked_weekdays_of_unit_none_blocks_a_day_no_open_unit_allows():
    """Review P2 / R1: Centro opens Mon-Fri and blocks Monday (its own list); Norte opens Tue-Fri
    and inherits the empty base. The plain intersection of the lists is empty, but no unit that
    OPENS on Monday does the service then - Monday is blocked."""
    centro, norte = _open_on(*MON_FRI, name="Centro"), _open_on(*MON_FRI[1:], name="Norte")
    doctor = _doctor(
        units=(centro, norte),
        terms={SERVICE: {centro.id: UnitTerms(None, None, frozenset({"monday"}))}},
    )

    assert flow_units.blocked_weekdays_of(doctor, _entry(), None) == frozenset({"monday"})
    assert flow_units.blocked_weekdays_of(doctor, _entry(), norte.id) == frozenset()


def test_blocked_weekdays_of_unit_none_keeps_a_day_one_open_unit_allows():
    """R1 still holds: Norte opening on Monday (inheriting the empty base) keeps Monday."""
    centro, norte = _open_on(*MON_FRI, name="Centro"), _open_on(*MON_FRI, name="Norte")
    doctor = _doctor(
        units=(centro, norte),
        terms={SERVICE: {centro.id: UnitTerms(None, None, frozenset({"monday"}))}},
    )

    assert flow_units.blocked_weekdays_of(doctor, _entry(), None) == frozenset()


def test_blocked_weekdays_of_without_units_is_the_base():
    doctor = _doctor(units=())
    assert flow_units.blocked_weekdays_of(doctor, _entry(["monday"]), None) == {"monday"}


def test_blocked_weekdays_of_unknown_service_blocks_nothing():
    """R5: no service fixed yet = no filter."""
    assert flow_units.blocked_weekdays_of(_doctor(), None, None) == frozenset()
    assert flow_units.blocked_weekdays_of(_doctor(), None, U1.id) == frozenset()


def test_blocked_weekdays_of_tolerates_an_old_snapshot():
    old = SimpleNamespace(id=uuid4(), name="Dr. Velho")  # predates units/terms
    assert flow_units.blocked_weekdays_of(old, _entry(["sunday"]), None) == {"sunday"}
    assert flow_units.blocked_weekdays_of(None, _entry(["sunday"]), None) == {"sunday"}


# The one place a patient's agenda gets the unit's hours AND the service's days (review P7: the
# unused `flow_units.service_scoped_calendar`, which could only ADD days, is gone).


async def test_service_scoped_uses_unit_hours_and_unit_block(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    centro = UnitOption(
        id=uuid4(),
        name="Centro",
        address=None,
        business_hours={"monday": MORNINGS, "tuesday": MORNINGS},
    )
    doctor = _doctor(
        units=(centro,),
        terms={SERVICE: {centro.id: UnitTerms(None, None, frozenset({"tuesday"}))}},
    )

    scoped = flow_router._service_scoped(cal, doctor, _entry(["monday"]), centro)

    assert scoped.unit_id == centro.id
    assert await scoped.list_free_slots(day=_local(cal, _next(1))) == []  # unit's own block
    assert await scoped.list_free_slots(day=_local(cal, _next(0))) != []  # base overridden
    assert await scoped.list_free_slots(day=_local(cal, _next(2))) == []  # unit closed Wed


async def test_service_scoped_without_unit_uses_base(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    doctor = _doctor(units=())

    scoped = flow_router._service_scoped(cal, doctor, _entry(["monday"]), None)

    assert await scoped.list_free_slots(day=_local(cal, _next(0))) == []
    assert await scoped.list_free_slots(day=_local(cal, _next(1))) != []


async def test_service_scoped_replaces_a_stale_block(db):
    """An agenda scoped earlier for another service (Monday blocked) and re-scoped for a service
    without blocks offers Monday again: the set is replaced, never added to."""
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id).with_blocked_weekdays({"monday"})
    doctor = _doctor(units=())

    scoped = flow_router._service_scoped(cal, doctor, _entry(), None)

    assert await scoped.list_free_slots(day=_local(cal, _next(0))) != []


def test_service_scoped_without_block_is_scoped_calendar():
    double = SimpleNamespace()
    assert flow_router._service_scoped(double, _doctor(), _entry(), None) is double


def test_flow_units_has_no_additive_service_scope():
    assert not hasattr(flow_units, "service_scoped_calendar")


# --------------------------------------------------------------------------- service_terms (DB)


async def _world(db):
    tenant = await seed_tenant(db)
    async with db() as session:
        service = Service(
            id=uuid4(), tenant_id=tenant.id, name="Cirurgia", normalized_name="cirurgia"
        )
        doctor = Professional(id=uuid4(), tenant_id=tenant.id, name="Dra. Ana", is_active=True)
        unit = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro")
        session.add_all([service, doctor, unit])
        await session.flush()
        session.add(
            ProfessionalServiceUnitTerms(
                tenant_id=tenant.id,
                professional_id=doctor.id,
                service_id=service.id,
                unit_id=unit.id,
                blocked_weekdays=["friday"],
            )
        )
        await session.commit()
    return SimpleNamespace(tenant=tenant, service=service, doctor=doctor, unit=unit)


async def test_blocked_weekdays_at_reads_the_unit_row(db):
    w = await _world(db)
    entry = {"service_id": str(w.service.id), "blocked_weekdays": ["monday"]}
    async with db() as session:
        at_unit = await service_terms.blocked_weekdays_at(
            session, w.tenant.id, w.doctor.id, entry, w.unit.id
        )
        no_unit = await service_terms.blocked_weekdays_at(
            session, w.tenant.id, w.doctor.id, entry, None
        )
        other_unit = await service_terms.blocked_weekdays_at(
            session, w.tenant.id, w.doctor.id, entry, uuid4()
        )

    assert at_unit == frozenset({"friday"})
    assert no_unit == frozenset({"monday"})
    assert other_unit == frozenset({"monday"})


async def test_blocked_weekdays_at_is_tenant_scoped(db):
    w = await _world(db)
    stranger = await seed_tenant(db, name="Outra")
    entry = {"service_id": str(w.service.id), "blocked_weekdays": ["monday"]}
    async with db() as session:
        got = await service_terms.blocked_weekdays_at(
            session, stranger.id, w.doctor.id, entry, w.unit.id
        )
    assert got == frozenset({"monday"})


async def test_blocked_weekdays_at_without_entry_or_professional(db):
    w = await _world(db)
    async with db() as session:
        assert await service_terms.blocked_weekdays_at(
            session, w.tenant.id, w.doctor.id, None, w.unit.id
        ) == (frozenset())
        assert await service_terms.blocked_weekdays_at(
            session, w.tenant.id, None, {"blocked_weekdays": ["sunday"]}, w.unit.id
        ) == frozenset({"sunday"})
