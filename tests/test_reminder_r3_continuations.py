"""Cancelar Consulta confirmed and Agendar Outra (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.models import AppointmentStatus, FlowState, PixDeposit, Tenant
from secretaria.services.attendee import ATTENDEE_QUESTION_BODY
from secretaria.services.flow_router import DECLINE_REASON_QUESTION, STEP_DECLINE_REASON
from secretaria.workers import tasks
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.deposit import _pix_retention_warning_line
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import get_conversation, sent, wire
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    get_reminder,
    reload_appointment,
    seed_paid_deposit,
    seed_world,
)


@pytest.fixture
def calendar(monkeypatch, db):  # noqa: F811
    return wire(monkeypatch, db)


def _future(hours: int = 48) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


async def _tap(world, action, reminder_id) -> None:
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    await tasks._handle_action_button(reply, action, str(reminder_id))


def _when(world) -> str:
    return _format_appointment_when(world.start_at, "America/Sao_Paulo")


async def test_yes_cancel_cancels_closes_the_reminders_and_asks_why(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")
    rid = await add_reminder(db, world, kind="day", due_at=_future(10))

    await _tap(world, "remgiveupyes", rid)

    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED
    assert calendar.cancelled == ["evt-old"]
    assert (await get_reminder(db, rid)).status == "cancelled"
    kinds = [item[0] for item in sent()]
    assert kinds == ["text", "list"]
    assert sent()[0][2] == "Consulta cancelada."
    assert sent()[1][2] == DECLINE_REASON_QUESTION
    conversation = await get_conversation(db, world)
    assert conversation.flow_step == STEP_DECLINE_REASON
    assert conversation.flow_managing_appointment_id == world.appointment.id


async def test_book_another_opens_a_booking_that_replaces_this_one(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remnew", rid)

    intro = ra.BOOK_ANOTHER_INTRO.format(when=_when(world))
    assert [item[0] for item in sent()] == ["text", "buttons"]
    assert sent()[0][2] == intro
    assert sent()[1][2] == ATTENDEE_QUESTION_BODY
    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG
    assert conversation.flow_replaces_appointment_id == world.appointment.id
    # Nothing is cancelled before the new booking is confirmed.
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_book_another_inside_the_refund_window_warns_about_the_deposit(
    db,  # noqa: F811
    calendar,
):
    world = await seed_world(db, start_at=_future(hours=5))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world, kind="hour")

    await _tap(world, "remnew", rid)

    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
        tenant = await session.get(Tenant, world.tenant.id)
    intro = ra.BOOK_ANOTHER_INTRO.format(when=_when(world))
    assert sent()[0][2] == f"{intro}\n\n{_pix_retention_warning_line(tenant, deposit)}"
