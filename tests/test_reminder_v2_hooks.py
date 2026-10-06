"""services/reminder_hooks.py — the schedule follows the appointment (TASK-032 R2)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import Appointment, AppointmentReminder, AppointmentStatus
from secretaria.services import reminder_hooks, reminder_schedule
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, reload_appointment, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    yield


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


async def _rows(db, appointment_id) -> list[AppointmentReminder]:  # noqa: F811
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder).where(
                    AppointmentReminder.appointment_id == appointment_id
                )
            )
        )


def test_enabled_for_reads_only_a_true_switch():
    assert reminder_hooks.enabled_for(SimpleNamespace(reminders_v2_enabled=True)) is True
    assert reminder_hooks.enabled_for(SimpleNamespace(reminders_v2_enabled=False)) is False
    assert reminder_hooks.enabled_for(SimpleNamespace()) is False
    assert reminder_hooks.enabled_for(None) is False


async def test_a_booking_plans_the_day_and_hour_reminders(db):  # noqa: F811
    world = await seed_world(db)

    created = await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW)

    rows = await _rows(db, world.appointment.id)
    assert created == 2
    assert sorted(r.kind for r in rows) == ["day", "hour"]
    assert {r.status for r in rows} == {"pending"}


async def test_a_booking_in_a_clinic_with_the_switch_off_plans_nothing(db):  # noqa: F811
    world = await seed_world(db, v2=False)

    assert await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW) == 0
    assert await _rows(db, world.appointment.id) == []


async def test_booking_twice_is_harmless(db):  # noqa: F811
    world = await seed_world(db)
    await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW)

    assert await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW) == 0
    assert len(await _rows(db, world.appointment.id)) == 2


async def test_a_reschedule_retires_old_rows_and_plans_new_ones(db):  # noqa: F811
    world = await seed_world(db, confirmation_count=1)
    await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW)
    new_start = world.start_at + timedelta(days=2)
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.start_at = new_start
        appointment.end_at = new_start + timedelta(minutes=30)
        appointment.status = AppointmentStatus.RESCHEDULED
        await session.commit()

    created = await reminder_hooks.after_appointment_rescheduled(world.appointment.id, now=NOW)

    rows = await _rows(db, world.appointment.id)
    old = [r for r in rows if _utc(r.appointment_start_at) == world.start_at]
    new = [r for r in rows if _utc(r.appointment_start_at) == new_start]
    assert created == 2
    assert {r.status for r in old} == {"cancelled"}
    assert {r.status for r in new} == {"pending"}
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0


async def test_a_reschedule_with_the_switch_off_still_zeroes_the_confirmation(db):  # noqa: F811
    world = await seed_world(db, v2=False, confirmation_count=1)

    assert await reminder_hooks.after_appointment_rescheduled(world.appointment.id, now=NOW) == 0
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0


async def test_closing_an_appointment_cancels_its_pending_rows(db):  # noqa: F811
    world = await seed_world(db)
    await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW)

    cancelled = await reminder_hooks.after_appointment_closed(
        world.appointment.id, reason="cancelled"
    )

    assert cancelled == 2
    assert {r.status for r in await _rows(db, world.appointment.id)} == {"cancelled"}


async def test_a_hook_never_raises(db, monkeypatch):  # noqa: F811
    async def _boom(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(reminder_schedule, "schedule_reminders", _boom)
    monkeypatch.setattr(reminder_schedule, "cancel_reminders", _boom)
    monkeypatch.setattr(reminder_schedule, "reschedule_reminders", _boom)
    world = await seed_world(db)

    assert await reminder_hooks.after_appointment_booked(world.appointment.id, now=NOW) == 0
    assert await reminder_hooks.after_appointment_rescheduled(world.appointment.id, now=NOW) == 0
    assert await reminder_hooks.after_appointment_closed(world.appointment.id, reason="x") == 0


async def test_reconcile_plans_only_what_is_missing_and_only_where_switched_on(db):  # noqa: F811
    missing = await seed_world(db)
    planned = await seed_world(db, phone_number_id="pnid-2")
    await reminder_hooks.after_appointment_booked(planned.appointment.id, now=NOW)
    switched_off = await seed_world(db, v2=False, phone_number_id="pnid-3")
    already_past = await seed_world(db, start_at=NOW - timedelta(hours=1), phone_number_id="pnid-4")
    cancelled = await seed_world(db, status=AppointmentStatus.CANCELLED, phone_number_id="pnid-5")

    created = await reminder_hooks.reconcile_missing_reminders(now=NOW)

    assert created == 2
    assert len(await _rows(db, missing.appointment.id)) == 2
    assert len(await _rows(db, planned.appointment.id)) == 2
    for world in (switched_off, already_past, cancelled):
        assert await _rows(db, world.appointment.id) == []
    assert await reminder_hooks.reconcile_missing_reminders(now=NOW) == 0
