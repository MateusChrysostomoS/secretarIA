"""Where the "Marcar outra" marker is written and where it is dropped (TASK-032 R3)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.ai import tools
from secretaria.ai.formatter import TextBubble
from secretaria.models import (
    AppointmentStatus,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import (
    LABEL_CANCEL,
    STEP_AWAITING_ATTENDEE_NAME,
    STEP_AWAITING_CONFIRMATION,
    STEP_AWAITING_RETRY,
    FlowRouterResult,
    MenuBubble,
)
from secretaria.workers import tasks
from secretaria.workers.shared import state_expiry
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, get_conversation, set_conversation, turn, wire
from tests._reminders_v2 import WA_ID, reload_appointment, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


def _reply(world) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=3)


async def test_apply_writes_the_marker_a_result_names(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    result = FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body="ok")],
        flow_state=FlowState.SERVICE_CATALOG,
        flow_replaces_appointment_id=world.appointment.id,
    )

    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, waba_token="t"
    )

    assert (await get_conversation(db, world)).flow_replaces_appointment_id == world.appointment.id


async def test_apply_clears_the_marker_when_the_result_leaves_the_booking(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_replaces_appointment_id=world.appointment.id,
    )
    menu = FlowRouterResult(
        action="reply",
        bubbles=[MenuBubble(body="Como posso ajudar?", labels=["Agendar"])],
        flow_state=FlowState.MENU,
    )

    await tasks._apply_flow_result(_reply(world), menu, WA_ID, tenant=world.tenant, waba_token="t")

    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None
    # Abandoned: the original is untouched.
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


def _parked(flow_state, flow_step=None) -> SimpleNamespace:
    return SimpleNamespace(
        flow_state=flow_state,
        flow_step=flow_step,
        flow_selected_type="Consulta",
        flow_selected_day=None,
        flow_selected_slot=None,
        flow_managing_appointment_id=None,
        flow_attendee_name="Ana",
        flow_draft=None,
        flow_replaces_appointment_id=uuid4(),
    )


def test_the_llm_floor_drops_the_marker():
    conversation = _parked(FlowState.LLM)
    quiet_since = datetime.now(UTC) - timedelta(days=2)

    assert state_expiry._expire_stale_llm_state(
        conversation, SimpleNamespace(initial_flows={}), quiet_since
    )
    assert conversation.flow_replaces_appointment_id is None


def test_the_attendee_floor_drops_the_marker():
    conversation = _parked(FlowState.SERVICE_CATALOG, STEP_AWAITING_ATTENDEE_NAME)
    quiet_since = datetime.now(UTC) - timedelta(days=2)

    assert state_expiry._expire_stale_attendee_step(
        conversation, SimpleNamespace(initial_flows={}), quiet_since
    )
    assert conversation.flow_replaces_appointment_id is None


async def test_no_to_quer_continuar_drops_the_marker(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await consent(db, world)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        reactivation_origin=FlowState.SERVICE_CATALOG.value,
        flow_replaces_appointment_id=world.appointment.id,
    )

    reply = await turn(db, world, "Não", send=False)

    assert reply.reactivation.kind == "reset"
    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None


async def test_an_abandoned_portal_code_wait_drops_the_marker(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.AWAITING_EMAIL_CODE,
        flow_replaces_appointment_id=world.appointment.id,
    )

    await turn(db, world, "oi", send=False)

    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.flow_replaces_appointment_id is None


async def _prior_message(db, world) -> None:  # noqa: F811
    """An earlier inbound, so the turn under test is not the conversation's first contact."""
    async with db() as session:
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
                created_at=datetime.now(UTC) - timedelta(minutes=5),
            )
        )
        await session.commit()


async def test_the_router_snapshot_carries_the_marker_through_a_turn(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await consent(db, world)
    await _prior_message(db, world)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_step=STEP_AWAITING_CONFIRMATION,
        flow_selected_type="Consulta",
        flow_selected_slot=(_future() + timedelta(days=1)).isoformat(),
        flow_replaces_appointment_id=world.appointment.id,
    )

    await turn(db, world, LABEL_CANCEL)

    conversation = await get_conversation(db, world)
    assert conversation.flow_step == STEP_AWAITING_RETRY
    assert conversation.flow_replaces_appointment_id == world.appointment.id


async def test_an_agent_booking_clears_the_marker_and_cancels_nothing(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await set_conversation(
        db,
        world,
        flow_state=FlowState.LLM,
        flow_replaces_appointment_id=world.appointment.id,
    )
    tenant_token = tools._tenant_id_ctx.set(world.tenant.id)
    conversation_token = tools._conversation_id_ctx.set(world.conversation.id)
    try:
        start = _future() + timedelta(days=2)
        await tools._persist_appointment(
            {"id": "evt-agent"}, start, start + timedelta(minutes=30), "Consulta"
        )
    finally:
        tools._conversation_id_ctx.reset(conversation_token)
        tools._tenant_id_ctx.reset(tenant_token)

    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED
