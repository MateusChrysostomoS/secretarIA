"""Which rows may be warned about, and the atomic claim (TASK-032 R4, spec 4.4)."""

from datetime import timedelta

import pytest
from sqlalchemy import update

from secretaria.models import Appointment, AppointmentReminder, AppointmentStatus
from secretaria.workers import confirmation_warnings as cw
from tests._confirmation_warnings import add_warnable_row
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, get_reminder, seed_world


async def _candidates(db, now=NOW):  # noqa: F811
    async with db() as session:
        return await cw.due_candidates(session, now)


async def _claim(db, candidate, now=NOW) -> bool:  # noqa: F811
    async with db() as session:
        async with session.begin():
            return await cw.claim_warning(session, candidate, now)


async def _set_appointment(db, appointment_id, **fields):  # noqa: F811
    async with db() as session:
        await session.execute(
            update(Appointment).where(Appointment.id == appointment_id).values(**fields)
        )
        await session.commit()


async def test_a_sent_unanswered_row_past_its_deadline_is_a_candidate(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)

    found = await _candidates(db)

    assert [c.reminder_id for c in found] == [rid]
    assert found[0].warn_kind == "unconfirmed" and found[0].kind == "day"
    assert found[0].appointment_id == world.appointment.id


async def test_the_failed_row_is_a_candidate_too(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, status="failed", warn_kind="delivery_failed")

    found = await _candidates(db)

    assert [c.warn_kind for c in found] == ["delivery_failed"]


@pytest.mark.parametrize("status", ["pending", "sending", "skipped", "cancelled"])
async def test_a_row_that_was_never_sent_is_not_a_candidate(db, status):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, status=status)

    assert await _candidates(db) == []


async def test_the_deadline_must_have_passed_and_exist(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, kind="day", warn_due_at=NOW + timedelta(minutes=5))
    await add_warnable_row(db, world, kind="hour", warn_due_at=None, warn_kind=None)

    assert await _candidates(db) == []


async def test_an_invalidated_row_is_neither_selected_nor_claimable(db):  # noqa: F811
    world = await seed_world(db)
    stale = await add_warnable_row(db, world, invalidated_at=NOW - timedelta(hours=1))
    assert await _candidates(db) == []

    live = await add_warnable_row(db, world, kind="morning")
    [candidate] = await _candidates(db)
    assert candidate.reminder_id == live
    async with db() as session:
        await session.execute(
            update(AppointmentReminder)
            .where(AppointmentReminder.id == live)
            .values(invalidated_at=NOW)
        )
        await session.commit()

    assert await _claim(db, candidate) is False
    assert (await get_reminder(db, live)).warned_at is None
    assert (await get_reminder(db, stale)).warned_at is None


async def test_the_chat_opening_row_is_never_warned_about(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, kind="chat")

    assert await _candidates(db) == []


async def test_a_row_of_an_older_start_is_not_a_candidate(db):  # noqa: F811
    world = await seed_world(db)
    await add_warnable_row(db, world, appointment_start_at=world.start_at - timedelta(days=1))

    assert await _candidates(db) == []


async def test_a_confirmed_appointment_is_never_warned_about(db):  # noqa: F811
    world = await seed_world(db, confirmation_count=1)
    await add_warnable_row(db, world)

    assert await _candidates(db) == []


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW],
)
async def test_a_closed_appointment_is_never_warned_about(db, status):  # noqa: F811
    world = await seed_world(db, status=status)
    await add_warnable_row(db, world)

    assert await _candidates(db) == []


async def test_a_started_appointment_is_never_warned_about(db):  # noqa: F811
    world = await seed_world(db, start_at=NOW - timedelta(minutes=10))
    await add_warnable_row(db, world)

    assert await _candidates(db) == []


async def test_a_clinic_with_the_switch_off_is_never_warned_about(db):  # noqa: F811
    world = await seed_world(db, v2=False)
    rid = await add_warnable_row(db, world)

    assert await _candidates(db) == []
    assert (await get_reminder(db, rid)).warned_at is None


async def test_a_row_of_another_clinic_pointing_here_is_never_selected(db):  # noqa: F811
    mine = await seed_world(db)
    theirs = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900000002")
    await add_warnable_row(db, mine, tenant_id=theirs.tenant.id)

    assert await _candidates(db) == []


async def test_the_claim_sets_warned_at_once(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)
    [candidate] = await _candidates(db)

    first = await _claim(db, candidate)
    second = await _claim(db, candidate)  # a second worker holding the same candidate

    assert (first, second) == (True, False)
    assert (await get_reminder(db, rid)).warned_at is not None
    assert await _candidates(db) == []


async def test_a_confirmation_between_selection_and_claim_defeats_the_claim(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)
    [candidate] = await _candidates(db)

    await _set_appointment(
        db, world.appointment.id, confirmation_count=1, status=AppointmentStatus.CONFIRMED
    )

    assert await _claim(db, candidate) is False
    assert (await get_reminder(db, rid)).warned_at is None


@pytest.mark.parametrize("status", [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED])
async def test_a_close_between_selection_and_claim_defeats_the_claim(db, status):  # noqa: F811
    world = await seed_world(db)
    rid = await add_warnable_row(db, world)
    [candidate] = await _candidates(db)

    await _set_appointment(db, world.appointment.id, status=status)

    assert await _claim(db, candidate) is False
    assert (await get_reminder(db, rid)).warned_at is None


async def test_the_cap_of_warnings_per_appointment(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(cw, "MAX_WARNINGS_PER_APPOINTMENT", 1)
    world = await seed_world(db)
    await add_warnable_row(db, world, kind="day", warn_due_at=NOW - timedelta(minutes=30))
    await add_warnable_row(db, world, kind="hour", warn_due_at=NOW - timedelta(minutes=1))
    first, second = await _candidates(db)

    assert await _claim(db, first) is True
    assert await _claim(db, second) is False  # the cap, not a race


async def test_the_default_cap_is_three(db):  # noqa: F811
    assert cw.MAX_WARNINGS_PER_APPOINTMENT == 3
