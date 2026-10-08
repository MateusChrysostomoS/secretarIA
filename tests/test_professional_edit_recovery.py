"""A committed patient edit must survive a lost enqueue or a crashed mail job."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from secretaria.models import FlowState
from secretaria.services import flow_router as fr
from secretaria.workers import tasks
from secretaria.workers.shared import appointment_edit_notification as notice
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import wire
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world
from tests.test_appointment_edit_apply import _edit, _reply
from tests.test_professional_edit_notification import _confirmed, _deliver, transport  # noqa: F401
from tests.test_professional_notification import _FakeRedis, _Mailer, _summary


async def test_failed_enqueue_is_recovered_from_committed_edit(db, monkeypatch):  # noqa: F811
    wire(monkeypatch, db)
    world = await seed_world(
        db,
        start_at=datetime(2030, 10, 15, 18, 20, tzinfo=UTC),
        professional_name="QA doctor",
        google_event_id="evt-old",
    )

    class BrokenQueue:
        async def enqueue_job(self, *args, **kwargs):
            raise ConnectionError("local test queue offline")

    edit = _edit(
        world,
        professional_id=world.appointment.professional_id,
        old_professional_id=world.appointment.professional_id,
    )
    result = fr.FlowRouterResult(action="reply", flow_state=FlowState.MENU, appointment_edit=edit)
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, redis=BrokenQueue(), waba_token="t"
    )
    saved = await reload_appointment(db, world.appointment.id)
    assert saved.start_at.date() != world.start_at.date()
    redis, sender = _FakeRedis(), _Mailer()
    monkeypatch.setattr(notice, "get_entitlements", AsyncMock(return_value=_summary()))
    monkeypatch.setattr(
        notice,
        "fetch_professional_emails",
        AsyncMock(
            return_value={str(saved.professional_id): "qa-doctor@example.test"},
        ),
    )
    monkeypatch.setattr(notice, "send_transactional_email_result", sender)
    await notice.dispatch_pending_professional_edits({"redis": redis})
    job = next(j for j in redis.jobs if j[0] == notice.JOB_NAME)
    await notice.send_professional_edit_notification({"redis": redis}, *job[1])
    await notice.send_professional_edit_notification({"redis": redis}, *job[1])
    assert len(sender.calls) == 1


async def test_legacy_confirmed_reschedule_also_queues_a_professional_notice(db, monkeypatch):  # noqa: F811
    wire(monkeypatch, db)
    world = await seed_world(
        db,
        start_at=datetime(2030, 10, 15, 18, 20, tzinfo=UTC),
        professional_name="QA doctor",
        google_event_id="evt-old",
    )
    redis = _FakeRedis()
    result = fr.FlowRouterResult(
        action="reply",
        flow_state=FlowState.MENU,
        appointment_reschedule={
            "google_event_id": "evt-old",
            "start_at": world.start_at + timedelta(days=1),
            "end_at": world.appointment.end_at + timedelta(days=1),
        },
    )
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, redis=redis, waba_token="t"
    )
    jobs = [j for j in redis.jobs if j[0] == notice.JOB_NAME]
    assert len(jobs) == 1 and jobs[0][1][-1] == ["data"]


async def test_a_b_a_suppresses_obsolete_occurrence_even_when_content_returns(
    db,  # noqa: F811
    transport,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    sender, redis = transport
    world, first = await _confirmed(db, transport, monkeypatch)
    for start, end in (
        (world.start_at, world.appointment.end_at),
        (datetime(2030, 10, 16, 12, tzinfo=UTC), datetime(2030, 10, 16, 12, 40, tzinfo=UTC)),
    ):
        edit = _edit(
            world,
            start_at=start,
            end_at=end,
            professional_id=world.appointment.professional_id,
            old_professional_id=world.appointment.professional_id,
        )
        await tasks._apply_flow_result(
            _reply(world),
            fr.FlowRouterResult(action="reply", flow_state=FlowState.MENU, appointment_edit=edit),
            WA_ID,
            tenant=world.tenant,
            redis=redis,
            waba_token="t",
        )
    await _deliver(first, redis)
    assert not sender.calls
    await _deliver(redis.jobs[-1], redis)
    assert len(sender.calls) == 1


async def test_crashed_sender_remains_recoverable_and_dispatch_is_leased(
    db,  # noqa: F811
    transport,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    sender, redis = transport
    world, job = await _confirmed(db, transport, monkeypatch)
    crashing = AsyncMock(side_effect=ConnectionError("local crash before acceptance"))
    monkeypatch.setattr(notice, "send_transactional_email_result", crashing)
    import pytest

    with pytest.raises(ConnectionError):
        await _deliver(job, redis)
    redis.jobs.clear()
    await notice.dispatch_pending_professional_edits({"redis": redis})
    await notice.dispatch_pending_professional_edits({"redis": redis})
    assert len(redis.jobs) == 1
    monkeypatch.setattr(notice, "send_transactional_email_result", sender)
    await _deliver(redis.jobs[0], redis)
    assert len(sender.calls) == 1
