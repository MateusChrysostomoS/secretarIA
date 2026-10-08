"""Reminder buttons tapped in the Portal (TASK-032 R3, spec §4.3 "Portal")."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import menu_label
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import chat_rows, consent, turn, wire
from tests._reminders_v2 import (
    add_reminder,
    get_reminder,
    outbound_messages,
    reload_appointment,
    seed_world,
)


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


async def _portal_world(db):  # noqa: F811
    world = await seed_world(
        db,
        channel=CHANNEL_BRAIN_MESSAGE,
        start_at=datetime.now(UTC) + timedelta(days=2),
        last_inbound_at=datetime.now(UTC) - timedelta(hours=7),
    )
    await consent(db, world)
    return world


async def _portal_tap(world, reply_id: str, text: str = "Confirmar"):
    return await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text=text,
        interactive_reply_id=reply_id,
    )


async def test_a_portal_tap_on_the_opening_card_confirms(db):  # noqa: F811
    world = await _portal_world(db)
    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)

    reply = await _portal_tap(world, f"remconfirm|{chat.id}")
    await _send_bot_reply(reply)

    assert reply.action_button == ("remconfirm", str(chat.id))
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert bodies[-2:] == [
        ra.CONFIRMED_TEXT.format(when=when),
        menu_label(world.tenant) + "\n(opções: 🗓️ Agendar, Outro)",
    ]


async def test_a_portal_tap_on_the_cancel_path_reaches_its_step(db):  # noqa: F811
    world = await _portal_world(db)
    await turn(db, world, "oi")
    [chat] = await chat_rows(db, world.appointment.id)
    # Cancelar directly records the confirmation card carrying remkeep.
    await _send_bot_reply(await _portal_tap(world, f"remcancel|{chat.id}", "Cancelar"))

    reply = await _portal_tap(world, f"remkeep|{chat.id}", "Manter consulta")
    await _send_bot_reply(reply)

    assert reply.action_button == ("remkeep", str(chat.id))
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert bodies[-1] == ra.KEPT_TEXT.format(when=when)


async def test_a_portal_tap_with_another_patients_reminder_id_counts_nothing(db):  # noqa: F811
    world = await _portal_world(db)
    async with db() as session:
        intruder = Patient(
            id=uuid4(),
            tenant_id=world.tenant.id,
            channel=CHANNEL_BRAIN_MESSAGE,
            external_id=str(uuid4()),
        )
        session.add(intruder)
        await session.flush()
        session.add(Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id))
        start = datetime.now(UTC) + timedelta(days=3)
        theirs = Appointment(
            id=uuid4(),
            tenant_id=world.tenant.id,
            patient_id=intruder.id,
            google_event_id="evt-theirs",
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(theirs)
        await session.commit()
    their_world = type(world)(world.tenant, intruder, world.conversation, theirs, start)
    their_row = await add_reminder(db, their_world)

    reply = await _portal_tap(world, f"remconfirm|{their_row}")

    assert reply.action_button is None
    assert (await reload_appointment(db, theirs.id)).confirmation_count == 0
    assert (await get_reminder(db, their_row)).answer is None


async def test_older_action_ids_keep_todays_title_routing_on_the_portal(db):  # noqa: F811
    world = await _portal_world(db)
    old_id = f"rebooksame|{world.appointment.id}"
    async with db() as session:
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body="Quer remarcar?",
                interactive={
                    "kind": "buttons",
                    "body": "Quer remarcar?",
                    "options": [{"id": old_id, "title": "Remarcar"}],
                },
            )
        )
        await session.commit()

    reply = await _portal_tap(world, old_id, "Remarcar")

    assert reply.action_button is None
