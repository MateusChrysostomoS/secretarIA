"""A doctor restricted to his own agenda cannot reach another doctor's appointment
(TASK-044 R7, spec 2026-10-09 §5.A)."""

# ruff: noqa: F811

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.models import Appointment, AppointmentStatus
from secretaria.services.agenda_visibility import AgendaViewer
from tests._agenda_viewer import view_as
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeArqPool,
    FakeCalendar,
    acting,
    add_professional,
    install_pool,
    r7,
    sent,
    set_tenant,
    setup_world,
    soon,
)
from tests._reminder_fixtures import db, other_tenant  # noqa: F401
from tests._reminders_v2 import outbound_messages, reload_appointment

SERVICES = [{"name": "Consulta", "is_active": True, "duration_min": 30}]


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _route(world, tail: str) -> tuple[str, dict | None]:
    later = world.start_at + timedelta(days=1)
    return {
        "cancel-preview": ("GET", None),
        "cancel": ("POST", {"confirm": True}),
        "release": ("POST", {}),
        "message": ("POST", {"text": "Oi"}),
        "reschedule": (
            "POST",
            {"new_start": _iso(later), "new_end": _iso(later + timedelta(minutes=30))},
        ),
        "status": ("PATCH", {"status": "confirmed"}),
        "edit": ("POST", {"attendee_name": "Outro nome"}),
    }[tail]


ID_ROUTES = ["cancel-preview", "cancel", "release", "message", "reschedule", "status", "edit"]


async def _call(client: AsyncClient, world, appointment_id, tail: str):
    method, body = _route(world, tail)
    url = f"{CALENDAR}/appointments/{appointment_id}/{tail}"
    return await client.request(method, url, json=body)


async def _anas_world(db, acting):
    world = await setup_world(db, acting, professional_name="Dra. Ana", google_event_id="evt-1")
    await set_tenant(db, world.tenant.id, appointment_types=SERVICES)
    return world


