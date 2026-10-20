"""Real event ownership regressions for hub-created doctor-owned appointments.

Owner's decision 2026-10-19: the hub creates a doctor's consultation/block on THAT
doctor's agenda (origin "professional"). Rows created before it sit on the clinic's
agenda with an explicit "clinic" origin and a doctor (`_legacy_hub_row`); everything
that acts on them must keep finding the clinic's event.
"""

# ruff: noqa: F811

from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest

from secretaria.api.hub import calendar as hub
from secretaria.models import Appointment, Tenant
from secretaria.services import appointment_edit_flow as flow
from secretaria.services.appointment_edit import EditContext, EditDraft
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.patient_context import load_upcoming_appointments
from secretaria.workers.shared.actions import _calendar_for_appointment as real_action_calendar
from secretaria.workers.shared.appointment_edit_apply import _finish_edit, apply_appointment_edit
from secretaria.workers.shared.llm_context import _appointment_calendar as real_worker_calendar
from tests._r7_support import (
    CALENDAR,
    FakeCalendar,
    acting,  # noqa: F401
    add_professional,
    r7,  # noqa: F401
    set_tenant,
    setup_world,
    soon,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment


class OwnedCalendar(FakeCalendar):
    tables = {}

    def __init__(self, label="tenant"):
        super().__init__(label)
        self.events = self.tables.setdefault(label, {})

    async def create_event(self, start, end, summary, description="", reminders=None):
        event_id = f"event-{self.label}-{len(self.events)}"
        self.events[event_id] = (start, end, summary, description)
        return {"id": event_id, "htmlLink": f"https://calendar.google.com/{event_id}"}

    async def update_event_details(self, event_id, start, end, summary, description=""):
        if event_id not in self.events:
            raise CalendarUnavailableError("event not present on this calendar")
        self.events[event_id] = (start, end, summary, description)
        return {"id": event_id}

    async def cancel_event(self, event_id):
        # Match Google: 404 deletion is success; the test must assert real removal.
        self.events.pop(event_id, None)


@pytest.fixture(autouse=True)
def calendars(r7, monkeypatch):
    OwnedCalendar.instances = {}
    OwnedCalendar.tables = {}
    monkeypatch.setattr(hub, "CalendarService", OwnedCalendar)

    async def resolve(session, tenant, professional, **kwargs):
        return OwnedCalendar.get(str(professional.id))

    monkeypatch.setattr(hub, "resolve_professional_calendar", resolve)
    from secretaria.workers.shared import actions, appointment_edit_apply, llm_context

    monkeypatch.setattr(actions, "_calendar_for_appointment", real_action_calendar)
    monkeypatch.setattr(llm_context, "_appointment_calendar", real_worker_calendar)
    monkeypatch.setattr(appointment_edit_apply, "_appointment_calendar", real_worker_calendar)

    monkeypatch.setattr(actions, "CalendarService", OwnedCalendar)
    monkeypatch.setattr(actions, "resolve_professional_calendar", resolve)
    monkeypatch.setattr(llm_context, "CalendarService", OwnedCalendar)
    monkeypatch.setattr(llm_context, "resolve_professional_calendar", resolve)


async def _created(client, db, acting, *, with_doctor: bool = True):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    await set_tenant(
        db,
        world.tenant.id,
        appointment_types=[{"name": "Consulta", "is_active": True, "duration_min": 30}],
    )
    start = soon(4)
    doctor = {"professional_id": str(world.appointment.professional_id)} if with_doctor else {}
    result = await client.post(
        f"{CALENDAR}/appointments",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
            "summary": "Consulta",
            "patient_id": str(world.patient.id),
            **doctor,
        },
    )
    assert result.status_code == 201
    row = await reload_appointment(db, UUID(result.json()["id"]))
    return world, row


async def _legacy_hub_row(client, db, acting):
    """A doctor's hub row as written before 2026-10-19: clinic agenda, "clinic" origin."""
    from tests._r7_support import set_appointment

    world, row = await _created(client, db, acting, with_doctor=False)
    assert row.google_calendar_source == "clinic"
    await set_appointment(db, row.id, professional_id=world.appointment.professional_id)
    return world, await reload_appointment(db, row.id)


async def test_hub_doctor_creation_edit_and_release_use_the_doctors_own_event(client, db, acting):
    world, row = await _created(client, db, acting)
    doctor = OwnedCalendar.get(str(world.appointment.professional_id))
    event_id = row.google_event_id
    assert row.google_calendar_source == "professional"
    assert event_id in doctor.events and OwnedCalendar.get("tenant").events == {}
    edited = await client.post(
        f"{CALENDAR}/appointments/{row.id}/edit", json={"insurance": "Unimed"}
    )
    assert edited.status_code == 200
    assert "Convênio: Unimed" in doctor.events[event_id][3]
    released = await client.post(f"{CALENDAR}/appointments/{row.id}/release", json={})
    assert released.status_code == 200
    assert doctor.events == {} and OwnedCalendar.get("tenant").events == {}


