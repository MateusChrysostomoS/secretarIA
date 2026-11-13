# ruff: noqa: F811
"""Units in a booking - the service half (TASK-083 spec §8, services/unit_booking.py).

Which units a professional offers a patient (one batched read), the hours there (NULL link =
the professional's hours, `{}` = closed), the agenda scoped to a unit (its hours restrict the
offered slots, its unit id lands on the local event, its location on the Google event) and the
check the hub and the AI tools run before writing a unit onto a booking.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import Unit
from secretaria.models.calendar_event import CalendarEvent
from secretaria.models.professional_unit import ProfessionalUnit
from secretaria.services import unit_booking
from secretaria.services.calendar import CalendarService
from tests._local_agenda import (
    agenda,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401 - fixture

MORNING = {
    day: [{"start": "08:00", "end": "12:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


@pytest.fixture(autouse=True)
def _local(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


async def _unit(db, tenant, name, address="Rua A, 10", *, active=True):
    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=tenant.id, name=name, address=address, is_active=active)
        session.add(unit)
        await session.commit()
        return unit


async def _link(db, professional, unit, *, hours=None, active=True, sort_order=0):
    async with db() as session:
        session.add(
            ProfessionalUnit(
                professional_id=professional.id,
                unit_id=unit.id,
                business_hours=hours,
                is_active=active,
                sort_order=sort_order,
            )
        )
        await session.commit()


@pytest.mark.asyncio
async def test_a_professional_without_links_offers_no_unit(db):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    await _unit(db, tenant, "Centro")
    async with db() as session:
        options = await unit_booking.unit_options_for(session, tenant, [doctor])
    assert options == {}


@pytest.mark.asyncio
async def test_the_offered_units_are_active_links_to_active_units_of_the_tenant(db):
    tenant = await seed_tenant(db)
    other = await seed_tenant(db, name="Outra")
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    centro = await _unit(db, tenant, "Centro", "Rua A, 10")
    sul = await _unit(db, tenant, "Zona Sul", "Av. B, 20")
    closed = await _unit(db, tenant, "Fechada", active=False)
    paused = await _unit(db, tenant, "Pausada")
    foreign = await _unit(db, other, "Alheia")
    await _link(db, doctor, sul, hours=MORNING, sort_order=0)
    await _link(db, doctor, centro, hours=None, sort_order=1)
    await _link(db, doctor, closed)
    await _link(db, doctor, paused, active=False)
    await _link(db, doctor, foreign)

    async with db() as session:
        options = (await unit_booking.unit_options_for(session, tenant, [doctor]))[doctor.id]

    assert [o.name for o in options] == ["Zona Sul", "Centro"]  # sort_order first
    assert options[0].address == "Av. B, 20"
    assert options[0].business_hours == MORNING
    # NULL link hours inherit the professional's resolved hours (here: the clinic's week).
    assert options[1].business_hours == tenant.business_hours
    assert all(o.bookable for o in options)


@pytest.mark.asyncio
async def test_an_empty_schedule_in_a_unit_is_closed_never_inherited(db):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    unit = await _unit(db, tenant, "Centro")
    await _link(db, doctor, unit, hours={})
    async with db() as session:
        [option] = (await unit_booking.unit_options_for(session, tenant, [doctor]))[doctor.id]
    assert option.business_hours == {}
    assert option.bookable is False


def test_the_location_names_the_unit_and_its_address():
    assert unit_booking.unit_location("Centro", "Rua A, 10") == "Centro — Rua A, 10"
    assert unit_booking.unit_location("Centro", "  ") == "Centro"
    assert unit_booking.unit_location("Centro", None) == "Centro"
    assert unit_booking.unit_line("Centro", "Rua A, 10") == (
        "Unidade: Centro\nEndereço: Rua A, 10\n"
    )
    assert unit_booking.unit_line("Centro", None) == "Unidade: Centro\n"


@pytest.mark.asyncio
async def test_the_units_hours_restrict_the_offered_slots_on_the_whole_agenda(db):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    calendar = agenda(tenant.id, doctor.id)
    day = tomorrow_at(0).astimezone(calendar.tzinfo).replace(hour=0)
    option = unit_booking.UnitOption(
        id=uuid4(), name="Centro", address="Rua A", business_hours=MORNING
    )

    scoped = unit_booking.scoped_calendar(calendar, option)
    slots = await scoped.list_free_slots(day=day, slot_minutes=60, max_slots=20)
    labels = [s["label"] for s in slots]
    assert labels[0] == "08:00" and labels[-1] == "11:00"
    # The agenda itself is untouched: still the clinic's 08-18.
    whole = await calendar.list_free_slots(day=day, slot_minutes=60, max_slots=20)
    assert whole[-1]["label"] == "17:00"


@pytest.mark.asyncio
async def test_a_unit_scoped_local_write_records_the_unit_and_a_plain_one_does_not(db):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    calendar = agenda(tenant.id, doctor.id)
    unit_id = uuid4()
    option = unit_booking.UnitOption(
        id=unit_id, name="Centro", address=None, business_hours=MORNING
    )
    start = tomorrow_at(9)

    event = await unit_booking.scoped_calendar(calendar, option).create_event(
        start, start + timedelta(minutes=30), "Consulta - Maria"
    )
    plain = await calendar.create_event(
        start + timedelta(hours=1), start + timedelta(hours=1, minutes=30), "Consulta - Rita"
    )
    async with db() as session:
        rows = {r.id: r for r in await session.scalars(select(CalendarEvent))}
    assert rows[event["id"]].unit_id == unit_id
    assert rows[plain["id"]].unit_id is None


@pytest.mark.asyncio
async def test_a_unit_scoped_google_write_carries_the_location_and_a_plain_one_none(monkeypatch):
    bodies: list[dict] = []

    class _Events:
        def insert(self, calendarId, body):  # noqa: N803
            bodies.append(body)
            return SimpleNamespace(execute=lambda: {"id": "g1", "htmlLink": None})

    calendar = CalendarService()
    monkeypatch.setattr(calendar, "_client", lambda: SimpleNamespace(events=lambda: _Events()))
    start = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)
    option = unit_booking.UnitOption(
        id=uuid4(), name="Centro", address="Rua A, 10", business_hours=MORNING
    )

    await unit_booking.scoped_calendar(calendar, option).create_event(
        start, start + timedelta(minutes=30), "Consulta"
    )
    await calendar.create_event(start, start + timedelta(minutes=30), "Consulta")

    assert bodies[0]["location"] == "Centro — Rua A, 10"
    assert "location" not in bodies[1]


def test_a_calendar_without_the_seam_is_returned_as_is():
    fake = SimpleNamespace(tzinfo=UTC)
    option = unit_booking.UnitOption(id=uuid4(), name="Centro", address=None, business_hours={})
    assert unit_booking.scoped_calendar(fake, option) is fake
    assert unit_booking.scoped_calendar(fake, None) is fake


def test_within_hours_is_a_containing_window_on_the_clinic_clock():
    tz = agenda(uuid4()).tzinfo
    monday_9 = datetime(2099, 1, 5, 9, 0, tzinfo=tz)  # 2099-01-05 is a Monday
    assert unit_booking.within_hours(MORNING, monday_9, monday_9 + timedelta(minutes=30))
    assert not unit_booking.within_hours(
        MORNING, monday_9.replace(hour=11, minute=45), monday_9.replace(hour=12, minute=15)
    )
    assert not unit_booking.within_hours({}, monday_9, monday_9 + timedelta(minutes=30))


@pytest.mark.asyncio
async def test_the_unit_check_refuses_other_tenants_inactive_and_unlinked_units(db):
    tenant = await seed_tenant(db)
    other = await seed_tenant(db, name="Outra")
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    free = await seed_professional(db, tenant, "Dr. Livre")
    centro = await _unit(db, tenant, "Centro")
    sul = await _unit(db, tenant, "Zona Sul")
    off = await _unit(db, tenant, "Fechada", active=False)
    foreign = await _unit(db, other, "Alheia")
    await _link(db, doctor, centro)

    async with db() as session:
        checked = await unit_booking.check_unit(session, tenant.id, doctor, centro.id)
        assert checked.id == centro.id
        # A professional without links may book at any active unit of the clinic.
        assert (await unit_booking.check_unit(session, tenant.id, free, sul.id)).id == sul.id
        assert (await unit_booking.check_unit(session, tenant.id, None, sul.id)).id == sul.id
        for unit_id, code in (
            (foreign.id, "unit_not_found"),
            (uuid4(), "unit_not_found"),
            (off.id, "unit_inactive"),
        ):
            with pytest.raises(unit_booking.UnitRefused) as refused:
                await unit_booking.check_unit(session, tenant.id, doctor, unit_id)
            assert refused.value.code == code
        with pytest.raises(unit_booking.UnitRefused) as refused:
            await unit_booking.check_unit(session, tenant.id, doctor, sul.id)
        assert refused.value.code == "unit_not_linked"
        assert refused.value.message  # a plain sentence for the clinic