@pytest.mark.parametrize("tail", ID_ROUTES)
async def test_another_doctors_appointment_does_not_exist_for_a_restricted_doctor(
    client: AsyncClient, db, acting, r7, tail
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", beto))

    response = await _call(client, world, world.appointment.id, tail)

    assert response.status_code == 404
    assert response.json() == {"detail": "Appointment not found"}
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED and row.attendee_name is None
    assert sent() == [] and pool.calls == [] and r7["mail"] == []
    assert await outbound_messages(db, world.conversation.id) == []
    assert FakeCalendar.instances == {}  # Google never touched


@pytest.mark.parametrize("tail", ["cancel-preview", "status"])
async def test_the_404_is_byte_identical_to_an_id_that_does_not_exist(
    client: AsyncClient, db, acting, tail
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", beto))

    hidden = await _call(client, world, world.appointment.id, tail)
    missing = await _call(client, world, uuid4(), tail)

    assert (hidden.status_code, hidden.content) == (missing.status_code, missing.content)


async def test_an_appointment_without_a_doctor_is_out_of_a_restricted_view(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting)  # professional_id is None
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", beto))

    assert (await _call(client, world, world.appointment.id, "status")).status_code == 404


async def test_a_restricted_doctor_without_a_professional_reaches_nothing(
    client: AsyncClient, db, acting
):
    world = await _anas_world(db, acting)
    view_as(AgendaViewer("own", None))

    assert (await _call(client, world, world.appointment.id, "cancel-preview")).status_code == 404


async def test_his_own_appointment_works_as_before(client: AsyncClient, db, acting):
    world = await _anas_world(db, acting)
    view_as(AgendaViewer("own", world.appointment.professional_id))

    preview = await _call(client, world, world.appointment.id, "cancel-preview")
    edited = await _call(client, world, world.appointment.id, "edit")

    assert preview.status_code == 200 and preview.json()["professional_name"] == "Dra. Ana"
    assert edited.status_code == 200, edited.text
    assert (await reload_appointment(db, world.appointment.id)).attendee_name == "Outro nome"


async def test_a_manager_who_is_also_a_doctor_reaches_every_doctor(client: AsyncClient, db, acting):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("clinic", beto))

    response = await _call(client, world, world.appointment.id, "cancel-preview")

    assert response.status_code == 200


def _create_body(**extra) -> dict:
    start = soon(5)
    return {
        "start": _iso(start),
        "end": _iso(start + timedelta(minutes=30)),
        "summary": "Consulta",
        **extra,
    }


async def _created(db, appointment_id: str) -> Appointment:
    async with db() as session:
        return await session.scalar(
            select(Appointment).where(Appointment.id == UUID(appointment_id))
        )


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_restricted_doctor_creates_on_his_own_agenda_by_default(
    client: AsyncClient, db, acting, path
):
    world = await _anas_world(db, acting)
    ana = world.appointment.professional_id
    view_as(AgendaViewer("own", ana))

    response = await client.post(f"{CALENDAR}/{path}", json=_create_body())

    assert response.status_code == 201, response.text
    row = await _created(db, response.json()["id"])
    assert row.professional_id == ana  # visible in his own "own" view at once
    assert len(FakeCalendar.get("tenant").created) == 1  # Google unchanged: the clinic calendar


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_restricted_doctor_may_name_himself(client: AsyncClient, db, acting, path):
    world = await _anas_world(db, acting)
    ana = world.appointment.professional_id
    view_as(AgendaViewer("own", ana))

    response = await client.post(f"{CALENDAR}/{path}", json=_create_body(professional_id=str(ana)))

    assert response.status_code == 201, response.text
    assert (await _created(db, response.json()["id"])).professional_id == ana


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_restricted_doctor_cannot_create_on_another_doctors_agenda(
    client: AsyncClient, db, acting, path
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", world.appointment.professional_id))

    response = await client.post(f"{CALENDAR}/{path}", json=_create_body(professional_id=str(beto)))

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "professional_not_allowed"
    assert FakeCalendar.instances == {}  # refused before Google
    async with db() as session:
        rows = list(
            await session.scalars(
                select(Appointment).where(Appointment.tenant_id == world.tenant.id)
            )
        )
    assert [r.id for r in rows] == [world.appointment.id]


@pytest.mark.parametrize("path", ["appointments", "blocks"])
async def test_a_restricted_viewer_without_a_professional_has_no_agenda_to_create_on(
    client: AsyncClient, db, acting, path
):
    await _anas_world(db, acting)
    view_as(AgendaViewer("own", None))

    response = await client.post(f"{CALENDAR}/{path}", json=_create_body())

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "no_own_agenda"
    assert FakeCalendar.instances == {}


async def test_a_clinic_viewer_may_name_any_active_doctor_of_the_clinic(
    client: AsyncClient, db, acting
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("clinic", None))  # a receptionist

    named = await client.post(
        f"{CALENDAR}/appointments", json=_create_body(professional_id=str(beto))
    )
    unnamed = await client.post(f"{CALENDAR}/appointments", json=_create_body())

    assert named.status_code == 201 and unnamed.status_code == 201
    assert (await _created(db, named.json()["id"])).professional_id == beto
    assert (await _created(db, unnamed.json()["id"])).professional_id is None  # today's behaviour


@pytest.mark.parametrize("which", ["inactive", "foreign", "unknown"])
async def test_a_clinic_viewer_cannot_name_a_doctor_outside_the_clinic(
    client: AsyncClient, db, acting, other_tenant, which
):
    world = await _anas_world(db, acting)
    view_as(AgendaViewer("clinic", None))
    if which == "inactive":
        target = await add_professional(db, world.tenant.id, "Dr. Antigo", active=False)
    elif which == "foreign":
        target = await add_professional(db, other_tenant.id, "Dr. Outra Clínica")
    else:
        target = uuid4()

    response = await client.post(
        f"{CALENDAR}/blocks", json=_create_body(professional_id=str(target))
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "unknown_professional"
    assert FakeCalendar.instances == {}


async def test_a_restricted_doctor_cannot_hand_his_appointment_to_another_doctor(
    client: AsyncClient, db, acting
):
    world = await _anas_world(db, acting)
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", world.appointment.professional_id))

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/edit",
        json={"professional_id": str(beto)},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "professional_not_allowed"
    row = await reload_appointment(db, world.appointment.id)
    assert row.professional_id == world.appointment.professional_id
    assert FakeCalendar.instances == {} and sent() == []
