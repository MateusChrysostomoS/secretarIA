"""The reminder card on EVERY entry, reminders on or off, until 3 confirmations (2026-10-27).

Owner, 2026-10-27: a patient with a booked appointment who enters the conversation with the
clinic (Portal AND WhatsApp) gets exactly the reminder message - same text, same buttons,
"Alterar Dados" leading to "Mudar serviço" - whether or not the clinic switched the
scheduled reminders on. The card stops being sent on entry once the patient confirmed the
appointment 3 times; the 1-day and 1-hour reminders stay exactly as they are.

"Entering" reuses the existing definitions: the first message of a conversation, the first
message after the clinic's reactivation gap (turn router, both channels) and the Portal's
entry job.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update

from secretaria.models import Appointment, AppointmentStatus, FlowState, Tenant
from secretaria.services import (
    appointment_edit as ae,
    clinic_action_notice,
    reminder_hooks,
    reminder_schedule,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.reminder_text import (
    build_reminder_body,
    load_reminder_content,
    reminder_buttons,
)
from secretaria.workers import reminder_engine, tasks
from secretaria.workers.shared import opening
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import (
    age_conversation,
    chat_rows,
    consent,
    get_conversation,
    reminder_rows,
    sent,
    turn,
    wire,
)
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment, seed_world

START = datetime(2030, 10, 15, 18, 20, tzinfo=UTC)
BODY = (
    "LEMBRE-SE: Sua consulta com Dra. Ana está marcada para o dia 15/10/2030 "
    "às 15:20 para Consulta.\n\nOrientações para a consulta:\n• Trazer exames anteriores"
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


async def _world(db, *, portal=False, v2=False, **kw):  # noqa: F811
    world = await seed_world(
        db,
        start_at=kw.pop("start_at", START),
        professional_name="Dra. Ana",
        requirements=["Trazer exames anteriores"],
        v2=v2,
        **({"channel": CHANNEL_BRAIN_MESSAGE} if portal else {}),
        **kw,
    )
    await consent(db, world)
    return world


def _reply(world):
    portal = world.patient.wa_id is None
    return tasks._ReplyContext(
        conversation_id=world.conversation.id,
        tenant_id=world.tenant.id,
        channel=CHANNEL_BRAIN_MESSAGE if portal else "whatsapp",
        patient_ref=world.patient.external_id if portal else WA_ID,
        inbound_body="",
    )


async def _cards(db, world):  # noqa: F811
    """The reminder cards that reached the patient, oldest first."""
    rows = await outbound_messages(db, world.conversation.id)
    return [row for row in rows if row.interactive and row.body.startswith("LEMBRE-SE:")]


async def _last_card_row(db, world):  # noqa: F811
    """The last card: its text and buttons are the reminder's, on the `chat` row."""
    cards = await _cards(db, world)
    assert cards, "no reminder card was sent"
    card = cards[-1]
    assert card.body.split("\n(opções:")[0] == BODY
    [chat] = await chat_rows(db, world.appointment.id)
    assert [(o["id"], o["title"]) for o in card.interactive["options"]] == reminder_buttons(chat.id)
    return chat


async def _confirm(db, world):  # noqa: F811
    chat = await _last_card_row(db, world)
    await tasks._handle_action_button(_reply(world), "remconfirm", str(chat.id))


async def _card_confirmations(db, world) -> int:  # noqa: F811
    return (await reload_appointment(db, world.appointment.id)).card_confirmation_count


# --- reminders OFF: the card still opens the conversation (both channels) -------------------


@pytest.mark.parametrize("portal", [False, True])
async def test_first_message_with_reminders_off_is_the_reminder_card(db, portal):  # noqa: F811
    world = await _world(db, portal=portal)

    await turn(db, world, "oi")

    await _last_card_row(db, world)
    assert len(await _cards(db, world)) == 1


@pytest.mark.parametrize("portal", [False, True])
async def test_a_quiet_return_with_reminders_off_opens_with_the_card(db, portal):  # noqa: F811
    world = await _world(db, portal=portal, last_inbound_at=datetime.now(UTC) - timedelta(hours=7))

    await turn(db, world, "oi")

    await _last_card_row(db, world)


