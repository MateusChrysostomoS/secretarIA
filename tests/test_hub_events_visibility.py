"""GET /events: who sees what, "só os meus" and the Editar pre-fill (TASK-044 R7, §5.A/§5.C)."""

# ruff: noqa: F811

from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient

from secretaria.models import Appointment, AppointmentStatus, Patient
from secretaria.services.agenda_visibility import AgendaViewer
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from tests._agenda_viewer import view_as
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeCalendar,
    acting,
    add_professional,
    r7,
    setup_world,
    soon,
)
from tests._reminder_fixtures import db  # noqa: F401

PREFILL_KEYS = {
    "professional_id",
    "professional_name",
    "service",
    "attendee_name",
    "phone",
    "patient_channel",
}
RANGE = {"start": "2026-01-01T00:00:00Z", "end": "2028-01-01T00:00:00Z"}
ALL = {"evt-ana", "evt-beto", "evt-sem-medico", "evt-google"}


def _google(monkeypatch, *ids: str) -> None:
    """The clinic's Google calendar returns these events (titles carry patient names)."""
    events = [
        {
            "id": gid,
            "summary": f"Consulta - paciente de {gid}",
            "start": "2026-10-20T13:00:00Z",
            "end": "2026-10-20T13:30:00Z",
        }
        for gid in ids
    ]

    async def _check(self, start, end):
        return events

    monkeypatch.setattr(FakeCalendar, "check_availability", _check)


async def _clinic(db, acting, monkeypatch):
    """Dra. Ana's WhatsApp patient (evt-ana), Dr. Beto's Portal patient (evt-beto), an
    appointment without a doctor (evt-sem-medico) and an event typed into Google."""
    world = await setup_world(db, acting, professional_name="Dra. Ana", google_event_id="evt-ana")
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    start = soon(4)
    async with db() as session:
        portal = Patient(
            id=uuid4(),
            tenant_id=world.tenant.id,
            wa_id=None,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=str(uuid4()),
            name="João",
        )
        session.add(portal)
        await session.flush()
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=world.tenant.id,
                patient_id=portal.id,
                google_event_id="evt-beto",
                appointment_type="Retorno",
                start_at=start,
                end_at=start + timedelta(minutes=40),
                status=AppointmentStatus.SCHEDULED,
                professional_id=beto,
                attendee_name="Pedro",
                phone="5511977770000",
                insurance="Unimed",
            )
        )
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=world.tenant.id,
                patient_id=None,
                google_event_id="evt-sem-medico",
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
                phone="5511966660000",
            )
        )
        await session.commit()
    _google(monkeypatch, "evt-ana", "evt-beto", "evt-sem-medico", "evt-google")
    return world, beto


async def _events(client: AsyncClient, **params):
    return await client.get(f"{CALENDAR}/events", params={**RANGE, **params})


def _by_id(body) -> dict:
    return {e["id"]: e for e in body}


async def test_the_clinic_sees_every_event_with_the_edit_prefill(
    client: AsyncClient, db, acting, monkeypatch
):
    world, beto = await _clinic(db, acting, monkeypatch)

    body = _by_id((await _events(client)).json())

    assert set(body) == ALL
    ana = body["evt-ana"]
    assert ana["professional_id"] == str(world.appointment.professional_id)
    assert ana["professional_name"] == "Dra. Ana"
    assert (ana["service"], ana["attendee_name"], ana["phone"]) == ("Consulta", None, None)
    assert ana["patient_channel"] == "whatsapp"
    joao = body["evt-beto"]
    assert (joao["professional_id"], joao["professional_name"]) == (str(beto), "Dr. Beto")
    assert (joao["service"], joao["attendee_name"]) == ("Retorno", "Pedro")
    assert (joao["phone"], joao["insurance"]) == ("5511977770000", "Unimed")
    assert joao["patient_channel"] == "brain_message"
    orphan = body["evt-sem-medico"]
    assert orphan["professional_id"] is None and orphan["professional_name"] is None
    assert orphan["patient_channel"] is None and orphan["phone"] == "5511966660000"
    assert all(body["evt-google"][key] is None for key in PREFILL_KEYS)


@pytest.mark.parametrize("mine", [None, "false", "true"])
async def test_a_restricted_doctor_sees_only_his_own_whatever_the_filter_says(
    client: AsyncClient, db, acting, monkeypatch, mine
):
    _world, beto = await _clinic(db, acting, monkeypatch)
    view_as(AgendaViewer("own", beto))

    response = await _events(client, **({} if mine is None else {"mine": mine}))

    assert response.status_code == 200
    body = response.json()
    assert [e["id"] for e in body] == ["evt-beto"]
    assert "paciente de evt-ana" not in response.text  # no other title, Google-only included


async def test_a_restricted_doctor_without_a_professional_sees_nothing(
    client: AsyncClient, db, acting, monkeypatch
):
    await _clinic(db, acting, monkeypatch)
    view_as(AgendaViewer("own", None))

    response = await _events(client)

    assert response.status_code == 200 and response.json() == []


async def test_a_manager_who_is_also_a_doctor_switches_between_all_and_his_own(
    client: AsyncClient, db, acting, monkeypatch
):
    _world, beto = await _clinic(db, acting, monkeypatch)
    view_as(AgendaViewer("clinic", beto))

    everyone = await _events(client, mine="false")
    his_own = await _events(client, mine="true")

    assert set(_by_id(everyone.json())) == ALL
    assert [e["id"] for e in his_own.json()] == ["evt-beto"]


async def test_a_receptionist_asking_for_her_own_agenda_is_422(
    client: AsyncClient, db, acting, monkeypatch
):
    await _clinic(db, acting, monkeypatch)  # conftest default viewer: CLINIC_WIDE

    response = await _events(client, mine="true")

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "no_own_agenda"


async def test_a_doctor_of_another_clinic_on_the_row_never_resolves(
    client: AsyncClient, db, acting, monkeypatch
):
    world, _beto = await _clinic(db, acting, monkeypatch)
    other = await setup_world(
        db, acting, phone_number_id="pnid-2", wa_id="5511900000002", professional_name="Dr. Fora"
    )
    acting["tenant_id"] = world.tenant.id
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.professional_id = other.appointment.professional_id
        await session.commit()

    ana = _by_id((await _events(client)).json())["evt-ana"]

    assert ana["professional_id"] is None and ana["professional_name"] is None
