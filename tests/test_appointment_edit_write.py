"""The shared row writer of an appointment edit (TASK-032 R6 extraction, used by R7)."""

from datetime import timedelta

import pytest

from secretaria.models import Appointment, AppointmentStatus
from secretaria.services import appointment_edit_write as writer
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import seed_world


def _edit(world, **overrides) -> dict:
    base = {
        "appointment_type": "Consulta",
        "professional_id": None,
        "insurance": None,
        "attendee_name": None,
        "google_event_id": world.appointment.google_event_id,
        "google_event_link": None,
        "start_at": world.start_at,
        "end_at": world.start_at + timedelta(minutes=30),
        "time_changed": False,
        "doctor_changed": False,
    }
    base.update(overrides)
    return base


async def _write(db, world, edit, *, source="hub"):  # noqa: F811
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        fields = await writer.write_appointment_edit(
            session,
            appointment,
            tenant_id=world.tenant.id,
            timezone="America/Sao_Paulo",
            edit=edit,
            source=source,
            idempotency_key="key-1",
        )
        await session.commit()
        await session.refresh(appointment)
        return fields, appointment


@pytest.fixture
def transitions(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    monkeypatch.setattr(writer, "log_status_transition", lambda **kw: calls.append(kw))
    return calls


async def test_a_new_time_moves_the_row_and_logs_a_reschedule(db, transitions):  # noqa: F811
    world = await seed_world(db)
    new = world.start_at + timedelta(hours=1)

    fields, row = await _write(
        db, world, _edit(world, start_at=new, end_at=new + timedelta(minutes=30), time_changed=True)
    )

    assert fields == ["horário"]
    assert row.status == AppointmentStatus.RESCHEDULED
    assert row.start_at.replace(tzinfo=new.tzinfo) == new
    [call] = transitions
    assert (call["source"], call["idempotency_key"]) == ("hub", "key-1")
    assert call["new_status"] == AppointmentStatus.RESCHEDULED


async def test_without_a_new_time_the_status_and_start_stay(db, transitions):  # noqa: F811
    world = await seed_world(db)

    fields, row = await _write(
        db,
        world,
        _edit(world, appointment_type="Retorno", end_at=world.start_at + timedelta(minutes=40)),
    )

    assert fields == ["serviço"]
    assert row.status == AppointmentStatus.SCHEDULED and transitions == []
    assert row.appointment_type == "Retorno"
    assert (row.end_at - row.start_at) == timedelta(minutes=40)


async def test_convenio_and_attendee_are_written_and_reported(db, transitions):  # noqa: F811
    world = await seed_world(db)

    fields, row = await _write(
        db, world, _edit(world, insurance="Particular", attendee_name="João")
    )

    assert fields == ["convênio", "paciente"]
    assert (row.insurance, row.attendee_name) == ("Particular", "João")
    assert row.insurance_plan_id is None and row.insurance_professional_plan_id is None


async def test_the_event_link_changes_only_with_the_calendar(db, transitions):  # noqa: F811
    world = await seed_world(db, google_event_id="evt-1")

    _, kept = await _write(
        db, world, _edit(world, google_event_link="https://new", calendar_changed=False)
    )
    assert kept.google_event_link is None

    _, moved = await _write(
        db,
        world,
        _edit(
            world, google_event_id="evt-2", google_event_link="https://new", calendar_changed=True
        ),
    )
    assert (moved.google_event_id, moved.google_event_link) == ("evt-2", "https://new")


def test_row_draft_is_the_r6_draft_of_the_row():
    from secretaria.workers.shared import appointment_edit_apply

    assert appointment_edit_apply._row_draft is writer.row_draft
