"""Confirming the new booking replaces the original in the same turn (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.models import Appointment, AppointmentStatus, BookingHold, FlowState
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import FlowRouterResult
from secretaria.workers import tasks
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import get_conversation, reminder_rows, sent, set_conversation, wire
from tests._reminders_v2 import WA_ID, add_reminder, get_reminder, reload_appointment, seed_world

CONFIRMED = "Pronto! Seu agendamento está confirmado. ✅"


@pytest.fixture
def calendar(monkeypatch, db):  # noqa: F811
    return wire(monkeypatch, db)


def _future(days: int = 3) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


def _booking(start: datetime) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=CONFIRMED)],
        flow_state=FlowState.IDLE,
        appointment={
            "google_event_id": "evt-new",
            "appointment_type": "Consulta",
            "start_at": start,
            "end_at": start + timedelta(minutes=30),
        },
    )


async def _confirm(world, result) -> None:
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="✅ Confirmar"
    )
    await tasks._apply_flow_result(reply, result, WA_ID, tenant=world.tenant, waba_token="t")


async def _new_appointment(db) -> Appointment | None:  # noqa: F811
    async with db() as session:
        return await session.scalar(
            select(Appointment).where(Appointment.google_event_id == "evt-new")
        )


def _notice(world) -> str:
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    return f"Sua consulta anterior, de {when}, foi cancelada."


async def test_confirming_the_new_booking_cancels_the_original_in_the_same_turn(
    db,  # noqa: F811
    calendar,
):
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")
    old_row = await add_reminder(db, world, kind="day", due_at=_future(2))
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_replaces_appointment_id=world.appointment.id,
    )

    await _confirm(world, _booking(_future(6)))

    new = await _new_appointment(db)
    assert new is not None and new.status == AppointmentStatus.SCHEDULED
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED
    assert calendar.cancelled == ["evt-old"]
    assert (await get_reminder(db, old_row)).status == "cancelled"
    assert any(row.status == "pending" for row in await reminder_rows(db, new.id))
    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None
    [text] = [item[2] for item in sent() if item[0] == "text"]
    assert text == f"{CONFIRMED}\n\n{_notice(world)}"


async def test_without_a_marker_nothing_else_is_cancelled(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")

    await _confirm(world, _booking(_future(6)))

    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED
    assert calendar.cancelled == []
    assert [item[2] for item in sent() if item[0] == "text"] == [CONFIRMED]


async def test_a_booking_that_fails_to_persist_cancels_nothing(db, calendar, monkeypatch):  # noqa: F811
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")
    await set_conversation(
        db,
        world,
        flow_state=FlowState.SERVICE_CATALOG,
        flow_replaces_appointment_id=world.appointment.id,
    )

    async def _broken(*args, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(workers_ns, "resolve_booking_plan_ids", _broken)

    await _confirm(world, _booking(_future(6)))

    assert await _new_appointment(db) is None
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED
    assert calendar.cancelled == []
    # Rolled back with the booking: the patient can still finish it.
    assert (await get_conversation(db, world)).flow_replaces_appointment_id == world.appointment.id


async def test_the_portal_promotion_replaces_the_original_too(db, calendar):  # noqa: F811
    world = await seed_world(
        db, start_at=_future(), channel=CHANNEL_BRAIN_MESSAGE, google_event_id="evt-old"
    )
    await set_conversation(
        db,
        world,
        flow_state=FlowState.AWAITING_EMAIL_CODE,
        flow_replaces_appointment_id=world.appointment.id,
    )
    start = _future(6)
    async with db() as session:
        session.add(
            BookingHold(
                tenant_id=world.tenant.id,
                conversation_id=world.conversation.id,
                patient_id=world.patient.id,
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()
    reply = tasks._ReplyContext(
        channel=CHANNEL_BRAIN_MESSAGE,
        conversation_id=world.conversation.id,
        patient_ref=world.patient.external_id,
        inbound_body="123456",
    )

    extra = await tasks._promote_booking_hold(
        reply, tenant=world.tenant, waba_token=None, professionals=[], redis=None
    )

    assert "Pronto! Seu agendamento está confirmado." in extra
    assert extra.endswith(_notice(world))
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED
    assert calendar.cancelled == ["evt-old"]
    assert (await get_conversation(db, world)).flow_replaces_appointment_id is None
