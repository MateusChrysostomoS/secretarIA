"""workers/reminder_engine.py — claim, guards, outcomes (TASK-032 R2, spec §4.2)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select  # noqa: F401  (used by the Task 8 tests)

from secretaria.core import database as core_database
from secretaria.models import AppointmentReminder, AppointmentStatus
from secretaria.services import reminder_delivery
from secretaria.services.entitlements_client import EntitlementSummary
from secretaria.workers import reminder_engine
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    NOW,
    FakeWhatsAppClient,
    add_reminder,
    entitled,
    fake_waba_token,
    get_reminder,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(reminder_delivery, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(reminder_engine, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(reminder_engine, "get_entitlements", entitled)
    yield


def _utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt


async def _tick(now: datetime = NOW) -> reminder_engine.TickReport:
    return await reminder_engine.run_reminder_tick(now=now)


async def _set(db, reminder_id, **values) -> None:  # noqa: F811
    async with db() as session:
        row = await session.get(AppointmentReminder, reminder_id)
        for key, value in values.items():
            setattr(row, key, value)
        await session.commit()


# ---- sending once ----------------------------------------------------------


async def test_a_due_row_is_sent_once_and_marked_sent(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    report = await _tick()

    assert (report.claimed, report.sent) == (1, 1)
    [(kind, *_rest)] = FakeWhatsAppClient.all_sent()
    assert kind == "buttons"
    row = await get_reminder(db, rid)
    assert (row.status, row.channel, row.attempts) == ("sent", "whatsapp", 1)
    assert _utc(row.sent_at) == NOW

    await _tick(NOW + timedelta(minutes=1))
    assert len(FakeWhatsAppClient.all_sent()) == 1


async def test_two_claims_on_the_same_row_only_one_wins(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world)

    assert await reminder_engine.claim_reminder(rid) == 1
    assert await reminder_engine.claim_reminder(rid) is None
    assert (await get_reminder(db, rid)).status == "sending"


async def test_a_worker_holding_a_stale_list_cannot_send_twice(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    async def _stale_list(now, *, limit):
        return [(rid, world.tenant.id)]  # what a second worker read before the first claimed

    monkeypatch.setattr(reminder_engine, "due_reminder_ids", _stale_list)

    first = await _tick()
    second = await _tick()

    assert (first.sent, second.claimed, second.sent) == (1, 0, 0)
    assert len(FakeWhatsAppClient.all_sent()) == 1


# ---- what is never touched -------------------------------------------------


async def test_a_clinic_with_the_switch_off_is_never_touched(db):  # noqa: F811
    world = await seed_world(db, v2=False)
    rid = await add_reminder(db, world)

    report = await _tick()

    assert report == reminder_engine.TickReport()
    row = await get_reminder(db, rid)
    assert (row.status, row.attempts) == ("pending", 0)
    assert FakeWhatsAppClient.created == []


async def test_a_row_not_yet_due_waits(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world, due_at=NOW + timedelta(minutes=5))

    assert (await _tick()).claimed == 0
    assert (await get_reminder(db, rid)).status == "pending"


async def test_chat_rows_belong_to_the_chat_and_are_never_sent(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world, kind="chat")

    assert (await _tick()).claimed == 0
    assert (await get_reminder(db, rid)).status == "pending"


async def test_an_inactive_subscription_leaves_the_row_waiting(db, monkeypatch):  # noqa: F811
    async def _inactive(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="canceled",
            active=False,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons={},
            limits={},
        )

    monkeypatch.setattr(reminder_engine, "get_entitlements", _inactive)
    world = await seed_world(db)
    rid = await add_reminder(db, world)

    report = await _tick()

    assert (report.deferred, report.claimed) == (1, 0)
    row = await get_reminder(db, rid)
    assert (row.status, row.attempts) == ("pending", 0)


# ---- guards: closed without sending ----------------------------------------


async def test_a_row_planned_for_the_old_time_is_cancelled_not_sent(db):  # noqa: F811
    """Reschedule between scheduling and sending: the row's version is stale."""
    world = await seed_world(db)
    rid = await add_reminder(db, world, appointment_start_at=world.start_at - timedelta(days=1))

    report = await _tick()

    row = await get_reminder(db, rid)
    assert report.closed == 1
    assert (row.status, row.last_error_code, row.warn_due_at) == (
        "cancelled",
        "stale_version",
        None,
    )
    assert FakeWhatsAppClient.created == []