async def test_the_portal_entry_with_reminders_off_sends_the_card(db):  # noqa: F811
    world = await _world(db, portal=True, last_inbound_at=datetime.now(UTC) - timedelta(hours=7))

    await tasks.process_brain_message_enter({}, str(world.tenant.id), world.patient.external_id)

    await _last_card_row(db, world)


async def test_the_card_is_exactly_the_scheduled_reminder(db):  # noqa: F811
    # The cron reminder is `build_reminder_body(load_reminder_content(...))` +
    # `reminder_buttons(row)` (workers/reminder_engine.py); the entry card must be the same.
    world = await _world(db, portal=True)

    await opening._send_context_opening(_reply(world), world.tenant, None, waba_token="t")

    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        content = await load_reminder_content(session, tenant, appointment)
    reminder_body = build_reminder_body(content)
    [card] = await _cards(db, world)
    [chat] = await chat_rows(db, world.appointment.id)
    assert card.body.split("\n(opções:")[0] == reminder_body
    assert [o["title"] for o in card.interactive["options"]] == [
        "Confirmar",
        "Cancelar",
        "Alterar Dados",
    ]
    assert [o["id"] for o in card.interactive["options"]] == [
        button_id for button_id, _label in reminder_buttons(chat.id)
    ]


async def test_with_reminders_off_alterar_dados_reaches_mudar_servico(db):  # noqa: F811
    world = await _world(db)
    await turn(db, world, "oi")
    chat = await _last_card_row(db, world)

    await tasks._handle_action_button(_reply(world), "remedit", str(chat.id))
    assert sent()[-1][0] == "list" and sent()[-1][2].startswith("*Alterar Dados*")
    assert ae.LABEL_EDIT_SERVICE in str(sent()[-1][4])  # the list's rows
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING

    await turn(db, world, ae.LABEL_EDIT_SERVICE)

    assert ae.EDIT_KEPT not in [item[2] for item in sent()]
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING


async def test_with_reminders_off_confirmar_counts(db):  # noqa: F811
    world = await _world(db)
    await turn(db, world, "oi")

    await _confirm(db, world)

    appointment = await reload_appointment(db, world.appointment.id)
    assert appointment.confirmation_count == 1
    assert appointment.card_confirmation_count == 1
    assert sent()[-2][2].startswith("Presença confirmada!")


# --- every entry until the 3rd confirmation --------------------------------------------------


@pytest.mark.parametrize("portal", [False, True])
async def test_every_entry_shows_the_card_until_three_confirmations(db, portal):  # noqa: F811
    world = await _world(db, portal=portal)

    await turn(db, world, "oi")  # first message of the conversation
    await _confirm(db, world)
    for expected in (2, 3):
        await age_conversation(db, world, hours=7)
        await turn(db, world, "oi")  # a new entry after the reactivation gap
        assert len(await _cards(db, world)) == expected
        await _confirm(db, world)
        assert await _card_confirmations(db, world) == expected

    await age_conversation(db, world, hours=7)
    await turn(db, world, "oi")

    assert len(await _cards(db, world)) == 3
    appointment = await reload_appointment(db, world.appointment.id)
    # Review I1: the entry card raises the agenda's counter once per date; repeats only
    # count towards the 3 that retire the card.
    assert (appointment.confirmation_count, appointment.card_confirmation_count) == (1, 3)


async def test_the_portal_entry_stops_after_three_confirmations(db):  # noqa: F811
    world = await _world(db, portal=True, last_inbound_at=datetime.now(UTC) - timedelta(hours=7))
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(card_confirmation_count=3, confirmation_count=2)
        )
        await session.commit()

    await tasks.process_brain_message_enter({}, str(world.tenant.id), world.patient.external_id)

    assert await _cards(db, world) == []
    assert await chat_rows(db, world.appointment.id) == []
    # The patient still gets the conversation's opening (with the manage buttons).
    assert await outbound_messages(db, world.conversation.id)


async def test_two_confirmations_still_show_the_card_on_entry(db):  # noqa: F811
    world = await _world(db, last_inbound_at=datetime.now(UTC) - timedelta(hours=7))
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(card_confirmation_count=2, confirmation_count=2)
        )
        await session.commit()

    await turn(db, world, "oi")

    await _last_card_row(db, world)


