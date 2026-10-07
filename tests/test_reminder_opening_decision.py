"""When the chat opens with the appointment reminder (TASK-032 R3, spec §4.3)."""

from datetime import timedelta

import pytest
from sqlalchemy import select, update

from secretaria.core import database as core_database
from secretaria.models import AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services import reminder_hooks, reminder_opening as ro
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import add_appointment
from tests._reminders_v2 import NOW, add_reminder, seed_world

QUIET = NOW - timedelta(hours=7)


async def _decide(db, world, *, last_activity_at=QUIET, now=NOW):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        return await ro.decide_reminder_opening(
            session,
            tenant=tenant,
            patient_id=world.patient.id,
            last_activity_at=last_activity_at,
            now=now,
        )


async def _row(db, world, **fields):  # noqa: F811
    """A reminder row of the CURRENT version, with any fields overridden."""
    kind = fields.pop("kind", "chat")
    start = fields.pop("appointment_start_at", None)
    rid = await add_reminder(db, world, kind=kind, appointment_start_at=start)
    if fields:
        async with db() as session:
            await session.execute(
                update(AppointmentReminder).where(AppointmentReminder.id == rid).values(**fields)
            )
            await session.commit()
    return rid


async def test_a_quiet_patient_with_a_live_appointment_opens_with_it(db):  # noqa: F811
    world = await seed_world(db)
    decision = await _decide(db, world)
    assert decision == ro.OpeningDecision(ro.OPEN, world.appointment.id)


async def test_the_switch_off_opens_nothing(db):  # noqa: F811
    world = await seed_world(db, v2=False)
    assert (await _decide(db, world)).reason == ro.SKIP_SWITCH_OFF


async def test_no_history_or_recent_activity_opens_nothing(db):  # noqa: F811
    world = await seed_world(db)
    assert (await _decide(db, world, last_activity_at=None)).reason == ro.SKIP_NO_HISTORY
    recent = NOW - timedelta(hours=1)
    assert (await _decide(db, world, last_activity_at=recent)).reason == ro.SKIP_RECENT_ACTIVITY


async def test_the_nearest_of_two_appointments_opens_the_chat(db):  # noqa: F811
    world = await seed_world(db, start_at=NOW + timedelta(days=5))
    nearest = await add_appointment(db, world, start_at=NOW + timedelta(days=1))
    assert (await _decide(db, world)).appointment_id == nearest.id


@pytest.mark.parametrize(
    "start_offset, status",
    [
        (timedelta(hours=-2), AppointmentStatus.SCHEDULED),
        (timedelta(days=2), AppointmentStatus.CANCELLED),
        (timedelta(days=2), AppointmentStatus.ATTENDED),
    ],
)
async def test_a_past_or_cancelled_appointment_never_opens_the_chat(
    db,  # noqa: F811
    start_offset,
    status,
):
    world = await seed_world(db, start_at=NOW + start_offset, status=status)
    assert (await _decide(db, world)).reason == ro.SKIP_NO_UPCOMING


async def test_confirmed_twice_never_asks_again(db):  # noqa: F811
    world = await seed_world(db, confirmation_count=2)
    assert (await _decide(db, world)).reason == ro.SKIP_CONFIRMED_TWICE


async def test_a_chat_card_already_confirmed_is_not_shown_again(db):  # noqa: F811
    world = await seed_world(db)
    await _row(db, world, status="sent", sent_at=NOW - timedelta(days=1), answer="confirm")
    assert (await _decide(db, world)).reason == ro.SKIP_CHAT_ALREADY_CONFIRMED


async def test_a_chat_row_shown_inside_the_gap_is_not_shown_again(db):  # noqa: F811
    world = await seed_world(db)
    await _row(db, world, status="sent", sent_at=NOW - timedelta(hours=1))
    assert (await _decide(db, world)).reason == ro.SKIP_DUPLICATE_OPENING


async def test_an_unanswered_cron_card_that_is_the_last_activity_is_not_repeated(db):  # noqa: F811
    world = await seed_world(db)
    await _row(db, world, kind="day", status="sent", sent_at=QUIET, warn_kind="unconfirmed")
    assert (await _decide(db, world)).reason == ro.SKIP_REMINDER_WAITING


async def test_an_answered_or_older_cron_card_does_not_block_the_opening(db):  # noqa: F811
    world = await seed_world(db)
    await _row(
        db, world, kind="day", status="sent", sent_at=QUIET, warn_kind="unconfirmed", answer="other"
    )
    await _row(
        db,
        world,
        kind="hour",
        status="sent",
        sent_at=QUIET - timedelta(days=2),
        warn_kind="unconfirmed",
    )
    assert (await _decide(db, world)).reason == ro.OPEN


async def test_rows_of_an_older_version_never_block(db):  # noqa: F811
    world = await seed_world(db)
    await _row(
        db,
        world,
        appointment_start_at=world.start_at - timedelta(days=1),
        status="sent",
        sent_at=NOW - timedelta(minutes=5),
        answer="confirm",
    )
    assert (await _decide(db, world)).reason == ro.OPEN


async def test_ensure_chat_reminder_reuses_the_one_row_of_this_version(db):  # noqa: F811
    world = await seed_world(db)
    ids = []
    for now in (NOW, NOW + timedelta(hours=8)):
        async with db() as session:
            appointment = await ro.nearest_live_appointment(
                session, world.tenant.id, world.patient.id, now=NOW
            )
            row = await ro.ensure_chat_reminder(session, appointment, now=now)
            ids.append(row.id)
            await session.commit()

    async with db() as session:
        rows = list(await session.scalars(select(AppointmentReminder)))
    assert ids[0] == ids[1] and len(rows) == 1
    [row] = rows
    assert (row.kind, row.status, row.channel, row.with_prompt) == ("chat", "sent", "chat", True)
    assert row.warn_due_at is None
    assert ro.as_utc(row.sent_at) == NOW + timedelta(hours=8)


async def test_the_reconcile_still_plans_an_appointment_that_only_has_a_chat_row(
    db,  # noqa: F811
    monkeypatch,
):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    world = await seed_world(db)
    await _row(db, world, status="sent", sent_at=NOW - timedelta(hours=1))

    created = await reminder_hooks.reconcile_missing_reminders(now=NOW)

    async with db() as session:
        kinds = set(
            await session.scalars(
                select(AppointmentReminder.kind).where(
                    AppointmentReminder.appointment_id == world.appointment.id
                )
            )
        )
    assert created >= 2
    assert {"chat", "day", "hour"} <= kinds