async def test_hub_creation_without_a_doctor_stays_on_the_clinic_agenda(client, db, acting):
    world, row = await _created(client, db, acting, with_doctor=False)
    assert row.professional_id is None and row.google_calendar_source == "clinic"
    assert row.google_event_id in OwnedCalendar.get("tenant").events
    assert OwnedCalendar.get(str(world.appointment.professional_id)).events == {}


async def test_i3_legacy_hub_row_edit_and_release_use_clinic_event(client, db, acting):
    world, row = await _legacy_hub_row(client, db, acting)
    event_id = row.google_event_id
    assert event_id in OwnedCalendar.get("tenant").events
    edited = await client.post(
        f"{CALENDAR}/appointments/{row.id}/edit", json={"insurance": "Unimed"}
    )
    assert edited.status_code == 200
    assert "Convênio: Unimed" in OwnedCalendar.get("tenant").events[event_id][3]
    released = await client.post(f"{CALENDAR}/appointments/{row.id}/release", json={})
    assert released.status_code == 200
    assert event_id not in OwnedCalendar.get("tenant").events
    assert OwnedCalendar.get(str(world.appointment.professional_id)).events == {}


async def test_i3_doctor_move_updates_origin_and_removes_original_clinic_event(client, db, acting):
    world, row = await _legacy_hub_row(client, db, acting)
    doctor = await add_professional(db, world.tenant.id, "Dr. Beto")
    result = await client.post(
        f"{CALENDAR}/appointments/{row.id}/edit",
        json={"professional_id": str(doctor), "service": "Consulta"},
    )
    assert result.status_code == 200
    current = await reload_appointment(db, row.id)
    assert row.google_event_id not in OwnedCalendar.get("tenant").events
    assert current.google_event_id in OwnedCalendar.get(str(doctor)).events
    assert current.google_calendar_source == "professional"
    released = await client.post(f"{CALENDAR}/appointments/{row.id}/release", json={})
    assert released.status_code == 200
    assert current.google_event_id not in OwnedCalendar.get(str(doctor)).events


async def test_i3_same_physical_calendar_keeps_clinic_origin(client, db, acting):
    world, row = await _legacy_hub_row(client, db, acting)
    doctor = await add_professional(db, world.tenant.id, "Dr. Beto")
    OwnedCalendar.instances[str(doctor)] = OwnedCalendar.get("tenant")
    result = await client.post(
        f"{CALENDAR}/appointments/{row.id}/edit",
        json={"professional_id": str(doctor), "service": "Consulta"},
    )
    assert result.status_code == 200
    current = await reload_appointment(db, row.id)
    assert current.google_event_id == row.google_event_id
    assert current.google_calendar_source == "clinic"
    assert len(OwnedCalendar.get("tenant").events) == 1


async def test_i3_r6_patient_edit_of_hub_created_row_patches_actual_origin(client, db, acting):
    world, row = await _legacy_hub_row(client, db, acting)
    async with db() as session:
        row = await session.get(Appointment, row.id)
        row.appointment_type = "Consulta"
        await session.commit()
        tenant = await session.get(Tenant, world.tenant.id)
        view = next(
            item
            for item in await load_upcoming_appointments(session, tenant.id, world.patient.id)
            if item["id"] == str(row.id)
        )
        # The DB view must carry origin, not infer from patient linkage or doctor.
        assert view["google_calendar_source"] == "clinic"
        draft = EditDraft.from_appointment(view, OwnedCalendar.tzinfo).with_changes(
            insurance="Amil"
        )
        doctor = SimpleNamespace(id=row.professional_id, name="Dra. Ana", appointment_types=None)
        ctx = EditContext(
            calendars={
                "tenant": OwnedCalendar.get("tenant"),
                str(doctor.id): OwnedCalendar.get(str(doctor.id)),
            }
        )
        result = await flow._apply_confirm(
            SimpleNamespace(), tenant, draft, view, [doctor], ctx, "Maria"
        )
        assert result.appointment_edit is not None
        applied = await apply_appointment_edit(
            session,
            tenant=tenant,
            tenant_id=tenant.id,
            patient_id=world.patient.id,
            edit=result.appointment_edit,
        )
        assert applied is not None
        await session.commit()
    assert "Convênio: Amil" in OwnedCalendar.get("tenant").events[row.google_event_id][3]
    assert (await reload_appointment(db, row.id)).google_calendar_source == "clinic"


