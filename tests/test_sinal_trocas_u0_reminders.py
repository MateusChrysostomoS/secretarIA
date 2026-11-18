"""TASK-085 R / U0 - the `price_change` reminder row is a link target, nothing else (mapa-u §2.1).

D-R14: the e-mail that tells a patient the clinic changed a price links to the Portal through an
`appointment_reminders` row of kind `price_change` (born `sent`, e-mail channel, no prompt, no
clinic warning). Like the `chat` and `staff_*` rows it is "shown, never planned": the reminder
engine, the confirmation-warning cron, the reconcile backfill and the agenda colour must all
behave exactly as if it were not there.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_EMAIL,
    REMINDER_KIND_PRICE_CHANGE,
    REMINDER_STATUS_SENT,
    REMINDER_WARN_UNCONFIRMED,
)
from secretaria.services import reminder_hooks, reminder_opening, reminder_schedule
from secretaria.workers import confirmation_warnings, reminder_engine
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)


async def _price_row(db, world, **overrides) -> AppointmentReminder:  # noqa: F811
    """The row as UB1's sweep creates it (mapa-u §2.1)."""
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        fields = dict(
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            kind=REMINDER_KIND_PRICE_CHANGE,
            appointment_start_at=appointment.start_at,
            due_at=NOW,
            status=REMINDER_STATUS_SENT,
            channel=REMINDER_CHANNEL_EMAIL,
            with_prompt=False,
            sent_at=NOW,
            warn_due_at=None,
        )
        fields.update(overrides)
        row = AppointmentReminder(**fields)
        session.add(row)
        await session.commit()
        return row


async def _kinds(db, appointment_id) -> set[str]:  # noqa: F811
    async with db() as session:
        return set(
            await session.scalars(
                select(AppointmentReminder.kind).where(
                    AppointmentReminder.appointment_id == appointment_id
                )
            )
        )


async def test_the_engine_never_picks_a_price_change_row(db):  # noqa: F811
    world = await seed_world(db)
    await _price_row(db, world)

    assert await reminder_engine.due_reminder_ids(NOW + timedelta(days=2), limit=50) == []


async def test_the_warning_cron_never_selects_a_price_change_row(db):  # noqa: F811
    world = await seed_world(db)
    # Even a row that (wrongly) carried a due warning is never warned about: the kind is excluded.
    await _price_row(
        db, world, warn_due_at=NOW, warn_kind=REMINDER_WARN_UNCONFIRMED
    )

    async with db() as session:
        found = await confirmation_warnings.due_candidates(session, NOW + timedelta(days=2))

    assert found == []


async def test_the_reconcile_still_plans_an_appointment_that_only_has_a_price_row(db):  # noqa: F811
    world = await seed_world(db)
    await _price_row(db, world)

    created = await reminder_hooks.reconcile_missing_reminders(now=NOW)

    kinds = await _kinds(db, world.appointment.id)
    assert created >= 2 and {REMINDER_KIND_PRICE_CHANGE, "day", "hour"} <= kinds


async def test_scheduling_reminders_ignores_the_price_row(db):  # noqa: F811
    world = await seed_world(db)
    await _price_row(db, world)

    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        tenant = await session.get(Tenant, world.tenant.id)
        created = await reminder_schedule.schedule_reminders(
            session, appointment, tenant, now=NOW
        )
        await session.commit()

    assert {row.kind for row in created} >= {"day", "hour"}
    assert REMINDER_KIND_PRICE_CHANGE not in {row.kind for row in created}


async def test_a_price_row_never_turns_the_agenda_red_nor_blocks_the_opening(db):  # noqa: F811
    world = await seed_world(db)
    row = await _price_row(db, world)

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
