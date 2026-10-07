"""The reminder as the chat's first message, end to end (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta

import pytest

from secretaria.models import AppointmentStatus, FlowState, Patient
from secretaria.services.attendee import ATTENDEE_QUESTION_BODY
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import LABEL_BOOK, menu_label
from secretaria.services.reminder_text import (
    ReminderContent,
    build_reminder_body,
    reminder_buttons,
)
from secretaria.workers import tasks
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import (
    AGENT_REPLY,
    add_appointment,
    age_conversation,
    chat_rows,
    consent,
    get_conversation,
    sent,
    turn,
    wire,
)
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    outbound_messages,
    reload_appointment,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


async def _quiet_world(db, *, consented=True, **kwargs):  # noqa: F811
    kwargs.setdefault("start_at", datetime.now(UTC) + timedelta(days=2))
    world = await seed_world(db, last_inbound_at=datetime.now(UTC) - timedelta(hours=7), **kwargs)
    if consented:
        await consent(db, world)
    return world


def _card_body(world, start=None) -> str:
    return build_reminder_body(
        ReminderContent(
            clinic_name="Clínica Olhar",
            timezone="America/Sao_Paulo",
            start_at=start or world.start_at,
            service_name="Consulta",
        )
    )


async def _tap(world, action, reminder_id) -> None:
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    await tasks._handle_action_button(reply, action, str(reminder_id))


async def test_after_six_hours_the_reminder_opens_the_chat_and_the_menu_is_suppressed(
    db,  # noqa: F811
):
    world = await _quiet_world(db)

    await turn(db, world, "oi")

    [chat] = await chat_rows(db, world.appointment.id)
    assert sent() == [("buttons", WA_ID, _card_body(world), reminder_buttons(chat.id))]
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE
    [card] = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    assert [option["id"] for option in card.interactive["options"]] == [
        button_id for button_id, _label in reminder_buttons(chat.id)
    ]


async def test_a_portal_patient_gets_the_same_card_on_the_same_rules(db):  # noqa: F811
    # The opening is channel-neutral (it goes out through `_send_buttons_reply`), so a
    # Brain-Message patient is greeted by the same card as a WhatsApp one.
    world = await _quiet_world(db, channel=CHANNEL_BRAIN_MESSAGE)

    await turn(db, world, "oi")

    [chat] = await chat_rows(db, world.appointment.id)
    [card] = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    # The stored `body` is the flattened text the AI reads: the card text plus its
    # "(opções: ...)" line. The tappable structure is `interactive`.
    assert card.body.startswith(_card_body(world))
    assert [option["id"] for option in card.interactive["options"]] == [
        button_id for button_id, _label in reminder_buttons(chat.id)
    ]


async def test_a_clear_request_is_answered_right_after_the_card(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, LABEL_BOOK)

    assert [item[0] for item in sent()] == ["buttons", "buttons"]
    assert sent()[0][2] == _card_body(world)
    assert sent()[1][2] == ATTENDEE_QUESTION_BODY
    assert (await get_conversation(db, world)).flow_state == FlowState.SERVICE_CATALOG


async def test_consent_not_yet_given_gets_only_the_consent_prompt(db):  # noqa: F811
    world = await _quiet_world(db, consented=False)

    await turn(db, world, "oi")

    assert await chat_rows(db, world.appointment.id) == []
    assert not any(str(item[2]).startswith("LEMBRE-SE") for item in sent())
    async with db() as session:
        assert (await session.get(Patient, world.patient.id)).lgpd_accepted_at is None


async def test_two_appointments_open_with_the_nearest_and_the_answer_names_the_other(
    db,  # noqa: F811
):
    world = await _quiet_world(db)
    other = await add_appointment(db, world, start_at=datetime.now(UTC) + timedelta(days=5))

    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    await _tap(world, "remconfirm", chat.id)

    assert sent()[0][2] == _card_body(world)
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    other_when = _format_appointment_when(other.start_at, "America/Sao_Paulo")
    assert sent()[-1][2] == (
        f"{ra.CONFIRMED_TEXT.format(when=when)}\n\n"
        f"{ra.ALSO_SCHEDULED_TEXT.format(whens=other_when)}"
    )


async def test_confirmed_twice_gets_the_normal_menu(db):  # noqa: F811
    world = await _quiet_world(db, confirmation_count=2)

    await turn(db, world, "oi")

    assert await chat_rows(db, world.appointment.id) == []
    assert [item[2] for item in sent()] == [menu_label(world.tenant)]


async def test_a_cancelled_appointment_gets_the_normal_menu(db):  # noqa: F811
    world = await _quiet_world(db, status=AppointmentStatus.CANCELLED)

    await turn(db, world, "oi")

    assert await chat_rows(db, world.appointment.id) == []
    assert [item[2] for item in sent()] == [menu_label(world.tenant)]


async def test_the_switch_off_keeps_todays_behaviour(db):  # noqa: F811
    world = await _quiet_world(db, v2=False)

    await turn(db, world, "oi")

    assert await chat_rows(db, world.appointment.id) == []
    assert [item[2] for item in sent()] == [menu_label(world.tenant)]


async def test_a_second_message_right_after_the_card_gets_no_second_card(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, "oi")
    await turn(db, world, "oi de novo")

    assert len(await chat_rows(db, world.appointment.id)) == 1
    assert [item[2] for item in sent()] == [_card_body(world), menu_label(world.tenant)]


async def test_chat_and_cron_confirmations_share_the_counter(db):  # noqa: F811
    world = await _quiet_world(db)
    day = await add_reminder(db, world, kind="day")
    await _tap(world, "remconfirm", day)

    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    await _tap(world, "remconfirm", chat.id)
    await _tap(world, "remconfirm", chat.id)

    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 2
    await age_conversation(db, world, hours=7)
    await turn(db, world, "oi")
    assert len(await chat_rows(db, world.appointment.id)) == 1
    assert sent()[-1][2] == menu_label(world.tenant)


async def test_the_same_chat_card_reshown_later_counts_once(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, "oi")
    await age_conversation(db, world, hours=7)
    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    cards = [item for item in sent() if item[0] == "buttons"]
    await _tap(world, "remconfirm", chat.id)
    await _tap(world, "remconfirm", chat.id)

    assert [card[3] for card in cards] == [reminder_buttons(chat.id)] * 2
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_outro_on_the_card_hands_the_next_message_to_the_ai(db):  # noqa: F811
    world = await _quiet_world(db)

    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    await _tap(world, "remother", chat.id)
    await turn(db, world, "quanto custa a consulta?")

    assert (await get_conversation(db, world)).flow_state == FlowState.LLM
    assert sent()[-1][2] == AGENT_REPLY
