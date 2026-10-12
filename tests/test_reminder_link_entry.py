"""Explicit authenticated reminder entry must present the card without losing drafts."""

from datetime import UTC, datetime, timedelta

from secretaria.models import Appointment, AppointmentStatus, FlowState, HandoverState, Tenant
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.workers.portal.inbound import process_brain_message_inbound
from secretaria.workers.portal.open import process_brain_message_enter
from secretaria.workers.shared.appointment_edit_apply import _row_draft
from secretaria.workers.shared.opening import _send_context_opening
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, get_conversation, set_conversation, wire
from tests._reminders_v2 import add_reminder, outbound_messages, seed_world
from tests.test_first_message_reminder import _reply


async def test_explicit_card_preserves_edit_in_progress(db, monkeypatch):  # noqa: F811
    wire(monkeypatch, db)
    world = await seed_world(
        db, channel=CHANNEL_BRAIN_MESSAGE, start_at=datetime.now(UTC) + timedelta(days=3)
    )
    await consent(db, world)
    draft = (
        _row_draft(world.appointment, world.tenant.timezone)
        .with_changes(
            insurance="Particular",
        )
        .to_json()
    )
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step="edit_confirm",
        flow_edit_draft=draft,
        flow_managing_appointment_id=world.appointment.id,
    )
    assert await _send_context_opening(
        _reply(world), world.tenant, world.patient.external_id, source="reminder_link"
    )
    saved = await get_conversation(db, world)
    assert saved.flow_edit_draft == draft
    assert saved.flow_state == FlowState.EDIT_BOOKING
    cards = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    assert cards[-1].body.startswith("LEMBRE-SE:")
    assert [o["title"] for o in cards[-1].interactive["options"]] == [
        "Confirmar",
        "Cancelar",
        "Alterar Dados",
    ]
    option = cards[-1].interactive["options"][-1]
    await process_brain_message_inbound(
        {},
        str(world.tenant.id),
        world.patient.external_id,
        text=option["title"],
        interactive_reply_id=option["id"],
    )
    assert (await get_conversation(db, world)).flow_edit_draft["current"] == draft["current"]


async def test_reminder_entry_ignores_quiet_window_and_reloads_without_duplicate(db, monkeypatch):  # noqa: F811
    wire(monkeypatch, db)
    world = await seed_world(
        db,
        channel=CHANNEL_BRAIN_MESSAGE,
        start_at=datetime.now(UTC) + timedelta(days=3),
        last_inbound_at=datetime.now(UTC),
    )
    await consent(db, world)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC))
    await process_brain_message_enter({}, str(world.tenant.id), world.patient.external_id)
    assert not await outbound_messages(db, world.conversation.id)
    context = {"source": "reminder_link", "reminder_id": str(rid)}
    for _ in range(2):
        await process_brain_message_enter(
            {}, str(world.tenant.id), world.patient.external_id, entry_context=context
        )
    cards = await outbound_messages(db, world.conversation.id)
    assert len(cards) == 1 and cards[0].body.startswith("LEMBRE-SE:")


async def test_forged_reminder_and_human_handover_cannot_show_card(db, monkeypatch):  # noqa: F811
    wire(monkeypatch, db)
    worlds = [
        await seed_world(
            db,
            channel=CHANNEL_BRAIN_MESSAGE,
            phone_number_id=None,
            start_at=datetime.now(UTC) + timedelta(days=3),
        )
        for _ in range(2)
    ]
    for world in worlds:
        await consent(db, world)
    own, other = worlds
    rid = await add_reminder(db, other, kind="day", due_at=datetime.now(UTC))
    context = {"source": "reminder_link", "reminder_id": str(rid)}
    await process_brain_message_enter(
        {}, str(own.tenant.id), own.patient.external_id, entry_context=context
    )
    assert not await outbound_messages(db, own.conversation.id)
    await set_conversation(db, other, handover_state=HandoverState.HUMAN_ACTIVE)
    await process_brain_message_enter(
        {}, str(other.tenant.id), other.patient.external_id, entry_context=context
    )
    assert not await outbound_messages(db, other.conversation.id)


async def test_cancelled_link_explains_status_without_an_actionable_old_card(db, monkeypatch):  # noqa: F811
    wire(monkeypatch, db)
    world = await seed_world(
        db, channel=CHANNEL_BRAIN_MESSAGE, start_at=datetime.now(UTC) + timedelta(days=3)
    )
    await consent(db, world)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC))
    async with db() as session:
        row = await session.get(Appointment, world.appointment.id)
        row.status = AppointmentStatus.CANCELLED
        await session.commit()
    await process_brain_message_enter(
        {},
        str(world.tenant.id),
        world.patient.external_id,
        entry_context={"source": "reminder_link", "reminder_id": str(rid)},
    )
    [message] = await outbound_messages(db, world.conversation.id)
    assert "não está mais ativa" in message.body
    assert message.interactive is None


async def test_explicit_link_keeps_reminders_off_policy(db, monkeypatch):  # noqa: F811
    wire(monkeypatch, db)
    world = await seed_world(
        db,
        channel=CHANNEL_BRAIN_MESSAGE,
        start_at=datetime.now(UTC) + timedelta(days=3),
        last_inbound_at=datetime.now(UTC),
    )
    await consent(db, world)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC))
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.reminders_v2_enabled = False
        await session.commit()
    await process_brain_message_enter(
        {},
        str(world.tenant.id),
        world.patient.external_id,
        entry_context={"source": "reminder_link", "reminder_id": str(rid)},
    )
    assert not await outbound_messages(db, world.conversation.id)


async def test_failed_card_delivery_releases_claim_and_next_entry_recovers(db, monkeypatch):  # noqa: F811
    from unittest.mock import AsyncMock

    from secretaria.services.channel_sender import BrainMessageSender

    wire(monkeypatch, db)
    world = await seed_world(
        db,
        channel=CHANNEL_BRAIN_MESSAGE,
        start_at=datetime.now(UTC) + timedelta(days=3),
        last_inbound_at=datetime.now(UTC),
    )
    await consent(db, world)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC))
    context = {"source": "reminder_link", "reminder_id": str(rid)}
    with monkeypatch.context() as failure:
        failure.setattr(
            BrainMessageSender,
            "send_buttons",
            AsyncMock(side_effect=ConnectionError("local DB offline")),
        )
        await process_brain_message_enter(
            {}, str(world.tenant.id), world.patient.external_id, entry_context=context
        )
    assert not await outbound_messages(db, world.conversation.id)
    await process_brain_message_enter(
        {}, str(world.tenant.id), world.patient.external_id, entry_context=context
    )
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_clinic_link_reload_keeps_silence_and_booking_in_progress(db, monkeypatch):  # noqa: F811
    """`clinic_link` is sent on every URL load (F5, back to the clinic): only a
    reminder link may override the quiet window / in-progress flow."""
    wire(monkeypatch, db)
    world = await seed_world(
        db,
        channel=CHANNEL_BRAIN_MESSAGE,
        start_at=datetime.now(UTC) + timedelta(days=3),
        last_inbound_at=datetime.now(UTC),
    )
    await consent(db, world)
    await set_conversation(
        db, world, flow_state=FlowState.SERVICE_CATALOG, flow_step="pick_slot"
    )
    for _ in range(3):
        await process_brain_message_enter(
            {},
            str(world.tenant.id),
            world.patient.external_id,
            entry_context={"source": "clinic_link"},
        )
    saved = await get_conversation(db, world)
    assert saved.flow_state == FlowState.SERVICE_CATALOG
    assert saved.flow_step == "pick_slot"
    assert not await outbound_messages(db, world.conversation.id)
