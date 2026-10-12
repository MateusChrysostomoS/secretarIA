"""The rows behind the clinic's confirm/edit cards are tap targets and nothing else (R7)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.services import reminder_hooks, reminder_opening, reminder_schedule
from secretaria.workers import confirmation_warnings, reminder_engine
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)


async def _ensure(db, world, kind="staff_confirm", *, with_prompt=True, now=NOW):  # noqa: F811
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        row, created = await reminder_schedule.ensure_staff_notice_row(
            session, appointment, kind=kind, channel="whatsapp", with_prompt=with_prompt, now=now
        )
        await session.commit()
        return row, created


async def _rows(db, appointment_id):  # noqa: F811
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder).where(
                    AppointmentReminder.appointment_id == appointment_id
                )
            )
        )


async def test_a_staff_row_is_born_sent_without_a_clinic_warning(db):  # noqa: F811
    world = await seed_world(db)

    row, created = await _ensure(db, world)

    assert created is True
    assert (row.kind, row.status, row.channel, row.with_prompt) == (
        "staff_confirm",
        "sent",
        "whatsapp",
        True,
    )
    assert row.warn_due_at is None and row.warned_at is None and row.invalidated_at is None
    assert row.patient_id == world.patient.id


async def test_the_same_start_reuses_the_row_and_keeps_the_answer(db):  # noqa: F811
    world = await seed_world(db)
    first, _ = await _ensure(db, world)
    async with db() as session:
        stored = await session.get(AppointmentReminder, first.id)
        stored.answer = "confirm"
        await session.commit()

    again, created = await _ensure(db, world, now=NOW + timedelta(minutes=5), with_prompt=False)

    assert created is False and again.id == first.id
    assert again.answer == "confirm" and again.with_prompt is False
    assert len(await _rows(db, world.appointment.id)) == 1


async def test_a_new_start_or_a_retired_row_gets_a_fresh_row(db):  # noqa: F811
    world = await seed_world(db)
    first, _ = await _ensure(db, world)
    async with db() as session:
        await reminder_schedule.retire_staff_notice_row(
            session, first.id, tenant_id=world.tenant.id
        )
        await session.commit()

    second, created = await _ensure(db, world)
    assert created is True and second.id != first.id

    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.start_at = world.start_at + timedelta(days=1)
        await session.commit()
    third, created = await _ensure(db, world)
    assert created is True and third.id not in {first.id, second.id}


async def test_kinds_are_validated_and_independent(db):  # noqa: F811
    world = await seed_world(db)
    confirm, _ = await _ensure(db, world, "staff_confirm")
    edit, _ = await _ensure(db, world, "staff_edit")
    assert confirm.id != edit.id
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        with pytest.raises(ValueError):
            await reminder_schedule.current_staff_notice_row(session, appointment, kind="day")


async def test_the_warning_cron_never_selects_a_staff_row(db):  # noqa: F811
    world = await seed_world(db)
    await _ensure(db, world)

    async with db() as session:
        found = await confirmation_warnings.due_candidates(session, NOW + timedelta(days=2))

    assert found == []


async def test_the_engine_never_picks_a_staff_row(db):  # noqa: F811
    world = await seed_world(db)
    await _ensure(db, world)

    assert await reminder_engine.due_reminder_ids(NOW + timedelta(days=2), limit=50) == []


async def test_the_reconcile_still_plans_an_appointment_that_only_has_a_staff_row(db):  # noqa: F811
    world = await seed_world(db)
    await _ensure(db, world)

    created = await reminder_hooks.reconcile_missing_reminders(now=NOW)

    kinds = {row.kind for row in await _rows(db, world.appointment.id)}
    assert created >= 2 and {"staff_confirm", "day", "hour"} <= kinds


async def test_a_staff_row_never_turns_the_agenda_red_nor_blocks_the_opening(db):  # noqa: F811
    world = await seed_world(db)
    row, _ = await _ensure(db, world)

    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        tenant = await session.get(Tenant, world.tenant.id)
        assert reminder_schedule.display_state(appointment, [row]) == "unconfirmed"
        decision = await reminder_opening.decide_reminder_opening(
            session,
            tenant=tenant,
            patient_id=world.patient.id,
            last_activity_at=NOW - timedelta(hours=7),
            now=NOW,
        )
    assert decision.reason == reminder_opening.OPEN