@pytest.mark.parametrize(
    "status", [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW]
)
async def test_a_terminal_appointment_cancels_the_row(db, status):  # noqa: F811
    world = await seed_world(db, status=status)
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("cancelled", "appointment_closed")
    assert FakeWhatsAppClient.created == []


async def test_a_row_pointing_at_another_clinics_appointment_is_cancelled(db):  # noqa: F811
    world = await seed_world(db)
    other = await seed_world(db, phone_number_id="pnid-2")
    rid = await add_reminder(db, world)
    await _set(db, rid, appointment_id=other.appointment.id)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("cancelled", "appointment_gone")
    assert FakeWhatsAppClient.created == []


async def test_an_hour_reminder_more_than_30_minutes_late_is_skipped(db):  # noqa: F811
    world = await seed_world(db, start_at=NOW + timedelta(minutes=29))
    rid = await add_reminder(db, world, kind="hour", due_at=NOW - timedelta(minutes=31))

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("skipped", "too_late")


async def test_an_appointment_that_already_started_is_not_reminded(db):  # noqa: F811
    world = await seed_world(db, start_at=NOW - timedelta(minutes=5))
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("skipped", "appointment_started")


async def test_an_opted_out_patient_is_skipped_without_building_a_client(db):  # noqa: F811
    world = await seed_world(db, opt_out=True)
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code, row.warn_due_at) == ("skipped", "opt_out", None)
    assert FakeWhatsAppClient.created == []


async def test_a_row_without_patient_fails_and_warns_the_clinic(db):  # noqa: F811
    world = await seed_world(db)
    rid = await add_reminder(db, world)
    await _set(db, rid, patient_id=None)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code, row.warn_kind) == (
        "failed",
        "no_patient",
        "delivery_failed",
    )
    assert _utc(row.warn_due_at) == NOW


# ---- gated rows are retired once too late -----------------------------------


async def test_a_gated_row_past_its_lateness_is_closed_too_late(db, monkeypatch):  # noqa: F811
    async def _inactive(tenant_id, redis):
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="canceled",
            active=False,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons={},
            limits={},
        )

    monkeypatch.setattr(reminder_engine, "get_entitlements", _inactive)
    world = await seed_world(db)
    rid = await add_reminder(db, world, kind="day", due_at=NOW - timedelta(hours=4))

    report = await _tick()

    row = await get_reminder(db, rid)
    assert report.closed == 1
    assert (row.status, row.last_error_code, row.warn_due_at) == ("skipped", "too_late", None)
    assert FakeWhatsAppClient.created == []


# ---- reaper for rows a dead worker left in 'sending' -------------------------


async def _stuck(db, *, v2=True, attempts=1, age_min=11):  # noqa: F811
    world = await seed_world(db, v2=v2)
    rid = await add_reminder(
        db, world, status="sending", attempts=attempts, due_at=NOW + timedelta(hours=1)
    )
    await _set(db, rid, updated_at=NOW - timedelta(minutes=age_min))
    return rid


async def test_a_stuck_sending_row_goes_back_to_pending(db):  # noqa: F811
    rid = await _stuck(db)

    assert await reminder_engine.reclaim_stuck_reminders(NOW) == 1

    row = await get_reminder(db, rid)
    assert (row.status, row.attempts, row.last_error_code) == ("pending", 1, "engine_error")


async def test_a_stuck_row_at_the_attempt_cap_fails_and_warns(db):  # noqa: F811
    rid = await _stuck(db, attempts=4)

    await reminder_engine.reclaim_stuck_reminders(NOW)

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code, row.warn_kind) == (
        "failed",
        "engine_error",
        "delivery_failed",
    )
    assert _utc(row.warn_due_at) == NOW


async def test_a_fresh_sending_row_is_left_alone(db):  # noqa: F811
    rid = await _stuck(db, age_min=1)

    assert await reminder_engine.reclaim_stuck_reminders(NOW) == 0
    assert (await get_reminder(db, rid)).status == "sending"


async def test_a_switch_off_clinics_stuck_row_is_untouched(db):  # noqa: F811
    rid = await _stuck(db, v2=False, age_min=30)

    assert await reminder_engine.reclaim_stuck_reminders(NOW) == 0
    assert (await get_reminder(db, rid)).status == "sending"
