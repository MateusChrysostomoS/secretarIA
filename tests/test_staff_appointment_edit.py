"""The clinic's Editar/Remarcar service (TASK-032 R7, spec 2026-10-09 §1/§3/§4)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    ProfessionalEditNotice,
    Tenant,
)
from secretaria.services import staff_appointment_edit as sae
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.staff_appointment_edit import StaffEditError, StaffEditRequest
from tests._r7_support import FakeCalendar, add_professional, set_appointment, set_tenant, soon
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world

SERVICES = [
    {"name": "Consulta", "is_active": True, "duration_min": 30},
    {"name": "Retorno", "is_active": True, "duration_min": 40},
]


@pytest.fixture(autouse=True)
def _calendars():
    FakeCalendar.reset()


async def calendar_for(professional_id):
    return FakeCalendar.get(str(professional_id) if professional_id else "tenant")


def _req(**values) -> StaffEditRequest:
    allow = values.pop("allow_overlap", False)
    return StaffEditRequest(fields=frozenset(values), allow_overlap=allow, **values)


async def _world(db, **kwargs):
    kwargs.setdefault("start_at", soon())
    kwargs.setdefault("google_event_id", "evt-1")
    world = await seed_world(db, **kwargs)
    await set_tenant(db, world.tenant.id, appointment_types=SERVICES)
    return world


async def _edit(db, world, request):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        return await sae.apply_staff_edit(
            session, tenant, appointment, request, calendar_for=calendar_for
        )


async def _notices(db, appointment_id) -> list[ProfessionalEditNotice]:
    async with db() as session:
        return list(
            await session.scalars(
                select(ProfessionalEditNotice).where(
                    ProfessionalEditNotice.appointment_id == appointment_id
                )
            )
        )


def _labels(outcome) -> list[str]:
    return [change.label for change in outcome.changes]


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


async def test_a_new_time_moves_the_event_and_the_row_keeping_the_duration(db):
    world = await _world(db)
    new = world.start_at + timedelta(days=1, hours=1)

    outcome = await _edit(db, world, _req(start_at=new))

    assert _labels(outcome) == ["Data", "Horário"] and outcome.time_changed is True
    [(event_id, start, end, summary, description)] = FakeCalendar.get("tenant").updated
    assert (event_id, start, end, summary) == (
        "evt-1",
        new,
        new + timedelta(minutes=30),
        "Consulta - Maria",
    )
    assert "Convênio: não informado" in description
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == new and row.status == AppointmentStatus.RESCHEDULED
    [notice] = await _notices(db, world.appointment.id)
    assert notice.changed_fields == ["data", "horário"]
    assert outcome.notice_id == str(notice.id)


async def test_a_new_service_takes_its_duration_and_mails_no_doctor(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(service="retorno"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Serviço", "Consulta", "Retorno")
    ]
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno"
    assert row.end_at - row.start_at == timedelta(minutes=40)
    assert outcome.notice_id is None and await _notices(db, world.appointment.id) == []


async def test_a_doctor_on_another_agenda_is_created_there_and_the_old_event_deleted(db):
    world = await _world(db, professional_name="Dra. Ana")
    old_pro = world.appointment.professional_id
    new_pro = await add_professional(db, world.tenant.id, "Dr. Bruno")

    outcome = await _edit(db, world, _req(professional_id=new_pro))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Médico", "Dra. Ana", "Dr. Bruno")
    ]
    assert len(FakeCalendar.get(str(new_pro)).created) == 1
    assert FakeCalendar.get(str(old_pro)).cancelled == ["evt-1"]
    row = await reload_appointment(db, world.appointment.id)
    assert row.professional_id == new_pro and row.google_event_id == f"evt-new-{new_pro}"
    assert [n.changed_fields for n in await _notices(db, world.appointment.id)] == [["médico"]]


async def test_a_doctor_on_the_same_agenda_keeps_the_event(db):
    world = await _world(db, professional_name="Dra. Ana")
    old_pro = world.appointment.professional_id
    new_pro = await add_professional(db, world.tenant.id, "Dr. Bruno")
    FakeCalendar.instances[str(old_pro)] = FakeCalendar("shared")
    FakeCalendar.instances[str(new_pro)] = FakeCalendar("shared")

    await _edit(db, world, _req(professional_id=new_pro))

    assert len(FakeCalendar.get(str(old_pro)).updated) == 1
    assert FakeCalendar.get(str(new_pro)).created == []
    assert FakeCalendar.get(str(old_pro)).cancelled == []
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == "evt-1"


async def test_a_taken_slot_is_409_unless_the_clinic_squeezes_it_in(db):
    world = await _world(db)
    FakeCalendar.get("tenant").free = False
    new = world.start_at + timedelta(hours=2)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=new))
    assert (err.value.status_code, err.value.code) == (409, "slot_unavailable")
    assert FakeCalendar.get("tenant").updated == []
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == world.start_at

    await _edit(db, world, _req(start_at=new, allow_overlap=True))
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == new


async def test_google_refusing_is_502_and_nothing_changes(db):
    world = await _world(db)
    FakeCalendar.get("tenant").fail = CalendarUnavailableError("down")

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    assert (err.value.status_code, err.value.code) == (502, "calendar_unavailable")
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == world.start_at and row.status == AppointmentStatus.SCHEDULED


async def test_a_database_failure_after_google_puts_google_back(db, monkeypatch):
    world = await _world(db)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(sae, "write_appointment_edit", _boom)

    with pytest.raises(RuntimeError):
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    moved, restored = FakeCalendar.get("tenant").updated
    assert restored[1] == world.start_at and restored[3] == "Consulta - Maria"
    assert _utc((await reload_appointment(db, world.appointment.id)).start_at) == world.start_at


async def test_a_row_cancelled_while_google_was_moving_is_409_and_google_is_put_back(db):
    world = await _world(db)

    async def _someone_cancels() -> None:
        await set_appointment(db, world.appointment.id, status=AppointmentStatus.CANCELLED)

    FakeCalendar.get("tenant").during = _someone_cancels

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    assert (err.value.status_code, err.value.code) == (409, "not_live")
    assert len(FakeCalendar.get("tenant").updated) == 2  # moved, then restored


@pytest.mark.parametrize(
    ("request_values", "code"),
    [
        ({"insurance": None}, "nothing_changed"),
        ({"service": "Cirurgia"}, "service_not_offered"),
        ({"phone": "1234"}, "invalid_phone"),
    ],
)
async def test_validation_errors_are_422(db, request_values, code):
    world = await _world(db)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(**request_values))

    assert (err.value.status_code, err.value.code) == (422, code)
    assert FakeCalendar.instances == {}  # Google never touched


async def test_the_same_time_is_nothing_changed_and_a_past_time_is_refused(db):
    world = await _world(db)
    with pytest.raises(StaffEditError) as same:
        await _edit(db, world, _req(start_at=world.start_at))
    assert same.value.code == "nothing_changed"

    with pytest.raises(StaffEditError) as past:
        await _edit(db, world, _req(start_at=datetime.now(UTC) - timedelta(hours=1)))
    assert (past.value.status_code, past.value.code) == (422, "start_in_past")


async def test_a_professional_of_another_clinic_or_inactive_is_unknown(db):
    world = await _world(db)
    other = await seed_world(db, phone_number_id="pnid-2", professional_name="Dr. Fora")
    retired = await add_professional(db, world.tenant.id, "Dr. Antigo", active=False)

    for professional_id in (other.appointment.professional_id, retired):
        with pytest.raises(StaffEditError) as err:
            await _edit(db, world, _req(professional_id=professional_id))
        assert (err.value.status_code, err.value.code) == (422, "unknown_professional")


async def test_only_the_contact_phone_changes_nothing_else(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(phone="+55 (11) 97777-6666"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Telefone de contato", "não informado", "5511977776666")
    ]
    assert FakeCalendar.instances == {}  # the event carries no phone: Google untouched
    assert outcome.notice_id is None
    row = await reload_appointment(db, world.appointment.id)
    assert row.phone == "5511977776666"
    async with db() as session:
        assert (await session.get(Patient, world.patient.id)).wa_id == WA_ID


async def test_convenio_and_who_it_is_for_change_and_can_be_cleared(db):
    world = await _world(db)

    outcome = await _edit(db, world, _req(insurance="Unimed", attendee_name="João"))

    assert [(c.label, c.before, c.after) for c in outcome.changes] == [
        ("Convênio", "não informado", "Unimed"),
        ("Paciente", "você", "João"),
    ]
    assert FakeCalendar.get("tenant").updated[0][3] == "Consulta - João"

    cleared = await _edit(db, world, _req(insurance="", attendee_name=None))
    assert [(c.label, c.after) for c in cleared.changes] == [
        ("Convênio", "não informado"),
        ("Paciente", "você"),
    ]
    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance is None and row.attendee_name is None


async def test_a_closed_appointment_or_a_block_cannot_be_edited(db):
    closed = await _world(db, status=AppointmentStatus.CANCELLED)
    with pytest.raises(StaffEditError) as err:
        await _edit(db, closed, _req(insurance="Unimed"))
    assert (err.value.status_code, err.value.code) == (409, "not_live")

    block = await _world(db, phone_number_id="pnid-3")
    await set_appointment(db, block.appointment.id, patient_id=None, phone=None)
    with pytest.raises(StaffEditError) as err:
        await _edit(db, block, _req(insurance="Unimed"))
    assert (err.value.status_code, err.value.code) == (422, "not_a_patient_appointment")


def test_phone_normalisation():
    assert sae.normalize_phone("+55 (11) 98888-7777") == "5511988887777"
    assert sae.normalize_phone("  ") is None and sae.normalize_phone(None) is None
    for bad in ("11988887777", "1" * 16, "abc"):
        with pytest.raises(ValueError):
            sae.normalize_phone(bad)


@pytest.mark.parametrize(
    "changed",
    [
        {"insurance": "Unimed"},
        {"attendee_name": "João"},
        {"phone": "5511999990000"},
        {"appointment_type": "Retorno"},
    ],
)
async def test_a_concurrent_field_edit_is_not_overwritten(db, changed):
    world = await _world(db)

    async def concurrent():
        await set_appointment(db, world.appointment.id, **changed)

    FakeCalendar.get("tenant").during = concurrent
    with pytest.raises(StaffEditError) as error:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))
    assert error.value.code == "appointment_changed"
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == world.start_at
    for key, value in changed.items():
        assert getattr(row, key) == value
    restored = FakeCalendar.get("tenant").updated[-1]
    assert restored[1] == world.start_at
    if "insurance" in changed:
        assert "Convênio: Unimed" in restored[4]
    if "attendee_name" in changed:
        assert restored[3] == "Consulta - João"
    if "appointment_type" in changed:
        assert restored[3] == "Retorno - Maria"
