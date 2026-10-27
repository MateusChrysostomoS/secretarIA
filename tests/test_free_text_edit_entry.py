"""Asking in free text to change the service / the appointment's data opens "Alterar Dados".

Owner, 2026-10-27 (browser proof 05c, finding 1): once the entry card stops (3
confirmations) a patient with a booked appointment who TYPES "quero mudar o serviço" or
"alterar dados" - straight at the menu or after "Outro" - must land on the same edit menu
the card's "Alterar Dados" opens ("Mudar serviço" included), never on the AI's reschedule
day picker. Portal and WhatsApp share the turn, so both are covered. A plain reschedule
request still goes to the AI exactly as before.
"""

from datetime import UTC, datetime, timedelta

import pytest

from secretaria.models import FlowState
from secretaria.services import appointment_edit as ae
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import (
    AGENT_REPLY,
    add_appointment,
    consent,
    get_conversation,
    set_conversation,
    turn,
    wire,
)
from tests._reminders_v2 import outbound_messages, seed_world

START = datetime(2030, 10, 15, 18, 20, tzinfo=UTC)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


async def _world(db, *, portal=False, **kw):  # noqa: F811
    world = await seed_world(
        db,
        start_at=kw.pop("start_at", START),
        professional_name="Dra. Ana",
        v2=False,
        last_inbound_at=datetime.now(UTC) - timedelta(minutes=5),
        **({"channel": CHANNEL_BRAIN_MESSAGE} if portal else {}),
        **kw,
    )
    await consent(db, world)
    return world


async def _outbound(db, world) -> list:  # noqa: F811
    """What reached the patient, oldest first (both channels record every outbound)."""
    return await outbound_messages(db, world.conversation.id)


async def _last_is_edit_menu(db, world) -> bool:  # noqa: F811
    last = (await _outbound(db, world))[-1]
    titles = [option["title"] for option in (last.interactive or {}).get("options", [])]
    return last.body.startswith("*Alterar Dados*") and ae.LABEL_EDIT_SERVICE in titles


async def _ai_answered(db, world) -> bool:  # noqa: F811
    return any(row.body == AGENT_REPLY for row in await _outbound(db, world))


# --- the pure matcher ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "quero mudar o serviço",
        "Quero trocar o servico da minha consulta",
        "alterar dados",
        "Alterar Dados",
        "preciso alterar os dados da consulta",
        "posso trocar o procedimento?",
        "queria mudar o tipo de consulta",
        "dá pra modificar o exame?",
    ],
)
def test_matcher_reads_a_service_or_data_change(text):
    assert ae.asks_to_edit_appointment(text)


@pytest.mark.parametrize(
    "text",
    [
        "quero remarcar",
        "quero mudar o horário",
        "trocar a data da consulta",
        "quero cancelar",
        "quais serviços vocês têm?",
        "oi",
        "",
        None,
    ],
)
def test_matcher_leaves_everything_else_alone(text):
    assert not ae.asks_to_edit_appointment(text)


# --- the turn (both channels) ----------------------------------------------------------------


@pytest.mark.parametrize("portal", [False, True])
async def test_free_text_after_outro_opens_the_edit_menu(db, portal):  # noqa: F811
    world = await _world(db, portal=portal)
    await set_conversation(db, world, flow_state=FlowState.MENU)
    await turn(db, world, "Outro")
    assert (await get_conversation(db, world)).flow_state == FlowState.LLM

    await turn(db, world, "quero mudar o serviço")

    assert await _last_is_edit_menu(db, world)
    assert not await _ai_answered(db, world)
    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.EDIT_BOOKING
    assert ae.EditDraft.from_json(conversation.flow_edit_draft).appointment_id == str(
        world.appointment.id
    )

    await turn(db, world, ae.LABEL_EDIT_SERVICE)

    assert ae.EDIT_KEPT not in [row.body for row in await _outbound(db, world)]
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING


@pytest.mark.parametrize("portal", [False, True])
async def test_free_text_at_the_menu_opens_the_edit_menu(db, portal):  # noqa: F811
    world = await _world(db, portal=portal)
    await set_conversation(db, world, flow_state=FlowState.MENU)

    await turn(db, world, "alterar dados")

    assert await _last_is_edit_menu(db, world)
    assert not await _ai_answered(db, world)
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING


async def test_a_reschedule_request_still_goes_to_the_ai(db):  # noqa: F811
    world = await _world(db)
    await set_conversation(db, world, flow_state=FlowState.LLM)

    await turn(db, world, "quero remarcar")

    assert await _ai_answered(db, world)
    assert (await get_conversation(db, world)).flow_state == FlowState.LLM


async def test_without_an_upcoming_appointment_the_ai_answers(db):  # noqa: F811
    world = await _world(db, start_at=datetime.now(UTC) - timedelta(days=2))
    await set_conversation(db, world, flow_state=FlowState.LLM)

    await turn(db, world, "quero mudar o serviço")

    assert await _ai_answered(db, world)
    assert (await get_conversation(db, world)).flow_state == FlowState.LLM


async def test_two_upcoming_appointments_are_left_to_the_ai(db):  # noqa: F811
    # Which one? The edit menu does not name the appointment, so it is never guessed.
    world = await _world(db)
    await add_appointment(db, world, start_at=START + timedelta(days=7))
    await set_conversation(db, world, flow_state=FlowState.LLM)

    await turn(db, world, "quero mudar o serviço")

    assert await _ai_answered(db, world)
