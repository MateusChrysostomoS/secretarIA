"""Real locks/JSON/leases on uniquely created local PostgreSQL databases and Redis.

Opt-in: REMEDIATION_POSTGRES_URL=postgresql+asyncpg://postgres@127.0.0.1:15432/remediation.
Never accepts a remote/production DSN. Each test owns a new database, then drops only it.
"""

# Imported test functions are deliberately recollected under this real-DB fixture.
# ruff: noqa: F401

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
from arq.connections import RedisSettings, create_pool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from secretaria.core.database import Base
from secretaria.workers.shared import appointment_edit_notification as notice
from tests.test_appointment_edit_safety import (
    isolated,  # noqa: F401
    test_a_clinic_change_after_calendar_write_wins_and_is_restored as test_pg_clinic_change,
    test_database_failure_compensates_calendar_and_keeps_the_draft as test_pg_compensates,
    test_exhausted_pix_limit_cannot_commit_a_time_change as test_pg_pix_limit,
    test_worker_refuses_another_patient_in_the_same_tenant as test_pg_patient_isolation,
)
from tests.test_professional_edit_notification import (
    _confirmed,
    _deliver,
    test_a_change_during_address_lookup_prevents_a_stale_email as test_pg_lookup_race,
    test_display_data_changed_during_lookup_is_loaded_for_the_fixed_email as test_pg_display,
    test_duplicate_delivery_is_once_but_another_confirmed_edit_can_notify_again as test_pg_edits,
    test_transient_failures_retry_then_send_once as test_pg_transient_retry,
    transport,  # noqa: F401
)
from tests.test_professional_edit_recovery import (
    test_failed_enqueue_is_recovered_from_committed_edit as test_pg_lost_enqueue,
)
from tests.test_reminder_link_entry import (
    test_failed_card_delivery_releases_claim_and_next_entry_recovers as test_pg_entry_recovery,
    test_reminder_entry_ignores_quiet_window_and_reloads_without_duplicate as test_pg_entry,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("REMEDIATION_POSTGRES_URL"),
    reason="requires opt-in disposable local PostgreSQL",
)


@pytest_asyncio.fixture
async def db():
    assert os.environ["REMEDIATION_POSTGRES_URL"] == (
        "postgresql+asyncpg://postgres@127.0.0.1:15432/remediation"
    ), "only the task-owned local PostgreSQL is permitted"
    name = "qa_r6_" + uuid4().hex
    admin = await asyncpg.connect("postgresql://postgres@127.0.0.1:15432/postgres")
    await admin.execute(f'CREATE DATABASE "{name}"')
    engine = create_async_engine(f"postgresql+asyncpg://postgres@127.0.0.1:15432/{name}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()
        assert name.startswith("qa_r6_") and len(name) == 38
        await admin.execute(f'DROP DATABASE "{name}"')
        await admin.close()


async def test_pg_two_mail_jobs_serialize_on_actual_appointment_lock(
    db,
    transport,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    sender, redis = transport
    _world, job = await _confirmed(db, transport, monkeypatch)
    entered, release, second_at_lock = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = notice._load_snapshot

    async def snapshot(*args, **kwargs):
        if kwargs.get("lock") and asyncio.current_task().get_name() == "duplicate":
            second_at_lock.set()
        return await original(*args, **kwargs)

    async def held_sender(**kwargs):
        entered.set()
        await release.wait()
        return await sender(**kwargs)

    monkeypatch.setattr(notice, "_load_snapshot", snapshot)
    monkeypatch.setattr(notice, "send_transactional_email_result", held_sender)
    first = asyncio.create_task(_deliver(job, redis))
    await asyncio.wait_for(entered.wait(), 5)
    second = asyncio.create_task(_deliver(job, redis), name="duplicate")
    try:
        await asyncio.wait_for(second_at_lock.wait(), 5)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(second), 0.1)
    finally:
        release.set()
        await asyncio.gather(first, second)
    assert len(sender.calls) == 1


async def test_pg_dispatch_lease_and_actual_redis_job_ids(
    db,
    transport,  # noqa: F811
    monkeypatch,  # noqa: F811
):
    sender, _fake = transport
    _world, _job = await _confirmed(db, transport, monkeypatch)
    redis = await create_pool(RedisSettings(host="127.0.0.1", port=16379, database=15))
    try:
        await asyncio.gather(
            notice.dispatch_pending_professional_edits({"redis": redis}),
            notice.dispatch_pending_professional_edits({"redis": redis}),
        )
        jobs = await redis.queued_jobs()
        assert len(jobs) == 1
        await notice.send_professional_edit_notification({"redis": redis}, *jobs[0].args)
        await notice.send_professional_edit_notification({"redis": redis}, *jobs[0].args)
        assert len(sender.calls) == 1
    finally:
        # This Redis database belongs only to the named disposable QA container.
        await redis.flushdb()
        await redis.aclose()


async def test_pg_two_entries_and_card_outside_tap_window_remain_usable(db, monkeypatch):
    from datetime import UTC, datetime, timedelta

    from secretaria.models import FlowState
    from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
    from secretaria.workers.portal.inbound import process_brain_message_inbound
    from secretaria.workers.portal.open import process_brain_message_enter
    from tests._reminders_r3 import consent, get_conversation
    from tests._reminders_v2 import add_reminder, outbound_messages, seed_world

    world = await seed_world(
        db,
        channel=CHANNEL_BRAIN_MESSAGE,
        start_at=datetime.now(UTC) + timedelta(days=3),
        last_inbound_at=datetime.now(UTC),
    )
    await consent(db, world)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC))
    context = {"source": "reminder_link", "reminder_id": str(rid)}

    async def enter():
        await process_brain_message_enter(
            {}, str(world.tenant.id), world.patient.external_id, entry_context=context
        )

    await asyncio.gather(enter(), enter())
    assert len(await outbound_messages(db, world.conversation.id)) == 1
    sender = BrainMessageSender(conversation_id=world.conversation.id, session_factory=db)
    for i in range(11):
        await sender.send_buttons(
            world.patient.external_id, "QA history", [(f"qa|{i}", "Continuar")]
        )
    await enter()
    rows = await outbound_messages(db, world.conversation.id)
    option = rows[-1].interactive["options"][-1]
    assert option["title"] == "Alterar Dados"
    await process_brain_message_inbound(
        {},
        str(world.tenant.id),
        world.patient.external_id,
        text=option["title"],
        interactive_reply_id=option["id"],
    )
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING
