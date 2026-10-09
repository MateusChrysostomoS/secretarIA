"""TASK-041 worker proof: real Portal ingress/cards/database, simulated AI only."""

import json

# Imported shared fixtures intentionally shadow their injection parameters.
# ruff: noqa: F811
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from secretaria.models import Professional, Tenant
from secretaria.services import appointment_edit as ae, flow_router as fr
from secretaria.workers.portal.inbound import process_brain_message_inbound
from secretaria.workers.shared import appointment_edit_notification as notice
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import get_conversation
from tests._reminders_v2 import outbound_messages, reload_appointment
from tests.test_appointment_edit_worker_choices import calendar, edit_world, tap  # noqa: F401
from tests.test_professional_notification import _FakeRedis, _Mailer, _summary


async def say(db, world, text, redis=None):
    await process_brain_message_inbound(
        {"redis": redis}, str(world.tenant.id), world.patient.external_id, text=text
    )


@pytest.mark.parametrize(
    "answer",
    [
        "__MANAGE_APPOINTMENT__:reschedule",
        "__BOOKING_DRAFT__:{}",
        "__SHOW_MAIN_MENU__",
        "__APPOINTMENT_EDIT__:{broken",
        "Podemos cancelar a consulta e agendar outra.",
        "__CALENDAR_UNAVAILABLE__",
        (
            "Desculpe, tive uma instabilidade rápida aqui 🙏. Pode me repetir sua "
            "última mensagem? Já te respondo."
        ),
    ],
)
async def test_i2_i3_worker_rejects_escaping_or_invalid_ai_and_redraws_current_stage(
    db,
    calendar,
    monkeypatch,
    answer,
):
    world = await edit_world(db)
    await tap(db, world, ae.LABEL_EDIT_DATE)
    before = await get_conversation(db, world)
    original = before.flow_edit_draft
    baseline = len(await outbound_messages(db, world.conversation.id))
    monkeypatch.setattr(workers_ns, "run_agent", AsyncMock(return_value=answer))
    await say(db, world, "na verdade, quero mudar outra coisa")
    after = await get_conversation(db, world)
    assert after.flow_state.value == "EDIT_BOOKING"
    assert after.flow_step == before.flow_step
    assert after.flow_edit_draft["current"] == original["current"]
    assert after.flow_edit_draft["original"] == original["original"]
    sent = (await outbound_messages(db, world.conversation.id))[baseline:]
    assert sent and sent[-1].interactive
    assert all("cancelar a consulta" not in m.body.casefold() for m in sent)


@pytest.mark.parametrize("v2", [False, True])
async def test_i4_real_worker_lands_edit_proposals_then_confirms_and_notifies_current_doctor_once(
    db,
    calendar,
    monkeypatch,
    v2,
):
    cal, _ = calendar
    world = await edit_world(db)
    pid = uuid4()
    async with db() as session:
        session.add(Professional(id=pid, tenant_id=world.tenant.id, name="Dr. Diogo"))
        t = await session.get(Tenant, world.tenant.id)
        t.initial_flows = {"ai_draft_v2": v2}
        await session.commit()
    before = await reload_appointment(db, world.appointment.id)
    redis, mailer = _FakeRedis(), _Mailer()
    monkeypatch.setattr(notice, "get_entitlements", AsyncMock(return_value=_summary()))
    monkeypatch.setattr(
        notice,
        "fetch_professional_emails",
        AsyncMock(return_value={str(pid): "qa-new-doctor@example.test"}),
    )
    monkeypatch.setattr(notice, "send_transactional_email_result", mailer)
    proposals = [
        {"open_field": "service"},
        {"open_field": "doctor"},
        {"professional": "Dr. Diogo", "keep": ["day", "time", "insurance"]},
    ]
    agent = AsyncMock(side_effect=["__APPOINTMENT_EDIT__:" + json.dumps(p) for p in proposals])
    monkeypatch.setattr(workers_ns, "run_agent", agent)
    cal._days = [(datetime.now(UTC) + timedelta(days=4)).replace(tzinfo=None)]
    await tap(db, world, ae.LABEL_EDIT_DATE)
    for body, step in [
        ("na verdade gostaria de mudar o serviço", fr.STEP_EDIT_SERVICE),
        ("desculpa, quero mudar o médico", fr.STEP_EDIT_DOCTOR),
        ("para o doutor Diogo, mantendo o horário", fr.STEP_EDIT_CONFIRM),
    ]:
        await say(db, world, body, redis)
        saved = await get_conversation(db, world)
        assert saved.flow_step == step
        assert saved.flow_edit_draft is not None
    card = (await outbound_messages(db, world.conversation.id))[-1]
    assert "O que mudou: médico." in card.body
    assert not cal.created and not cal.detail_updates
    assert (
        await reload_appointment(db, world.appointment.id)
    ).professional_id == before.professional_id
    await tap(db, world, fr.LABEL_CONFIRM, redis=redis)
    row = await reload_appointment(db, world.appointment.id)
    assert row.professional_id == pid and row.start_at == before.start_at
    assert len(cal.created) + len(cal.detail_updates) == 1
    jobs = [job for job in redis.jobs if job[0] == notice.JOB_NAME]
    assert len(jobs) == 1 and jobs[0][1][-1] == ["médico"]
    await notice.send_professional_edit_notification({"redis": redis}, *jobs[0][1])
    assert len(mailer.calls) == 1
    assert agent.call_args.kwargs["editing"] is True


async def test_worker_does_not_apply_old_ai_proposal_over_a_newer_edit_draft(
    db, calendar, monkeypatch
):
    from secretaria.models import Conversation

    world = await edit_world(db)
    await tap(db, world, ae.LABEL_EDIT_DATE)
    before = await get_conversation(db, world)
    latest = {
        **before.flow_edit_draft,
        "current": {**before.flow_edit_draft["current"], "insurance": "QA updated plan"},
    }

    async def late_agent(*args, **kwargs):
        async with db() as session:
            conv = await session.get(Conversation, world.conversation.id)
            conv.flow_edit_draft = latest
            await session.commit()
        return '__APPOINTMENT_EDIT__:{"insurance":"Particular"}'

    monkeypatch.setattr(workers_ns, "run_agent", late_agent)
    await say(db, world, "quero mudar o convênio")
    saved = await get_conversation(db, world)
    assert saved.flow_edit_draft == latest
    assert saved.flow_step == before.flow_step


async def test_worker_redraws_same_edit_when_agent_entrypoint_raises(db, calendar, monkeypatch):
    world = await edit_world(db)
    await tap(db, world, ae.LABEL_EDIT_DATE)
    before = await get_conversation(db, world)
    count = len(await outbound_messages(db, world.conversation.id))
    monkeypatch.setattr(
        workers_ns, "run_agent", AsyncMock(side_effect=RuntimeError("AI unavailable"))
    )
    await say(db, world, "quero seguir com o mesmo horário")
    saved = await get_conversation(db, world)
    assert saved.flow_edit_draft == before.flow_edit_draft
    assert saved.flow_step == before.flow_step
    outbound = (await outbound_messages(db, world.conversation.id))[count:]
    assert outbound and outbound[-1].interactive
