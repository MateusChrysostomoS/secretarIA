"""Taps on remconfirm| / remcancel| / remother| (TASK-032 R2): scoped to the patient."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.core import database as core_database
from secretaria.models import (
    AppointmentStatus,
    Conversation,
    Message,
    MessageDirection,
    Patient,
)
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import menu_label
from secretaria.workers import tasks
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._patching import workers_ns
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    WA_ID,
    FakeWhatsAppClient,
    add_reminder,
    fake_waba_token,
    get_reminder,
    reload_appointment,
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


def _future() -> datetime:
    return datetime.now(UTC) + timedelta(days=2)


def _reply(conversation_id, *, channel="whatsapp", patient_ref=WA_ID) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        channel=channel, conversation_id=conversation_id, patient_ref=patient_ref, inbound_body="x"
    )


async def _tap(conversation_id, action, reminder_id, **reply_kwargs) -> None:
    await tasks._handle_action_button(
        _reply(conversation_id, **reply_kwargs), action, str(reminder_id)
    )


def _texts() -> list[str]:
    return [item[2] for item in FakeWhatsAppClient.all_sent() if item[0] == "text"]


def _confirmed_text(world) -> str:
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    return f"Presença confirmada! ✅ Até {when}."


async def test_confirm_counts_one_and_answers_with_the_date(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)

    appointment = await reload_appointment(db, world.appointment.id)
    assert (appointment.confirmation_count, appointment.status) == (1, AppointmentStatus.CONFIRMED)
    assert _texts() == [_confirmed_text(world)]


async def test_the_same_message_confirmed_twice_counts_once(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)
    await _tap(world.conversation.id, "remconfirm", rid)

    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1
    assert _texts() == [_confirmed_text(world)] * 2


async def test_two_different_reminders_count_two(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    day = await add_reminder(db, world, kind="day")
    hour = await add_reminder(db, world, kind="hour")

    await _tap(world.conversation.id, "remconfirm", day)
    await _tap(world.conversation.id, "remconfirm", hour)

    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 2


async def test_cancel_after_confirm_never_lets_the_same_message_count_again(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)
    await _tap(world.conversation.id, "remcancel", rid)
    await _tap(world.conversation.id, "remconfirm", rid)

    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1
    assert (await get_reminder(db, rid)).answer == "confirm"


async def test_another_patients_reminder_is_not_found_and_counts_nothing(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    async with db() as session:
        intruder = Patient(
            id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900002222", name="João"
        )
        session.add(intruder)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id)
        session.add(conversation)
        await session.commit()

    await _tap(conversation.id, "remconfirm", rid, patient_ref="5511900002222")

    assert _texts() == ["Não encontrei essa consulta."]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0
    assert (await get_reminder(db, rid)).answer is None


async def test_another_clinics_reminder_is_not_found(db):  # noqa: F811
    mine = await seed_world(db, start_at=_future())
    theirs = await seed_world(db, start_at=_future(), phone_number_id="pnid-2")
    rid = await add_reminder(db, theirs)

    await _tap(mine.conversation.id, "remconfirm", rid)

    assert _texts() == ["Não encontrei essa consulta."]
    assert (await reload_appointment(db, theirs.appointment.id)).confirmation_count == 0


async def test_confirm_after_the_appointment_was_cancelled_does_nothing(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), status=AppointmentStatus.CANCELLED)
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)

    appointment = await reload_appointment(db, world.appointment.id)
    assert _texts() == ["Essa consulta não está mais ativa."]
    assert (appointment.confirmation_count, appointment.status) == (0, AppointmentStatus.CANCELLED)


async def test_a_tap_on_a_message_about_the_old_time_points_to_the_new_one(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world, appointment_start_at=world.start_at - timedelta(days=1))

    await _tap(world.conversation.id, "remconfirm", rid)

    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    assert _texts() == [
        f"Essa mensagem era sobre um horário antigo. Sua consulta agora é em {when}."
    ]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0


async def test_cancel_asks_for_confirmation_on_this_appointment(db):  # noqa: F811
    # TASK-032 R6 asks directly before cancelling the appointment.
    from secretaria.services.reminder_text import give_up_confirm_buttons
    from secretaria.workers.shared.reminder_actions import GIVE_UP_CONFIRM_TEXT

    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remcancel", rid)

    [(kind, _to, body, buttons)] = FakeWhatsAppClient.all_sent()
    assert (kind, body) == (
        "buttons",
        GIVE_UP_CONFIRM_TEXT.format(
            when=_format_appointment_when(world.start_at, "America/Sao_Paulo")
        ),
    )
    assert buttons == give_up_confirm_buttons(rid)
    row = await get_reminder(db, rid)
    assert row.answer == "cancel" and row.answered_at is not None
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_legacy_other_opens_the_edit_menu(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world.conversation.id, "remother", rid)

    card = FakeWhatsAppClient.all_sent()[0]
    assert card[0] == "list" and card[2].startswith("*Alterar Dados*")
    assert (await get_reminder(db, rid)).answer == "other"


async def test_a_portal_tap_is_answered_in_the_portal(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), channel=CHANNEL_BRAIN_MESSAGE)
    rid = await add_reminder(db, world)

    await _tap(
        world.conversation.id,
        "remconfirm",
        rid,
        channel=CHANNEL_BRAIN_MESSAGE,
        patient_ref=world.patient.external_id,
    )

    assert FakeWhatsAppClient.created == []
    async with db() as session:
        bodies = list(
            await session.scalars(
                select(Message.body).where(
                    Message.conversation_id == world.conversation.id,
                    Message.direction == MessageDirection.OUTBOUND,
                )
            )
        )
    assert bodies == [
        _confirmed_text(world),
        menu_label(world.tenant) + "\n(opções: 🗓️ Agendar, Outro)",
    ]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_a_malformed_reminder_id_is_ignored_silently(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())

    await tasks._handle_action_button(_reply(world.conversation.id), "remconfirm", "not-a-uuid")

    assert FakeWhatsAppClient.created == []


async def _retired_row(db, world):  # noqa: F811
    rid = await add_reminder(db, world, status="cancelled")
    return rid


async def test_confirm_on_a_retired_row_claims_nothing_and_writes_nothing(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await _retired_row(db, world)

    await _tap(world.conversation.id, "remconfirm", rid)

    assert _texts() == ["Essa consulta não está mais ativa."]
    row = await get_reminder(db, rid)
    assert (row.answer, row.answered_at) == (None, None)
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0


@pytest.mark.parametrize("action", ["remcancel", "remother"])
async def test_cancel_and_other_on_a_retired_row_write_nothing(db, action):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await _retired_row(db, world)

    await _tap(world.conversation.id, action, rid)

    assert _texts() == ["Essa consulta não está mais ativa."]
    row = await get_reminder(db, rid)
    assert (row.answer, row.answered_at) == (None, None)


async def test_confirm_on_an_invalidated_row_claims_nothing(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    async with db() as session:
        row = await session.get(type(await get_reminder(db, rid)), rid)
        row.invalidated_at = datetime.now(UTC)
        await session.commit()

    await _tap(world.conversation.id, "remconfirm", rid)

    assert _texts() == ["Essa consulta não está mais ativa."]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0
