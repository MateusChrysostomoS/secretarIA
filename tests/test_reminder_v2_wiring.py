"""Every booking / move / cancel path calls the reminder hooks (TASK-032 R2, spec §4.2)."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from secretaria.ai import tools
from secretaria.core import database as core_database
from secretaria.models import Appointment, Tenant
from secretaria.services import reminder_hooks
from secretaria.services.flow_router import FlowRouterResult
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    NOW,
    WA_ID,
    FakeWhatsAppClient,
    HookSpy,
    fake_waba_token,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(workers_ns, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(workers_ns, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(workers_ns, "get_waba_token", fake_waba_token)
    yield


@pytest.fixture
def spy(monkeypatch) -> HookSpy:
    return HookSpy().install(monkeypatch, reminder_hooks)


def _reply(world) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=world.conversation.id,
        tenant_id=world.tenant.id,
        patient_ref=WA_ID,
        inbound_body="",
    )


async def _tenant(db, world) -> Tenant:  # noqa: F811
    async with db() as session:
        return await session.get(Tenant, world.tenant.id)


async def _appointment_id(db, google_event_id: str):  # noqa: F811
    async with db() as session:
        return await session.scalar(
            select(Appointment.id).where(Appointment.google_event_id == google_event_id)
        )


async def _flow(db, world, result: FlowRouterResult) -> None:  # noqa: F811
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=await _tenant(db, world), waba_token="tok"
    )


def _booking(event_id: str) -> FlowRouterResult:
    start = NOW + timedelta(days=5)
    return FlowRouterResult(
        action="reply",
        bubbles=[],
        appointment={
            "google_event_id": event_id,
            "appointment_type": "Consulta",
            "start_at": start,
            "end_at": start + timedelta(minutes=30),
        },
    )


# ---- booking paths (Task 10) ------------------------------------------------


@pytest.mark.parametrize("v2", [True, False])
async def test_a_flow_booking_plans_reminders_only_with_the_switch_on(db, spy, v2):  # noqa: F811
    world = await seed_world(db, v2=v2)

    await _flow(db, world, _booking("evt-flow"))

    expected = [("booked", await _appointment_id(db, "evt-flow"))] if v2 else []
    assert spy.calls == expected


@pytest.mark.parametrize("v2", [True, False])
async def test_an_agent_booking_plans_reminders_only_with_the_switch_on(db, spy, v2):  # noqa: F811
    world = await seed_world(db, v2=v2)
    tenant_token = tools._tenant_id_ctx.set(world.tenant.id)
    conversation_token = tools._conversation_id_ctx.set(world.conversation.id)
    try:
        start = NOW + timedelta(days=4)
        await tools._persist_appointment(
            {"id": "evt-agent"}, start, start + timedelta(minutes=30), "Consulta"
        )
    finally:
        tools._conversation_id_ctx.reset(conversation_token)
        tools._tenant_id_ctx.reset(tenant_token)

    expected = [("booked", await _appointment_id(db, "evt-agent"))] if v2 else []
    assert spy.calls == expected


# ---- move and cancel paths (Task 11) ----------------------------------------


@pytest.mark.parametrize("v2", [True, False])
async def test_a_flow_cancel_cancels_the_reminders_only_with_the_switch_on(db, spy, v2):  # noqa: F811
    world = await seed_world(db, v2=v2)

    await _flow(
        db,
        world,
        FlowRouterResult(
            action="reply", bubbles=[], appointment_cancel_id=world.appointment.google_event_id
        ),
    )

    assert spy.calls == ([("closed", world.appointment.id, "cancelled")] if v2 else [])


@pytest.mark.parametrize(
    ("v2", "confirmations", "expected"),
    [(True, 0, True), (False, 0, False), (False, 1, True)],
)
async def test_a_flow_reschedule_replans_when_on_or_when_there_is_a_count_to_zero(
    db,  # noqa: F811
    spy,
    v2,
    confirmations,
    expected,  # noqa: F811
):
    world = await seed_world(db, v2=v2, confirmation_count=confirmations)
    new_start = world.start_at + timedelta(days=2)

    await _flow(
        db,
        world,
        FlowRouterResult(
            action="reply",
            bubbles=[],
            appointment_reschedule={
                "google_event_id": world.appointment.google_event_id,
                "start_at": new_start,
                "end_at": new_start + timedelta(minutes=30),
            },
        ),
    )

    assert spy.calls == ([("moved", world.appointment.id)] if expected else [])


@pytest.mark.parametrize("action", ["apptcancel", "apptcancelyes"])
async def test_a_cancel_button_cancels_the_reminders(db, spy, action):  # noqa: F811
    world = await seed_world(db, google_event_id="")  # no Google event: no calendar call

    await tasks._handle_action_button(_reply(world), action, str(world.appointment.id))

    assert spy.calls == [("closed", world.appointment.id, "cancelled")]


async def test_a_cancel_button_with_the_switch_off_touches_nothing(db, spy):  # noqa: F811
    world = await seed_world(db, v2=False, google_event_id="")

    await tasks._handle_action_button(_reply(world), "apptcancel", str(world.appointment.id))

    assert spy.calls == []


@pytest.mark.parametrize("v2", [True, False])
async def test_an_agent_cancel_cancels_the_reminders_only_with_the_switch_on(db, spy, v2):  # noqa: F811
    world = await seed_world(db, v2=v2)
    tenant_token = tools._tenant_id_ctx.set(world.tenant.id)
    try:
        await tools._mark_appointment_cancelled(world.appointment.google_event_id)
    finally:
        tools._tenant_id_ctx.reset(tenant_token)

    assert spy.calls == ([("closed", world.appointment.id, "cancelled")] if v2 else [])
