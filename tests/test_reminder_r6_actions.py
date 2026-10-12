"""Confirmar + the recurring menu, Cancelar straight to the question (TASK-032 R6, spec §5.1)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import (
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    PixDeposit,
    Tenant,
)
from secretaria.services.flow_router import main_menu_buttons, menu_label
from secretaria.services.reminder_text import give_up_confirm_buttons
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


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


def _future(hours: int = 48) -> datetime:
    return datetime.now(UTC) + timedelta(hours=hours)


async def _tap(world, action, reminder_id, *, conversation_id=None, patient_ref=WA_ID) -> None:
    reply = tasks._ReplyContext(
        conversation_id=conversation_id or world.conversation.id,
        patient_ref=patient_ref,
        inbound_body="x",
    )
    await tasks._handle_action_button(reply, action, str(reminder_id))


def _when(start) -> str:
    return _format_appointment_when(start, "America/Sao_Paulo")


async def test_confirm_sends_the_confirmation_then_the_recurring_menu(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remconfirm", rid)

    assert [item[0] for item in sent()] == ["text", "buttons"]
    assert sent()[0][2] == ra.CONFIRMED_TEXT.format(when=_when(world.start_at))
    _kind, _to, body, buttons = sent()[1]
    assert body == menu_label(world.tenant)
    assert [label for _id, label in buttons] == main_menu_buttons()
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_a_confirmation_that_was_not_recorded_shows_no_menu(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), status=AppointmentStatus.CANCELLED)
    rid = await add_reminder(db, world)

    await _tap(world, "remconfirm", rid)

    assert [item[0] for item in sent()] == ["text"]
    assert sent()[0][2] == ra.NOT_ACTIVE_TEXT


async def test_cancel_goes_straight_to_the_confirmation_question(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remcancel", rid)

    [(kind, _to, body, buttons)] = sent()
    assert kind == "buttons"
    assert body == ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))
    assert buttons == give_up_confirm_buttons(rid)
    assert (await get_reminder(db, rid)).answer == "cancel"
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_cancel_inside_the_refund_window_warns_about_the_deposit_first(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(hours=5))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world, kind="hour")

    await _tap(world, "remcancel", rid)

    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
        tenant = await session.get(Tenant, world.tenant.id)
    [(_kind, _to, body, _buttons)] = sent()
    warning = _pix_retention_warning_line(tenant, deposit)
    assert body == f"{warning} {ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))}"


async def test_legacy_ids_still_resolve(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remgiveup", rid)  # today's "Cancelar"
    assert [item[0] for item in sent()] == ["buttons"]
    assert sent()[0][2] == ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))

    for action in ("remother", "remresched", "remnew"):
        await _tap(world, action, rid)
    cards = [item for item in sent() if item[0] == "list"]
    assert len(cards) == 3
    assert all(item[2].startswith("*Alterar Dados*") for item in cards)
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_alterar_dados_is_recorded_and_opens_the_edit_menu(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remedit", rid)

    assert sent()[0][0] == "list" and sent()[0][2].startswith("*Alterar Dados*")
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING
    assert (await get_reminder(db, rid)).answer == "other"


async def test_new_ids_are_checked_against_the_patient_and_the_version(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    async with db() as session:
        intruder = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900002222")
        session.add(intruder)
        await session.flush()
        intruder_conversation = Conversation(
            id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id
        )
        session.add(intruder_conversation)
        await session.commit()

    for action in ("remcancel", "remedit"):
        await _tap(
            world,
            action,
            rid,
            conversation_id=intruder_conversation.id,
            patient_ref="5511900002222",
        )
    old = await add_reminder(
        db, world, kind="hour", appointment_start_at=world.start_at - timedelta(days=1)
    )
    await _tap(world, "remcancel", old)

    texts = [item[2] for item in sent() if item[0] == "text"]
    assert texts == [ra.NOT_FOUND_TEXT] * 2 + [ra.MOVED_TEXT.format(when=_when(world.start_at))]
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE
