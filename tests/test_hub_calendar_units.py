# ruff: noqa: F811
"""The hub agenda names the unit, and the staff may book at one (TASK-083 frozen contract).

`GET /tenants/me/calendar/events` items carry `unit_id` / `unit_name` / `unit_address` (null
without a unit; only THIS clinic's units resolve). `POST /appointments` accepts an optional
`unit_id`: a unit of this clinic (404), active and - when the doctor attends at units - one of
theirs (422), each with a plain message; absent is always accepted (staff "encaixe": no unit).
Real routes, real secretarIA agenda (tests/test_hub_calendar_local.py's wiring).
"""

from datetime import timedelta
from uuid import UUID, uuid4

from httpx import AsyncClient

from secretaria.models import Unit
from secretaria.models.professional_unit import ProfessionalUnit
from tests._local_agenda import seed_professional, seed_tenant, tomorrow_at
from tests._reminder_fixtures import db  # noqa: F401
from tests.test_hub_calendar_local import (  # noqa: F401 - fixtures
    CALENDAR,
    _appointment,
    _clinic,
    _create,
    _events,
    _wire,
    acting,
)


async def _unit(db, tenant, name="Centro", address="Rua A, 10", *, active=True) -> Unit:
    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=tenant.id, name=name, address=address, is_active=active)
        session.add(unit)
        await session.commit()
        return unit


async def _link(db, professional, unit) -> None:
    async with db() as session:
        session.add(ProfessionalUnit(professional_id=professional.id, unit_id=unit.id))
        await session.commit()


async def _list(client, start):
    resp = await client.get(
        f"{CALENDAR}/events",
        params={
            "start": (start - timedelta(days=1)).isoformat(),
            "end": (start + timedelta(days=1)).isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_a_consultation_booked_at_a_unit_is_listed_with_its_unit(
    client: AsyncClient, db, acting
):
    tenant = await _clinic(db, acting)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    centro = await _unit(db, tenant)
    await _link(db, doctor, centro)
    start = tomorrow_at(10)

    resp = await _create(client, start, professional_id=str(doctor.id), unit_id=str(centro.id))
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert (await _appointment(db, UUID(created["id"]))).unit_id == centro.id
    [event_row] = await _events(db, tenant)
    assert event_row.unit_id == centro.id

    [item] = await _list(client, start)
    assert (item["unit_id"], item["unit_name"], item["unit_address"]) == (
        str(centro.id),
        "Centro",
        "Rua A, 10",
    )


async def test_without_a_unit_the_items_carry_null_unit_fields(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    await _link(db, doctor, await _unit(db, tenant))  # a multi-unit doctor all the same
    start = tomorrow_at(11)

    # Absent unit_id: always accepted (the staff's encaixe), no unit recorded.
    resp = await _create(client, start, professional_id=str(doctor.id))
    assert resp.status_code == 201, resp.text
    assert (await _appointment(db, UUID(resp.json()["id"]))).unit_id is None
    [item] = await _list(client, start)
    assert (item["unit_id"], item["unit_name"], item["unit_address"]) == (None, None, None)


async def test_an_invalid_unit_is_refused_before_anything_is_written(
    client: AsyncClient, db, acting
):
    tenant = await _clinic(db, acting)
    other = await seed_tenant(db, name="Outra")
    doctor = await seed_professional(db, tenant, "Dra. Ana")
    centro = await _unit(db, tenant)
    sul = await _unit(db, tenant, "Zona Sul")
    closed = await _unit(db, tenant, "Fechada", active=False)
    foreign = await _unit(db, other, "Alheia")
    await _link(db, doctor, centro)
    start = tomorrow_at(12)

    for unit_id, status, code in (
        (foreign.id, 404, "unit_not_found"),
        (uuid4(), 404, "unit_not_found"),
        (closed.id, 422, "unit_inactive"),
        (sul.id, 422, "unit_not_linked"),
    ):
        resp = await _create(client, start, professional_id=str(doctor.id), unit_id=str(unit_id))
        assert resp.status_code == status, (code, resp.text)
        assert resp.json()["detail"]["code"] == code
        assert resp.json()["detail"]["message"]
    assert await _events(db, tenant) == []


async def test_a_doctor_without_units_and_a_doctorless_booking_take_any_active_unit(
    client: AsyncClient, db, acting
):
    tenant = await _clinic(db, acting)
    doctor = await seed_professional(db, tenant, "Dr. Livre")
    sul = await _unit(db, tenant, "Zona Sul", None)
    start = tomorrow_at(13)
    with_doctor = await _create(client, start, professional_id=str(doctor.id), unit_id=str(sul.id))
    clinic = await _create(client, start + timedelta(hours=1), unit_id=str(sul.id))
    assert (with_doctor.status_code, clinic.status_code) == (201, 201)
    items = await _list(client, start)
    assert {(i["unit_name"], i["unit_address"]) for i in items} == {("Zona Sul", None)}
