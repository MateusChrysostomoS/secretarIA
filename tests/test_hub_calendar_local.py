# ruff: noqa: F811
"""The hub agenda of a clinic WITHOUT Google: every route on the secretarIA agenda.

Native agenda (founder's decision 2026-10-20). Nothing here is faked on the calendar
side: the routes build their agendas through the real factories, which pick
services/local_calendar.py because no Google credential is stored. Only the patient
notices (WhatsApp/Portal delivery, tested elsewhere) are stubbed.
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.models.calendar_event import CalendarEvent
from secretaria.services import appointment_release, clinic_action_notice
from secretaria.services.channel_sender import CHANNEL_WHATSAPP
from secretaria.services.tenant_config import set_google_refresh_token
from tests._local_agenda import (
    agenda,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda as _wire_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401

CALENDAR = "/tenants/me/calendar"


@pytest.fixture
def acting() -> dict:
    return {"tenant_id": None}


@pytest.fixture(autouse=True)
def _wire(db, acting, monkeypatch: pytest.MonkeyPatch):
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, acting["tenant_id"])

    async def _notice(*_args, **_kwargs):
        return "whatsapp_queued"

    async def _staff_notice(*_args, **_kwargs):
        return SimpleNamespace(code=None, whatsapp_link=None)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    _wire_agenda(monkeypatch, db)
    monkeypatch.setattr(appointment_release, "notify_cancelled_patient", _notice)
    monkeypatch.setattr(appointment_release, "notify_released_patient", _notice)
    for name in ("notify_staff_edit", "notify_staff_confirmation", "notify_attended"):
        monkeypatch.setattr(clinic_action_notice, name, _staff_notice)
    app.state.arq_pool = None
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _clinic(db, acting) -> Tenant:
    tenant = await seed_tenant(db)
    acting["tenant_id"] = tenant.id
    return tenant


async def _events(db, tenant) -> list[CalendarEvent]:
    async with db() as session:
        rows = await session.scalars(
            select(CalendarEvent).where(CalendarEvent.tenant_id == tenant.id)
        )
        return list(rows)


async def _appointment(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _create(client, start, **extra):
    body = {
        "start": start.isoformat(),
        "end": (start + timedelta(minutes=30)).isoformat(),
        "summary": "Consulta - Joana",
        "phone": "5511999990000",
        **extra,
    }
    return await client.post(f"{CALENDAR}/appointments", json=body)


async def _patient_appointment(db, tenant, start) -> Appointment:
    """A WhatsApp patient's booking, made the way the bot makes it (on the local agenda)."""
    event = await agenda(tenant.id).create_event(start, start + timedelta(minutes=30), "Consulta")
    async with db() as session:
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511988887777", name="Rui",
                          channel=CHANNEL_WHATSAPP)
        session.add(patient)
        await session.flush()
        appointment = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=event["id"],
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            phone="5511988887777",
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.commit()
        await session.refresh(appointment)
        return appointment


# --------------------------------------------------------------------------
# Read
# --------------------------------------------------------------------------


async def test_the_agenda_opens_without_google_and_lists_local_events(
    client: AsyncClient, db, acting
):
    tenant = await _clinic(db, acting)
    start = tomorrow_at(10)
    created = (await _create(client, start)).json()

    resp = await client.get(
        f"{CALENDAR}/events",
        params={"start": (start - timedelta(days=1)).isoformat(),
                "end": (start + timedelta(days=1)).isoformat()},
    )

    assert resp.status_code == 200, resp.text  # no more 422 "Google not connected"
    [event] = resp.json()
    assert event["id"] == created["google_event_id"] and event["id"].startswith("loc_")
    assert event["appointment_id"] == created["id"]
    assert event["calendar_backend"] == "local"
    assert event["status"] == "scheduled"
    assert len(await _events(db, tenant)) == 1


async def test_the_config_says_the_agenda_is_the_secretarias(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    body = (await client.get("/tenants/me/config")).json()
    assert body["calendar_connected"] is False
    assert body["calendar_backend"] == "local"

    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "clinic-token")
        await session.commit()
    body = (await client.get("/tenants/me/config")).json()
    assert (body["calendar_connected"], body["calendar_backend"]) == (True, "google")


async def test_each_doctor_lists_from_his_own_local_agenda(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    ana = await seed_professional(db, tenant, "Dra. Ana")
    bia = await seed_professional(db, tenant, "Dra. Bia")
    start = tomorrow_at(10)
    a = (await _create(client, start, professional_id=str(ana.id))).json()
    b = (await _create(client, start, professional_id=str(bia.id))).json()
    window = {"start": (start - timedelta(hours=1)).isoformat(),
              "end": (start + timedelta(hours=1)).isoformat()}

    events = (await client.get(f"{CALENDAR}/events", params=window)).json()

    assert {e["appointment_id"] for e in events} == {a["id"], b["id"]}
    assert {e["professional_name"] for e in events} == {"Dra. Ana", "Dra. Bia"}
    rows = {row.id: row.professional_id for row in await _events(db, tenant)}
    assert rows == {a["google_event_id"]: ana.id, b["google_event_id"]: bia.id}
    # Two doctors at the same hour is not a conflict: each has his own agenda.
    assert await agenda(tenant.id, ana.id).has_conflict(start, start + timedelta(minutes=30))
    assert not await agenda(tenant.id).has_conflict(start, start + timedelta(minutes=30))


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


async def test_create_and_block_write_the_local_agenda(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    start = tomorrow_at(10)

    created = await _create(client, start)
    block = await client.post(
        f"{CALENDAR}/blocks",
        json={"start": (start + timedelta(hours=2)).isoformat(),
              "end": (start + timedelta(hours=3)).isoformat()},
    )

    assert created.status_code == 201 and block.status_code == 201
    assert created.json()["google_event_link"] is None
    cal = agenda(tenant.id)
    assert await cal.has_conflict(start, start + timedelta(minutes=30))
    assert await cal.has_conflict(start + timedelta(hours=2), start + timedelta(hours=3))
    appt = await _appointment(db, UUID(block.json()["id"]))
    assert appt.appointment_type == "Bloqueado" and appt.google_event_id.startswith("loc_")


async def test_cancel_frees_the_slot(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    start = tomorrow_at(10)
    created = (await _create(client, start)).json()

    resp = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/cancel", json={"confirm": True}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "cancelled"
    assert not await agenda(tenant.id).has_conflict(start, start + timedelta(minutes=30))
    [row] = await _events(db, tenant)
    assert row.status == "cancelled"


async def test_release_frees_an_unconfirmed_patient_slot(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    start = tomorrow_at(10)
    appointment = await _patient_appointment(db, tenant, start)

    resp = await client.post(f"{CALENDAR}/appointments/{appointment.id}/release", json={})

    assert resp.status_code == 200, resp.text
    assert (await _appointment(db, appointment.id)).status == AppointmentStatus.CANCELLED
    assert not await agenda(tenant.id).has_conflict(start, start + timedelta(minutes=30))


async def test_reschedule_moves_the_local_event(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    start = tomorrow_at(10)
    created = (await _create(client, start)).json()
    new_start = start + timedelta(hours=3)

    resp = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/reschedule",
        json={"new_start": new_start.isoformat(),
              "new_end": (new_start + timedelta(minutes=30)).isoformat()},
    )

    assert resp.status_code == 200, resp.text
    cal = agenda(tenant.id)
    assert not await cal.has_conflict(start, start + timedelta(minutes=30))
    assert await cal.has_conflict(new_start, new_start + timedelta(minutes=30))
    assert resp.json()["google_event_id"] == created["google_event_id"]


async def test_reschedule_of_an_event_gone_from_the_agenda_is_409(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    start = tomorrow_at(10)
    created = (await _create(client, start)).json()
    await agenda(tenant.id).cancel_event(created["google_event_id"])

    resp = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/reschedule",
        json={"new_start": (start + timedelta(hours=1)).isoformat(),
              "new_end": (start + timedelta(hours=2)).isoformat()},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "calendar_event_not_found"


async def test_edit_moves_the_time_and_refuses_a_taken_one(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    start = tomorrow_at(10)
    created = (await _create(client, start)).json()
    busy = start + timedelta(hours=2)
    await _create(client, busy)

    taken = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/edit", json={"start_at": busy.isoformat()}
    )
    assert taken.status_code == 409
    assert taken.json()["detail"]["code"] == "slot_unavailable"

    free = start + timedelta(hours=4)
    moved = await client.post(
        f"{CALENDAR}/appointments/{created['id']}/edit", json={"start_at": free.isoformat()}
    )
    assert moved.status_code == 200, moved.text
    cal = agenda(tenant.id)
    assert await cal.has_conflict(free, free + timedelta(minutes=30))
    assert not await cal.has_conflict(start, start + timedelta(minutes=30))


async def test_confirm_and_attendance_need_no_calendar(client: AsyncClient, db, acting):
    tenant = await _clinic(db, acting)
    start = tomorrow_at(10)
    appointment = await _patient_appointment(db, tenant, start)

    confirmed = await client.patch(
        f"{CALENDAR}/appointments/{appointment.id}/status", json={"status": "confirmed"}
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["status"] == "confirmed"
    # The booking still holds its slot after the confirmation.
    assert await agenda(tenant.id).has_conflict(start, start + timedelta(minutes=30))