async def test_day_and_hour_reminders_stay_scheduled_after_three_confirmations(db):  # noqa: F811
    world = await _world(db, v2=True)
    assert await reminder_hooks.after_appointment_booked(world.appointment.id) >= 2
    planned = {
        row.kind: row for row in await reminder_rows(db, world.appointment.id) if row.kind != "chat"
    }
    assert {"day", "hour"} <= set(planned)

    await turn(db, world, "oi")
    await _confirm(db, world)
    for _ in range(2):
        await age_conversation(db, world, hours=7)
        await turn(db, world, "oi")
        await _confirm(db, world)
    assert await _card_confirmations(db, world) == 3

    after = {row.kind: row for row in await reminder_rows(db, world.appointment.id)}
    for kind in ("day", "hour"):
        row = after[kind]
        assert row.status == "pending" and row.invalidated_at is None
        assert row.due_at == planned[kind].due_at
    due = await reminder_engine.due_reminder_ids(START, limit=10)
    assert {after["day"].id, after["hour"].id} <= {rid for rid, _tenant in due}


# --- nothing booked, nothing to show -----------------------------------------------------------


@pytest.mark.parametrize("portal", [False, True])
@pytest.mark.parametrize(
    "status, start_at",
    [
        (AppointmentStatus.CANCELLED, START),
        (AppointmentStatus.SCHEDULED, datetime.now(UTC) - timedelta(days=2)),
    ],
)
async def test_no_upcoming_appointment_no_card(db, portal, status, start_at):  # noqa: F811
    world = await _world(
        db,
        portal=portal,
        status=status,
        start_at=start_at,
        last_inbound_at=datetime.now(UTC) - timedelta(hours=7),
    )

    await turn(db, world, "oi")
    if portal:
        await age_conversation(db, world, hours=7)
        await tasks.process_brain_message_enter({}, str(world.tenant.id), world.patient.external_id)

    assert await _cards(db, world) == []
    assert await chat_rows(db, world.appointment.id) == []


async def test_three_entry_confirmations_keep_the_day_reminder_buttons(db):  # noqa: F811
    # Review I1: repeated entry confirmations must not reach the agenda's cap of 2, which
    # would send the 1 d / 1 h reminders without Confirmar/Cancelar and hide the clinic's
    # confirm card.
    world = await _world(db, v2=True)
    await reminder_hooks.after_appointment_booked(world.appointment.id)
    await turn(db, world, "oi")
    await _confirm(db, world)
    for _ in range(2):
        await age_conversation(db, world, hours=7)
        await turn(db, world, "oi")
        await _confirm(db, world)

    appointment = await reload_appointment(db, world.appointment.id)
    assert (appointment.confirmation_count, appointment.card_confirmation_count) == (1, 3)
    assert reminder_schedule.display_state(appointment, []) == reminder_schedule.DISPLAY_CONFIRMED
    [day] = [row for row in await reminder_rows(db, world.appointment.id) if row.kind == "day"]
    job = await reminder_engine._prepare(day.id, 1, START - timedelta(hours=23))
    assert job is not None and job.with_prompt is True
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        current = await session.get(Appointment, world.appointment.id)
        assert clinic_action_notice.buttons_allowed(tenant, current, datetime.now(UTC))


async def test_a_scheduled_reminder_still_brings_the_second_agenda_confirmation(db):  # noqa: F811
    world = await _world(db, v2=True)
    await reminder_hooks.after_appointment_booked(world.appointment.id)
    await turn(db, world, "oi")
    await _confirm(db, world)
    await age_conversation(db, world, hours=7)
    await turn(db, world, "oi")
    await _confirm(db, world)
    [day] = [row for row in await reminder_rows(db, world.appointment.id) if row.kind == "day"]

    await tasks._handle_action_button(_reply(world), "remconfirm", str(day.id))

    appointment = await reload_appointment(db, world.appointment.id)
    assert (appointment.confirmation_count, appointment.card_confirmation_count) == (2, 3)


async def test_a_double_tap_on_the_same_showing_counts_once(db):  # noqa: F811
    world = await _world(db)
    await turn(db, world, "oi")
    await _confirm(db, world)
    await age_conversation(db, world, hours=7)
    await turn(db, world, "oi")
    await _confirm(db, world)
    await _confirm(db, world)

    appointment = await reload_appointment(db, world.appointment.id)
    assert (appointment.confirmation_count, appointment.card_confirmation_count) == (1, 2)
