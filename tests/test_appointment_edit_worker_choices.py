"""R6 choices through actual Portal ingress, persisted cards and worker routing."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from secretaria.models import FlowState, Tenant
from secretaria.services import appointment_edit as ae, flow_router as fr
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.workers.portal.inbound import process_brain_message_inbound
from secretaria.workers.shared import appointment_edit_notification as notice
from secretaria.workers.shared.opening import _send_context_opening
from tests._edit_flow_support import EditCalendar
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, get_conversation, wire
from tests._reminders_v2 import outbound_messages, reload_appointment, seed_world
from tests.test_first_message_reminder import _reply
from tests.test_professional_notification import _FakeRedis, _Mailer, _summary


@pytest.fixture
def calendar(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)
    cal = EditCalendar()
    monkeypatch.setattr(workers_ns, "resolve_professional_calendar", AsyncMock(return_value=cal))
    monkeypatch.setattr(fr, "_hold_windows", AsyncMock(return_value=[]))
    agent = AsyncMock(side_effect=AssertionError("offered R6 tap must remain deterministic"))
    monkeypatch.setattr(workers_ns, "run_agent", agent)
    return cal, agent


async def tap(db, world, title, *, redis=None):  # noqa: F811
    cards = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    option = next(
        o for m in reversed(cards) for o in m.interactive["options"] if o["title"] == title
    )
    await process_brain_message_inbound(
        {"redis": redis},
        str(world.tenant.id),
        world.patient.external_id,
        text=option["title"],
        interactive_reply_id=option["id"],
    )


async def edit_world(db):  # noqa: F811
    world = await seed_world(
        db,
        channel=CHANNEL_BRAIN_MESSAGE,
        professional_name="QA doctor",
        start_at=datetime.now(UTC) + timedelta(days=3),
    )
    await consent(db, world)
    assert await _send_context_opening(_reply(world), world.tenant, world.patient.external_id)
    await tap(db, world, "Alterar Dados")
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING
    return world


@pytest.mark.parametrize("field", ["insurance", "service"])
async def test_valid_portal_choice_reaches_summary_without_early_write(db, calendar, field):  # noqa: F811
    _cal, agent = calendar
    world = await edit_world(db)
    if field == "insurance":
        await tap(db, world, ae.LABEL_EDIT_MORE)
        await tap(db, world, ae.LABEL_EDIT_INSURANCE)
        await tap(db, world, "Particular")
    else:
        await tap(db, world, ae.LABEL_EDIT_SERVICE)
        cards = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
        await tap(db, world, cards[-1].interactive["options"][0]["title"])
    saved = await get_conversation(db, world)
    assert saved.flow_step == fr.STEP_EDIT_CONFIRM
    if field == "insurance":
        assert saved.flow_edit_draft["current"]["insurance"] == "Particular"
        assert (await reload_appointment(db, world.appointment.id)).insurance is None
    assert saved.flow_edit_draft["current"]["service"] == "Consulta"
    agent.assert_not_called()


async def test_date_edit_via_portal_notifies_doctor_when_whatsapp_is_off(db, calendar, monkeypatch):  # noqa: F811
    cal, agent = calendar
    world = await edit_world(db)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.is_active = False
        await session.commit()
    sender, redis = _Mailer(), _FakeRedis()
    monkeypatch.setattr(notice, "get_entitlements", AsyncMock(return_value=_summary()))
    monkeypatch.setattr(
        notice,
        "fetch_professional_emails",
        AsyncMock(
            return_value={str(world.appointment.professional_id): "qa-doctor@example.test"},
        ),
    )
    monkeypatch.setattr(notice, "send_transactional_email_result", sender)
    cal._days = [(world.start_at + timedelta(days=1)).replace(tzinfo=None)]
    await tap(db, world, ae.LABEL_EDIT_DATE, redis=redis)
    cards = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    day_option = next(o for o in cards[-1].interactive["options"] if o["id"].startswith("day|"))
    await tap(db, world, day_option["title"], redis=redis)
    await tap(db, world, ae.LABEL_TIME_TOO_NO, redis=redis)
    assert (await get_conversation(db, world)).flow_step == fr.STEP_EDIT_CONFIRM
    assert cal.detail_updates == []
    await tap(db, world, fr.LABEL_CONFIRM, redis=redis)
    assert len(cal.detail_updates) == 1
    job = next(j for j in redis.jobs if j[0] == notice.JOB_NAME)
    assert job[1][-1] == ["data"]
    assert sender.calls == []
    await notice.send_professional_edit_notification({"redis": redis}, *job[1])
    assert len(sender.calls) == 1
    row = await reload_appointment(db, world.appointment.id)
    assert row.start_at.date() != world.appointment.start_at.date()
    assert "mudou para:" in sender.bodies[0]
    agent.assert_not_called()