async def test_i3_r6_doctor_move_cleans_up_the_old_clinic_calendar(client, db, acting):
    world, row = await _legacy_hub_row(client, db, acting)
    new_id = await add_professional(db, world.tenant.id, "Dr. Beto")
    async with db() as session:
        row = await session.get(Appointment, row.id)
        row.appointment_type = "Consulta"
        await session.commit()
        tenant = await session.get(Tenant, world.tenant.id)
        view = next(
            item
            for item in await load_upcoming_appointments(session, tenant.id, world.patient.id)
            if item["id"] == str(row.id)
        )
        draft = EditDraft.from_appointment(view, OwnedCalendar.tzinfo).with_changes(
            professional_id=str(new_id)
        )
        doctors = [
            SimpleNamespace(id=pid, name="Médico", appointment_types=None)
            for pid in (row.professional_id, new_id)
        ]
        ctx = EditContext(
            calendars={
                "tenant": OwnedCalendar.get("tenant"),
                **{str(pid): OwnedCalendar.get(str(pid)) for pid in (row.professional_id, new_id)},
            }
        )
        result = await flow._apply_confirm(
            SimpleNamespace(), tenant, draft, view, doctors, ctx, "Maria"
        )
        assert result.appointment_edit is not None and result.appointment_edit["calendar_changed"]
        applied = await apply_appointment_edit(
            session,
            tenant=tenant,
            tenant_id=tenant.id,
            patient_id=world.patient.id,
            edit=result.appointment_edit,
        )
        await session.commit()
    await _finish_edit(tenant, applied)
    assert view["google_event_id"] not in OwnedCalendar.get("tenant").events
    current = await reload_appointment(db, row.id)
    assert current.google_event_id in OwnedCalendar.get(str(new_id)).events
    assert current.google_calendar_source == "professional"


async def test_i3_legacy_null_origin_keeps_professional_inference(db, acting):
    from secretaria.services.appointment_calendar_origin import calendar_professional_id
    from secretaria.workers.shared import actions, llm_context

    world = await setup_world(db, acting, professional_name="Dra. Ana")
    row = await reload_appointment(db, world.appointment.id)
    doctor_id = row.professional_id
    assert row.google_calendar_source is None
    assert calendar_professional_id(row) == doctor_id
    doctor = SimpleNamespace(id=doctor_id)
    assert (
        llm_context._appointment_calendar_target({"professional_id": str(doctor_id)}, [doctor])
        is doctor
    )
    async with db() as session:
        calendar = await actions._calendar_for_appointment(
            session, world.tenant, SimpleNamespace(), row
        )
    assert calendar is OwnedCalendar.get(str(doctor_id))


@pytest.mark.parametrize("with_doctor", [True, False])
async def test_i3_hub_blocks_have_an_explicit_origin(client, db, acting, with_doctor):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    doctor = str(world.appointment.professional_id)
    start = soon(5)
    result = await client.post(
        f"{CALENDAR}/blocks",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
            "summary": "Bloqueio",
            **({"professional_id": doctor} if with_doctor else {}),
        },
    )
    assert result.status_code == 201
    row = await reload_appointment(db, UUID(result.json()["id"]))
    # A doctor's block on HIS agenda: his patients are never offered that slot.
    where, origin = (doctor, "professional") if with_doctor else ("tenant", "clinic")
    assert row.google_calendar_source == origin
    assert row.google_event_id in OwnedCalendar.get(where).events


async def test_i3_source_is_internal_not_editable_by_hub(client, db, acting):
    world, row = await _legacy_hub_row(client, db, acting)
    result = await client.post(
        f"{CALENDAR}/appointments/{row.id}/edit",
        json={"google_calendar_source": "professional", "insurance": "Amil"},
    )
    assert result.status_code == 422
    assert (await reload_appointment(db, row.id)).google_calendar_source == "clinic"


@pytest.mark.parametrize("source", ["professional", "unexpected"])
async def test_i3_explicit_unresolved_origin_never_deletes_clinic_event(client, db, acting, source):
    from tests._r7_support import set_appointment

    world, row = await _legacy_hub_row(client, db, acting)
    await set_appointment(db, row.id, google_calendar_source=source, professional_id=None)
    edited = await client.post(f"{CALENDAR}/appointments/{row.id}/edit", json={"insurance": "Amil"})
    assert edited.status_code == 409 and edited.json()["detail"]["code"] == "calendar_unresolved"
    released = await client.post(f"{CALENDAR}/appointments/{row.id}/release", json={})
    assert (
        released.status_code == 409 and released.json()["detail"]["code"] == "calendar_unresolved"
    )
    assert row.google_event_id in OwnedCalendar.get("tenant").events
    assert (await reload_appointment(db, row.id)).status.value == "scheduled"


@pytest.mark.parametrize("source", ["professional", "unexpected"])
async def test_i3_worker_refuses_explicit_unresolved_calendar_origin(db, acting, source):
    from secretaria.workers.shared import actions, llm_context
    from tests._r7_support import set_appointment

    world = await setup_world(db, acting)
    await set_appointment(
        db, world.appointment.id, google_calendar_source=source, professional_id=None
    )
    row = await reload_appointment(db, world.appointment.id)
    assert (
        llm_context._appointment_calendar_target(
            {"professional_id": None, "google_calendar_source": source}, []
        )
        is None
    )
    async with db() as session:
        assert (
            await actions._calendar_for_appointment(session, world.tenant, SimpleNamespace(), row)
            is None
        )
