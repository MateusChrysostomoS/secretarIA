"""workers/reminder_engine.py — claim, guards, outcomes (TASK-032 R2, spec §4.2)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import AppointmentReminder, AppointmentStatus, Conversation
from secretaria.services import reminder_delivery
from secretaria.services.brain_patients import PatientEmailResult
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
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
    outbound_messages,
    seed_paid_deposit,
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


# --------------------------------------------------------------------------
# Outcomes (Task 8)
# --------------------------------------------------------------------------


@pytest.fixture
def usage(monkeypatch):
    calls: list[dict] = []

    async def _emit(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(reminder_engine, "emit_usage_event", _emit)
    return calls


async def test_a_failing_send_is_retried_then_marked_delivery_failed(db, usage):  # noqa: F811
    FakeWhatsAppClient.fail_everything = True
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    for attempt in (1, 2, 3):
        report = await _tick(NOW + timedelta(minutes=attempt))
        row = await get_reminder(db, rid)
        assert report.retried == 1
        assert (row.status, row.attempts, row.last_error_code) == (
            "pending",
            attempt,
            "RuntimeError",
        )
        assert row.warn_kind is None

    final = NOW + timedelta(minutes=4)
    report = await _tick(final)

    row = await get_reminder(db, rid)
    assert report.failed == 1
    assert (row.status, row.attempts, row.warn_kind) == ("failed", 4, "delivery_failed")
    assert _utc(row.warn_due_at) == final
    assert usage == []
    assert await outbound_messages(db, world.conversation.id) == []


async def test_a_send_that_recovers_on_the_second_attempt_is_sent(db):  # noqa: F811
    FakeWhatsAppClient.fail_everything = True
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)
    await _tick()
    FakeWhatsAppClient.fail_everything = False

    await _tick(NOW + timedelta(minutes=1))

    row = await get_reminder(db, rid)
    assert (row.status, row.attempts, row.last_error_code) == ("sent", 2, None)


async def test_a_patient_without_any_channel_fails_at_once_and_warns(db):  # noqa: F811
    world = await seed_world(db, wa_id=None)
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.attempts, row.last_error_code, row.warn_kind) == (
        "failed",
        1,
        "no_channel",
        "delivery_failed",
    )


async def test_a_sent_prompt_keeps_the_clinic_warning_armed(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)
    planned_warning = _utc((await get_reminder(db, rid)).warn_due_at)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.with_prompt, row.warn_kind) == ("sent", True, "unconfirmed")
    assert _utc(row.warn_due_at) == planned_warning


async def test_after_two_confirmations_the_reminder_has_no_buttons_and_warns_nobody(db):  # noqa: F811
    world = await seed_world(db, confirmation_count=2, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world, with_prompt=True)  # planned before the 2nd confirmation

    await _tick()

    [(kind, _to, body)] = FakeWhatsAppClient.all_sent()
    assert kind == "text" and body.startswith("LEMBRE-SE:")
    row = await get_reminder(db, rid)
    assert (row.status, row.with_prompt, row.warn_kind, row.warn_due_at) == (
        "sent",
        False,
        None,
        None,
    )


async def test_a_whatsapp_reminder_is_recorded_in_the_conversation(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    await add_reminder(db, world)

    await _tick()

    [row] = await outbound_messages(db, world.conversation.id)
    assert row.wam_id == "wamid.buttons"
    assert row.body.endswith("(opções: Confirmar, Cancelar, Outro)")
    assert row.interactive["kind"] == "buttons"
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.last_bot_message_at is not None


async def test_only_a_template_send_is_metered(db, usage):  # noqa: F811
    outside = await seed_world(db)  # never wrote: template
    inside = await seed_world(
        db, phone_number_id="pnid-2", last_inbound_at=NOW - timedelta(hours=1)
    )
    rid_outside = await add_reminder(db, outside)
    await add_reminder(db, inside)

    await _tick()

    assert usage == [
        {
            "tenant_id": str(outside.tenant.id),
            "feature": "reminders",
            "amount": 1,
            "event_id": f"reminder:v2:{rid_outside}",
        }
    ]


async def test_a_metering_failure_never_unsends_the_reminder(db, monkeypatch):  # noqa: F811
    async def _boom(**kwargs):
        raise RuntimeError("brain-api unreachable")

    monkeypatch.setattr(reminder_engine, "emit_usage_event", _boom)
    world = await seed_world(db)
    rid = await add_reminder(db, world)

    await _tick()

    assert (await get_reminder(db, rid)).status == "sent"


async def test_a_row_cancelled_while_in_flight_stays_cancelled(db, monkeypatch):  # noqa: F811
    """R1's cancel_reminders also cancels 'sending' rows; finishing must not resurrect it."""
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)
    real_deliver = reminder_engine.deliver_reminder

    async def _deliver_while_cancelled(job):
        await _set(db, rid, status="cancelled", warn_due_at=None)
        return await real_deliver(job)

    monkeypatch.setattr(reminder_engine, "deliver_reminder", _deliver_while_cancelled)

    report = await _tick()

    row = await get_reminder(db, rid)
    assert report.closed == 1
    assert (row.status, row.sent_at, row.warn_kind) == ("cancelled", None, None)
    assert len(await outbound_messages(db, world.conversation.id)) == 1  # it did reach the patient


async def test_a_portal_patient_is_reminded_by_chat_and_email(db, monkeypatch):  # noqa: F811
    async def _fetch(tenant_id, external_id):
        return PatientEmailResult(available=True, email="maria@example.com")

    async def _send(to, template, variables):
        return EmailOutcome.SENT

    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _fetch)
    monkeypatch.setattr(reminder_delivery, "send_transactional_email_result", _send)
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    rid = await add_reminder(db, world)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.channel) == ("sent", "email")
    assert FakeWhatsAppClient.created == []
    assert len(await outbound_messages(db, world.conversation.id)) == 1  # written by the sender


async def test_a_pix_paid_appointment_keeps_its_three_button_variant(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world)

    await _tick()

    [(_kind, _to, _body, buttons)] = FakeWhatsAppClient.all_sent()
    assert [label for _, label in buttons] == ["Confirmar", "Reagendar", "Cancelar"]
    assert buttons[0][0] == f"remconfirm|{rid}"
    assert buttons[1][0] == f"apptresched|{world.appointment.id}"


async def test_rows_are_never_read_across_clinics(db):  # noqa: F811
    off = await seed_world(db, v2=False, last_inbound_at=NOW - timedelta(hours=1))
    on = await seed_world(db, phone_number_id="pnid-2", last_inbound_at=NOW - timedelta(hours=1))
    rid_off = await add_reminder(db, off)
    await add_reminder(db, on)

    await _tick()

    async with db() as session:
        statuses = dict(
            (
                await session.execute(select(AppointmentReminder.id, AppointmentReminder.status))
            ).all()
        )
    assert statuses[rid_off] == "pending"
    assert sorted(statuses.values()) == ["pending", "sent"]


# ---- delivered but the bookkeeping failed: never a duplicate (controller ruling) ----


async def test_bookkeeping_failure_after_an_ok_send_never_resends(db, monkeypatch, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db blip")

    monkeypatch.setattr(reminder_engine, "_finish", _boom)

    first = await _tick()
    second = await _tick(NOW + timedelta(minutes=1))

    assert len(FakeWhatsAppClient.all_sent()) == 1
    row = await get_reminder(db, rid)
    assert (row.status, row.attempts) == ("sent", 1)
    assert _utc(row.sent_at) == NOW
    assert (row.warn_kind, row.last_error_code) == ("unconfirmed", None)
    assert (first.sent, second.claimed) == (1, 0)


async def test_when_even_the_rescue_fails_the_row_stays_sending_not_pending(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    async def _boom(*args, **kwargs):
        raise RuntimeError("db down")

    def _boom_sync(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(reminder_engine, "_finish", _boom)
    monkeypatch.setattr(reminder_engine, "_mark_sent", _boom_sync)

    await _tick()
    await _tick(NOW + timedelta(minutes=1))

    assert len(FakeWhatsAppClient.all_sent()) == 1
    assert (await get_reminder(db, rid)).status == "sending"


async def test_a_crash_before_the_send_still_goes_back_to_pending(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)

    async def _boom(job):
        raise RuntimeError("before send")

    monkeypatch.setattr(reminder_engine, "deliver_reminder", _boom)

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("pending", "engine_error")
    assert FakeWhatsAppClient.all_sent() == []


# --------------------------------------------------------------------------
# Cron entry points (Task 12)
# --------------------------------------------------------------------------


async def test_the_minute_cron_sends_what_is_due_now(db):  # noqa: F811
    now = datetime.now(UTC)
    world = await seed_world(
        db, start_at=now + timedelta(days=2), last_inbound_at=now - timedelta(hours=1)
    )
    rid = await add_reminder(db, world, due_at=now - timedelta(minutes=1))

    await reminder_engine.process_appointment_reminders({"redis": None})

    assert (await get_reminder(db, rid)).status == "sent"


async def test_the_reconcile_cron_plans_missing_rows(db):  # noqa: F811
    now = datetime.now(UTC)
    world = await seed_world(db, start_at=now + timedelta(days=3))

    await reminder_engine.reconcile_appointment_reminders({})

    async with db() as session:
        kinds = sorted(
            (
                await session.scalars(
                    select(AppointmentReminder.kind).where(
                        AppointmentReminder.appointment_id == world.appointment.id
                    )
                )
            ).all()
        )
    assert kinds == ["day", "hour"]


# ---- review fixes: chat copy from data, late-send warning, per-row clock ----


@pytest.fixture
def portal_mail(monkeypatch):
    state = {"outcome": EmailOutcome.SENT}

    async def _fetch(tenant_id, external_id):
        return PatientEmailResult(available=True, email="maria@example.com")

    async def _send(to, template, variables):
        return state["outcome"]

    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _fetch)
    monkeypatch.setattr(reminder_delivery, "send_transactional_email_result", _send)
    return state


async def test_a_portal_first_attempt_that_died_before_the_card_still_writes_it_on_retry(
    db,  # noqa: F811
    monkeypatch,
    portal_mail,
):
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    rid = await add_reminder(db, world)
    real_deliver = reminder_engine.deliver_reminder
    calls = {"n": 0}

    async def _crash_once(job):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("worker blip before the sender")
        return await real_deliver(job)

    monkeypatch.setattr(reminder_engine, "deliver_reminder", _crash_once)

    await _tick()
    assert (await get_reminder(db, rid)).status == "pending"
    assert await outbound_messages(db, world.conversation.id) == []

    await _tick(NOW + timedelta(minutes=1))

    row = await get_reminder(db, rid)
    assert (row.status, row.attempts) == ("sent", 2)
    [card] = await outbound_messages(db, world.conversation.id)
    assert card.interactive["options"][0]["id"] == f"remconfirm|{rid}"


@pytest.mark.parametrize("with_prompt", [True, False])
async def test_a_portal_retry_after_the_card_was_written_never_writes_a_second_one(
    db,  # noqa: F811
    portal_mail,
    with_prompt,
):
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    rid = await add_reminder(db, world, with_prompt=with_prompt)
    portal_mail["outcome"] = EmailOutcome.SEND_FAILED  # card written, e-mail fails -> retry

    await _tick()
    assert (await get_reminder(db, rid)).status == "pending"
    portal_mail["outcome"] = EmailOutcome.SENT
    await _tick(NOW + timedelta(minutes=1))

    row = await get_reminder(db, rid)
    assert (row.status, row.attempts) == ("sent", 2)
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_an_on_time_send_leaves_the_warning_deadline_as_r1_wrote_it(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world)  # due 1 min ago, warn = due + 2 h
    planned = _utc((await get_reminder(db, rid)).warn_due_at)

    await _tick()

    assert _utc((await get_reminder(db, rid)).warn_due_at) == planned


async def test_a_late_send_moves_the_warning_deadline_to_send_time_plus_the_r1_delay(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW + timedelta(hours=1))
    due = NOW - timedelta(hours=2)
    rid = await add_reminder(db, world, kind="day", due_at=due)  # warn = due + 2 h = NOW
    assert _utc((await get_reminder(db, rid)).warn_due_at) == NOW

    await _tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.warn_kind) == ("sent", "unconfirmed")
    assert _utc(row.warn_due_at) == NOW + timedelta(hours=2)


async def test_each_row_is_judged_against_the_clock_at_that_moment(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    rid = await add_reminder(db, world, kind="hour")  # may be at most 30 min late
    readings = iter([NOW, NOW + timedelta(hours=1)])  # tick start, then the row's turn
    monkeypatch.setattr(reminder_engine, "_utcnow", lambda: next(readings))

    report = await reminder_engine.run_reminder_tick()

    row = await get_reminder(db, rid)
    assert (row.status, row.last_error_code) == ("skipped", "too_late")
    assert report.closed == 1 and FakeWhatsAppClient.all_sent() == []
