"""First upcoming-appointment cards use the actual reminder and its scoped actions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import update

from secretaria.models import Appointment, FlowState
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.workers import tasks
from secretaria.workers.shared import opening
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import (
    age_conversation,
    chat_rows,
    consent,
    get_conversation,
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


async def _world(db, *, portal=False, count=0, **kw):  # noqa: F811
    world = await seed_world(
        db,
        start_at=START,
        professional_name="Dra. Ana",
        requirements=["Trazer exames anteriores"],
        confirmation_count=count,
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


async def _card(db, world):  # noqa: F811
    rows = await outbound_messages(db, world.conversation.id)
    cards = [row for row in rows if row.interactive]
    assert len(cards) == 1
    card = cards[0]
    assert card.body.split("\n(opções:")[0] == BODY
    options = card.interactive["options"]
    assert [option["title"] for option in options] == ["Confirmar", "Cancelar", "Alterar Dados"]
    [chat] = await chat_rows(db, world.appointment.id)
    assert [option["id"] for option in options] == [
        f"remconfirm|{chat.id}",
        f"remcancel|{chat.id}",
        f"remedit|{chat.id}",
    ]
    return chat


@pytest.mark.parametrize("portal", [False, True])
async def test_first_consent_given_inbound_uses_reminder_without_legacy_greeting(db, portal):  # noqa: F811
    world = await _world(db, portal=portal)
    await turn(db, world, "oi")
    await _card(db, world)
    rows = await outbound_messages(db, world.conversation.id)
    assert len(rows) == 1
    assert "Vi aqui" not in rows[0].body


async def test_portal_context_entry_uses_identical_reminder_and_ids(db):  # noqa: F811
    world = await _world(db, portal=True)
    await opening._send_context_opening(_reply(world), world.tenant, None, waba_token="t")
    await _card(db, world)
    assert (await get_conversation(db, world)).flow_state == FlowState.MENU


async def test_actual_portal_enter_job_uses_that_same_card(db):  # noqa: F811
    world = await _world(db, portal=True, last_inbound_at=datetime.now(UTC) - timedelta(hours=7))
    await tasks.process_brain_message_enter({}, str(world.tenant.id), world.patient.external_id)
    await _card(db, world)


@pytest.mark.parametrize("action", ["remconfirm", "remcancel", "remedit"])
async def test_first_card_buttons_use_the_existing_reminder_flows(db, action):  # noqa: F811
    world = await _world(db)
    await turn(db, world, "oi")
    chat = await _card(db, world)
    await tasks._handle_action_button(_reply(world), action, str(chat.id))
    if action == "remconfirm":
        assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1
        assert [label for _id, label in sent()[-1][3]] == ["🗓️ Agendar", "Outro"]
    elif action == "remcancel":
        assert "Tem certeza" in sent()[-1][2]
        assert [label for _id, label in sent()[-1][3]] == ["Sim, cancelar", "Manter consulta"]
    else:
        assert sent()[-1][0] == "list" and sent()[-1][2].startswith("*Alterar Dados*")
        assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING


@pytest.mark.parametrize("portal", [False, True])
async def test_explicit_first_card_does_not_increase_confirmations_past_two(db, portal):  # noqa: F811
    world = await _world(db, portal=portal, count=2)
    if portal:
        await opening._send_context_opening(_reply(world), world.tenant, None, waba_token="t")
    else:
        await turn(db, world, "oi")
    chat = await _card(db, world)
    await tasks._handle_action_button(_reply(world), "remconfirm", str(chat.id))
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 2


async def test_superseded_portal_entry_sends_no_card_or_reminder_row(db):  # noqa: F811
    world = await _world(db, portal=True)
    await opening._send_context_opening(
        _reply(world),
        world.tenant,
        None,
        waba_token="t",
        still_current=AsyncMock(return_value=False),
    )
    assert not await outbound_messages(db, world.conversation.id)
    assert not await chat_rows(db, world.appointment.id)


async def test_a_foreign_conversation_cannot_resolve_medical_details(db):  # noqa: F811
    world = await _world(db, portal=True)
    other = await _world(db, portal=True, phone_number_id=f"pn-{uuid4()}")
    result = await opening.resolve_opening_message(world.conversation.id, other.tenant)
    assert result is None


async def test_pending_privacy_still_gets_identification_before_medical_details(db):  # noqa: F811
    world = await seed_world(db, start_at=START, professional_name="Dra. Ana")
    await turn(db, world, "oi")
    assert not await chat_rows(db, world.appointment.id)
    assert not any(
        "LEMBRE-SE:" in row.body for row in await outbound_messages(db, world.conversation.id)
    )


async def test_a_tap_after_the_opening_is_delivered_is_not_cleared(db, monkeypatch):  # noqa: F811
    world = await _world(db, portal=True)
    send = opening._send_reminder_opening

    async def then_tap(*args, **kwargs):
        rendered = await send(*args, **kwargs)
        [chat] = await chat_rows(db, world.appointment.id)
        await tasks._handle_action_button(_reply(world), "remedit", str(chat.id))
        return rendered

    monkeypatch.setattr(opening, "_send_reminder_opening", then_tap)
    await opening._send_context_opening(_reply(world), world.tenant, None, waba_token="t")
    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.EDIT_BOOKING
    assert conversation.flow_edit_draft["appointment_id"] == str(world.appointment.id)


async def test_a_cancelled_appointment_between_resolve_and_send_is_not_shown(db, monkeypatch):  # noqa: F811
    world = await _world(db, portal=True)
    resolve = opening.resolve_opening_message

    async def then_cancel(*args):
        from secretaria.models import AppointmentStatus

        resolved = await resolve(*args)
        async with db() as session:
            await session.execute(
                update(Appointment)
                .where(Appointment.id == world.appointment.id)
                .values(status=AppointmentStatus.CANCELLED)
            )
            await session.commit()
        return resolved

    monkeypatch.setattr(opening, "resolve_opening_message", then_cancel)
    await opening._send_context_opening(_reply(world), world.tenant, None, waba_token="t")
    assert not await chat_rows(db, world.appointment.id)
    assert not any(
        "LEMBRE-SE:" in row.body for row in await outbound_messages(db, world.conversation.id)
    )


async def test_staff_origin_attendee_in_the_first_card_is_masked_for_model_history(
    db,  # noqa: F811
    monkeypatch,
):
    from secretaria.services import pii_pseudonymization as pii

    monkeypatch.setattr(pii, "async_session_factory", db)
    world = await _world(db, portal=True, attendee_name="Ana Souza")
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(conversation_id=None)
        )
        await session.commit()
    await opening._send_context_opening(_reply(world), world.tenant, None, waba_token="t")
    [card] = await outbound_messages(db, world.conversation.id)
    assert "A consulta de Ana Souza" in card.body
    masker = await pii.load_pseudonymizer(world.conversation.id)
    assert "Ana Souza" not in masker.scrub(card.body)


async def test_a_real_edit_during_preparation_supersedes_the_portal_entry(db, monkeypatch):  # noqa: F811
    from secretaria.workers.shared import reminder_opening

    world = await _world(db, portal=True)
    await opening._send_context_opening(_reply(world), world.tenant, None, waba_token="t")
    [chat] = await chat_rows(db, world.appointment.id)
    await age_conversation(db, world, hours=7)
    prepare = reminder_opening._prepare_opening

    async def prepare_then_edit(*args, **kwargs):
        prepared = await prepare(*args, **kwargs)
        await tasks.process_brain_message_inbound(
            {},
            str(world.tenant.id),
            world.patient.external_id,
            "Alterar Dados",
            interactive_reply_id=f"remedit|{chat.id}",
        )
        return prepared

    monkeypatch.setattr(reminder_opening, "_prepare_opening", prepare_then_edit)
    await tasks.process_brain_message_enter({}, str(world.tenant.id), world.patient.external_id)
    rows = await outbound_messages(db, world.conversation.id)
    assert rows[-1].body.startswith("*Alterar Dados*")
    assert sum(row.body.startswith("LEMBRE-SE:") for row in rows) == 1
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING
