"""Direct cancellation and legacy 'Agendar Outra' edit compatibility after R6."""

from datetime import UTC, datetime, timedelta

import pytest

from secretaria.models import AppointmentStatus, FlowState
from secretaria.services.appointment_edit import (
    EDIT_PIX_NOTICE,
    LABEL_EDIT_DOCTOR,
    LABEL_EDIT_SERVICE,
)
from secretaria.services.flow_router import DECLINE_REASON_QUESTION, STEP_DECLINE_REASON
from secretaria.workers import tasks
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


async def test_legacy_book_another_edits_the_existing_booking_instead(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    await _tap(world, "remnew", rid)
    conv = await get_conversation(db, world)
    assert conv.flow_state == FlowState.EDIT_BOOKING
    assert conv.flow_replaces_appointment_id is None
    assert conv.flow_edit_draft["appointment_id"] == str(world.appointment.id)
    assert calendar.created == [] and calendar.cancelled == []
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_legacy_book_another_with_paid_deposit_hides_price_changes(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=_future(hours=5))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world, kind="hour")
    await _tap(world, "remnew", rid)
    assert EDIT_PIX_NOTICE in sent()[0][2]
    labels = [label for _id, label in sent()[0][3]]
    assert LABEL_EDIT_SERVICE not in labels and LABEL_EDIT_DOCTOR not in labels
    assert calendar.created == [] and calendar.cancelled == []
