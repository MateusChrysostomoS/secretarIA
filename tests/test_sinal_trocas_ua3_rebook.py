"""TASK-085 R / UA3 - "Outro horário" end to end: day -> time -> Confirmar -> the old consultation
moves to the new slot (D-R11, mapa-u §1.5), on WhatsApp and on the Portal, over several
MANAGE_BOOKING turns.

UA1's order at Confirmar (results/UA1-implementer.md, "Ordem de chamada"): 1) the old event is gone
(`ensure_old_event_gone`, clean session; False -> nothing created, "tente de novo"); 2) the window
(`slot_taken_at_confirm`, nothing ignored); 3) the event `difmove<hex>` OUTSIDE any transaction;
4) one transaction: start/end/status + `finish_rebook(source="patient")`; 5) False -> rollback,
`discard_event`, `choice_answer_text(already_decided)`. Never `register_reschedule` (§7.6); the
A7 notices and the reminders follow as in any reschedule. Doubles and the `ua2` stub:
tests/test_sinal_trocas_ua3_patient.py.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from secretaria.models import (
    AppointmentStatus,
    FlowState,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    OLD_SLOT_REBOOKED,
    OLD_SLOT_RELEASED,
    OLD_SLOT_RELEASING,
    STATUS_AWAITING_CHOICE,
    STATUS_CANCELLED,
    STATUS_OLD_SLOT_LOST,
    STATUS_REBOOKED,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import ATTENTION_OPEN, ATTENTION_RESOLVED
from secretaria.services import flow_router as fr, reminder_hooks
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.payments import old_slot, service_change
from secretaria.services.payments.service_change import ChoiceOutcome
from secretaria.workers import orchestrator, tasks
from secretaria.workers.portal.inbound import process_brain_message_inbound
from secretaria.workers.shared import flow_runner
from secretaria.workers.shared.handover import CALENDAR_UNAVAILABLE_MESSAGE
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import get_conversation, turn
from tests._reminders_v2 import outbound_messages, reload_appointment
from tests.test_sinal_trocas_ua3_patient import (  # noqa: F401 - fixtures
    OTHER,
    Agenda,
    adjustment_of,
    agenda,  # noqa: F811
    holds,
    portal_card,
    reply_of,
    ua2,  # noqa: F811
    ua3_world,
    wa_messages,
)

CONFIRM = ("confirm_yes", "✅ Confirmar")


@pytest.fixture
def spies(monkeypatch, agenda):  # noqa: F811
    """A7 notices, reminder re-plans and the ORDER of `ensure_old_event_gone` vs the agenda."""
    calls = SimpleNamespace(changes=[], replans=[], ensure=[])

    async def _change(redis, tenant_id, appointment_id, change, **kwargs):
        calls.changes.append((change, appointment_id, kwargs.get("new_start_at")))

    async def _moved(appointment_id, *, now=None):
        calls.replans.append(appointment_id)
        return 0

    real_ensure = old_slot.ensure_old_event_gone

    async def _ensure(session, *, tenant_id, adjustment_id):
        # A clean session: nothing pending when it is handed over.
        calls.ensure.append(not (session.new or session.dirty or session.deleted))
        agenda.log.append("ensure")
        return await real_ensure(session, tenant_id=tenant_id, adjustment_id=adjustment_id)

    monkeypatch.setattr(flow_runner, "enqueue_appointment_change_notification", _change)
    monkeypatch.setattr(reminder_hooks, "after_appointment_rescheduled", _moved)
    monkeypatch.setattr(old_slot, "ensure_old_event_gone", _ensure)
    return calls


# --------------------------------------------------------------------------------------------
# Drivers: WhatsApp (the webhook's action tap, then whole turns) and the Portal (card taps)
# --------------------------------------------------------------------------------------------


def _last_list_rows() -> list[tuple]:
    return [item for item in wa_messages() if item[0] == "list"][-1][4]


async def wa_rebook_to_first_slot(db, world, adjustment_id):  # noqa: F811
    """Tap "Outro horário", then the first day, then the first time: the confirmation card."""
    await tasks._handle_action_button(reply_of(world), OTHER, str(adjustment_id))
    day = _last_list_rows()[0]
    await turn(db, world, day[1], interactive_reply_id=day[0])
    slot = _last_list_rows()[0]
    await turn(db, world, slot[1], interactive_reply_id=slot[0])
    assert (await get_conversation(db, world)).flow_step == fr.STEP_MANAGE_CONFIRM
    return datetime.fromisoformat(slot[0].split("|", 1)[1]).replace(
        tzinfo=fr.ZoneInfo("America/Sao_Paulo")
    )


async def wa_confirm(db, world):  # noqa: F811
    await turn(db, world, CONFIRM[1], interactive_reply_id=CONFIRM[0])


async def portal_options(db, world) -> list[dict]:  # noqa: F811
    cards = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    return cards[-1].interactive["options"]


async def portal_tap(db, world, option: dict) -> None:  # noqa: F811
    await process_brain_message_inbound(
        {"redis": None},
        str(world.tenant.id),
        world.patient.external_id,
        text=option["title"],
        interactive_reply_id=option["id"],
    )


async def portal_rebook_to_first_slot(db, world, adjustment_id):  # noqa: F811
    await portal_card(world, "ocupado", service_change.lost_slot_buttons(adjustment_id))
    await portal_tap(db, world, (await portal_options(db, world))[0])  # "Outro horário"
    day = (await portal_options(db, world))[0]
    assert day["id"].startswith("day|")
    await portal_tap(db, world, day)
    slot = (await portal_options(db, world))[0]
    assert slot["id"].startswith("slot|")
    await portal_tap(db, world, slot)
    assert (await get_conversation(db, world)).flow_step == fr.STEP_MANAGE_CONFIRM
    return datetime.fromisoformat(slot["id"].split("|", 1)[1]).replace(
        tzinfo=fr.ZoneInfo("America/Sao_Paulo")
    )


async def portal_confirm(db, world):  # noqa: F811
    confirm = next(o for o in await portal_options(db, world) if "Confirmar" in o["title"])
    await portal_tap(db, world, confirm)


async def _lose_old_slot(db, world, adjustment_id):  # noqa: F811
    """The silence found the old slot taken (UA1's `mark_old_slot_lost`): old_slot_lost + item."""
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        adjustment = await session.get(DepositAdjustment, adjustment_id)
        assert await old_slot.mark_old_slot_lost(
            session, tenant=tenant, adjustment=adjustment, now=datetime.now(UTC)
        )
        await session.commit()


async def _items(db) -> list[PaymentAttentionItem]:  # noqa: F811
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


async def _deposit(db) -> PixDeposit:  # noqa: F811
    async with db() as session:
        return await session.scalar(select(PixDeposit))


def _move_id(adjustment_id, calendar, start, salt=0) -> str:
    return fr.patient_rebook_event_id(
        adjustment_id, calendar=calendar, start=start, end=start + timedelta(minutes=30), salt=salt
    )


def _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id, salt=0):  # noqa: F811
    event_id = _move_id(adjustment_id, agenda, new_start, salt)
    assert event_id.startswith(
        old_slot.rebook_event_id(adjustment_id, calendar=agenda, source="patient")
    )
    created = agenda.created[-1]
    assert created["id"] == event_id and event_id in agenda.live()
    assert created["start"] == new_start and created["end"] == new_start + timedelta(minutes=30)
    assert created["summary"] == "Consulta - Maria"
    assert row.google_event_id == event_id
    assert row.google_event_link == f"https://cal/{event_id}"
    # As every reschedule writes it (`flow_runner`); SQLite keeps the wall time given, without
    # the offset (Postgres stores the instant).
    assert row.start_at.replace(tzinfo=None) == new_start.replace(tzinfo=None)
    assert row.status == AppointmentStatus.RESCHEDULED
    assert row.appointment_type == "Consulta"  # the same service, doctor and convênio
    assert (adjustment.status, adjustment.old_slot) == (STATUS_REBOOKED, OLD_SLOT_REBOOKED)
    assert adjustment.resolved_at is not None


# --------------------------------------------------------------------------------------------
# The whole rebook, both channels
# --------------------------------------------------------------------------------------------


@pytest.mark.parametrize("lost", [False, True])
async def test_whatsapp_other_time_moves_the_old_consultation(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
    lost,
):
    world, adjustment_id = await ua3_world(db)
    if lost:
        await _lose_old_slot(db, world, adjustment_id)
        assert [item.status for item in await _items(db)] == [ATTENTION_OPEN]

    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    await wa_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    adjustment = await adjustment_of(db, adjustment_id)
    _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id)
    assert wa_messages()[-1][2].startswith("Pronto! Sua consulta foi remarcada.")
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE
    # UA1's order: the old event is gone (clean session) BEFORE the window and the create.
    assert agenda.log[-3:] == ["ensure", "conflict", "create"]
    assert spies.ensure == [True]
    # The sinal stays on the same row; "Outro horário" never counts as a reschedule.
    deposit = await _deposit(db)
    assert deposit.appointment_id == world.appointment.id
    assert (deposit.reschedule_count or 0) == 0
    # As any reschedule: A7 notice + reminders re-planned.
    assert spies.changes == [("rescheduled", world.appointment.id, new_start)]
    assert spies.replans == [world.appointment.id]
    assert await holds(db) == []
    if lost:  # the clinic's "remarcar" item closes itself
        [item] = await _items(db)
        assert (item.status, item.resolution_action) == (ATTENTION_RESOLVED, "rebooked")


@pytest.mark.parametrize("lost", [False, True])
async def test_portal_other_time_moves_the_old_consultation(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
    lost,
):
    world, adjustment_id = await ua3_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    if lost:
        await _lose_old_slot(db, world, adjustment_id)

    new_start = await portal_rebook_to_first_slot(db, world, adjustment_id)
    await portal_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    adjustment = await adjustment_of(db, adjustment_id)
    _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id)
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert any(body.startswith("Certo! Escolha o novo dia") for body in bodies)
    assert bodies[-1].startswith("Pronto! Sua consulta foi remarcada.")
    assert spies.changes == [("rescheduled", world.appointment.id, new_start)]
    assert (await _deposit(db)).reschedule_count in (0, None)


async def test_every_manage_turn_keeps_listing_the_consultation_being_moved(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
    monkeypatch,
):
    """mapa-u §6 risk 7: without `rebooking_id` on EVERY MANAGE_BOOKING turn the flow would fall
    into the AI. Only those turns ask for it."""
    world, adjustment_id = await ua3_world(db)
    asked: list = []
    real_load = orchestrator.load_upcoming_appointments

    async def _load(session, tenant_id, patient_id, *, now=None, rebooking_id=None):
        asked.append(rebooking_id)
        return await real_load(session, tenant_id, patient_id, now=now, rebooking_id=rebooking_id)

    monkeypatch.setattr(orchestrator, "load_upcoming_appointments", _load)

    await wa_rebook_to_first_slot(db, world, adjustment_id)
    await wa_confirm(db, world)

    assert asked == [world.appointment.id] * 3  # day, time, Confirmar
    assert not any(item[2] == "Resposta da IA." for item in wa_messages())
    await turn(db, world, "Remarcar")  # back in IDLE: today's call, no rebooking id
    assert len(asked) == 4 and asked[-1] is None


# --------------------------------------------------------------------------------------------
# Confirmar: UA1's order and each way it can stop
# --------------------------------------------------------------------------------------------


async def test_old_event_not_confirmed_gone_creates_nothing_and_asks_again(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
    monkeypatch,
):
    world, adjustment_id = await ua3_world(db)
    await wa_rebook_to_first_slot(db, world, adjustment_id)
    answers = [False]

    async def _ensure(session, *, tenant_id, adjustment_id):
        agenda.log.append("ensure")
        return answers.pop(0) if answers else True

    monkeypatch.setattr(old_slot, "ensure_old_event_gone", _ensure)

    await wa_confirm(db, world)

    assert agenda.created == [] and agenda.log[-1] == "ensure"
    text, card = wa_messages()[-2:]
    assert text[2] == fr.REBOOK_RETRY_TEXT == "Não consegui agora, tente de novo em instantes."
    assert card[0] == "buttons" and card[2].startswith("Remarcar para:")
    conversation = await get_conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (
        FlowState.MANAGE_BOOKING,
        fr.STEP_MANAGE_CONFIRM,
    )
    row = await reload_appointment(db, world.appointment.id)
    assert (row.google_event_id, row.status) == ("evt-old", AppointmentStatus.SCHEDULED)
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE

    await wa_confirm(db, world)  # a moment later: done
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_REBOOKED


async def test_a_pending_delete_of_the_old_event_finishes_before_the_new_one(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
):
    """`old_slot='releasing'` (the vacate's delete failed): Confirmar deletes the old event
    first; `finish_rebook` only accepts `released`."""
    world, adjustment_id = await ua3_world(db, old_slot_state=OLD_SLOT_RELEASING)
    agenda.add("evt-old", world.start_at, world.start_at + timedelta(minutes=30))
    await wa_rebook_to_first_slot(db, world, adjustment_id)

    await wa_confirm(db, world)

    assert agenda.cancelled[0] == "evt-old" and "evt-old" not in agenda.live()
    assert agenda.log.index("cancel") < agenda.log.index("create")
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_REBOOKED


async def test_a_new_slot_taken_at_confirm_is_offered_again(db, agenda, ua2, spies):  # noqa: F811
    world, adjustment_id = await ua3_world(db)
    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    agenda.add("evt-someone", new_start, new_start + timedelta(minutes=30))

    await wa_confirm(db, world)

    assert agenda.created == []
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-old"
    conversation = await get_conversation(db, world)
    assert conversation.flow_state == FlowState.MANAGE_BOOKING  # still choosing
    assert spies.changes == []


async def test_a_change_decided_meanwhile_undoes_the_move_and_discards_the_event(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
):
    """`finish_rebook` False (the clinic cancelled / a payment landed between the create and the
    write): nothing of the move stands, the created event goes, one line, the flow ends."""
    world, adjustment_id = await ua3_world(db)
    await wa_rebook_to_first_slot(db, world, adjustment_id)

    async def _decided_elsewhere():
        async with db() as session:
            await session.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == adjustment_id)
                .values(status=STATUS_CANCELLED)
            )
            await session.commit()

    agenda.on_create = _decided_elsewhere

    await wa_confirm(db, world)

    [created] = agenda.created
    assert created["id"] in agenda.cancelled and created["id"] not in agenda.live()
    row = await reload_appointment(db, world.appointment.id)
    assert (row.google_event_id, row.status) == ("evt-old", AppointmentStatus.SCHEDULED)
    assert row.start_at.replace(tzinfo=UTC) == world.start_at
    assert wa_messages()[-1][2] == service_change.choice_answer_text(
        ChoiceOutcome(kind="already_decided")
    )
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE
    assert spies.changes == [] and spies.replans == []


async def test_a_failed_write_discards_the_event_and_hands_over(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
    monkeypatch,
):
    world, adjustment_id = await ua3_world(db)
    await wa_rebook_to_first_slot(db, world, adjustment_id)

    async def _boom(*args, **kwargs):
        raise RuntimeError("database blip")

    monkeypatch.setattr(old_slot, "finish_rebook", _boom)

    await wa_confirm(db, world)

    [created] = agenda.created
    assert created["id"] in agenda.cancelled
    row = await reload_appointment(db, world.appointment.id)
    assert (row.google_event_id, row.status) == ("evt-old", AppointmentStatus.SCHEDULED)
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    texts = [item[2] for item in wa_messages()]
    assert CALENDAR_UNAVAILABLE_MESSAGE in texts
    assert not any(text.startswith("Pronto! Sua consulta foi remarcada.") for text in texts)


async def test_a_spent_id_of_the_window_is_retried_with_a_salt(db, agenda, ua2, spies):  # noqa: F811
    """UA3 review I1: an earlier attempt at THIS window created and deleted its event (Google
    never frees the id: "event id is taken"); the next id of the window is used."""
    world, adjustment_id = await ua3_world(db)
    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    agenda.add(
        _move_id(adjustment_id, agenda, new_start),
        new_start,
        new_start + timedelta(minutes=30),
        status="cancelled",
    )

    await wa_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    adjustment = await adjustment_of(db, adjustment_id)
    _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id, salt=1)


async def test_the_secretaria_agenda_spent_row_is_retried_too(db, agenda, ua2, spies):  # noqa: F811
    """The own agenda hands the deleted row back (`status="cancelled"`) instead of raising."""
    world, adjustment_id = await ua3_world(db)
    agenda.local_style = True
    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    for salt in (0, 1):
        agenda.add(
            _move_id(adjustment_id, agenda, new_start, salt),
            new_start,
            new_start + timedelta(minutes=30),
            status="cancelled",
        )

    await wa_confirm(db, world)

    adjustment = await adjustment_of(db, adjustment_id)
    row = await reload_appointment(db, world.appointment.id)
    _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id, salt=2)


async def test_every_id_of_the_window_spent_hands_over(db, agenda, ua2, spies):  # noqa: F811
    """Bounded: after REBOOK_ID_TRIES dead ids a person takes over; nothing is written."""
    world, adjustment_id = await ua3_world(db)
    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    for salt in range(fr.REBOOK_ID_TRIES):
        agenda.add(
            _move_id(adjustment_id, agenda, new_start, salt),
            new_start,
            new_start + timedelta(minutes=30),
            status="cancelled",
        )

    await wa_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-old" and agenda.created == []
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    assert CALENDAR_UNAVAILABLE_MESSAGE in [item[2] for item in wa_messages()]


def _fail_first_write(monkeypatch) -> None:
    real = old_slot.finish_rebook
    calls = {"n": 0}

    async def _once(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("database blip")
        return await real(*args, **kwargs)

    monkeypatch.setattr(old_slot, "finish_rebook", _once)


async def test_the_same_card_after_a_compensated_failure_rebooks(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
    monkeypatch,
):
    """Reviewer probe (I1): the write failed and its event was deleted; once the bot has the
    conversation back, Confirmar on the SAME card (same window) must not dead-end on the spent
    id."""
    from secretaria.models import HandoverState
    from tests._reminders_r3 import set_conversation

    world, adjustment_id = await ua3_world(db)
    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    _fail_first_write(monkeypatch)
    await wa_confirm(db, world)  # compensated: the event is gone, a person is told
    assert _move_id(adjustment_id, agenda, new_start) in agenda.cancelled
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    await set_conversation(db, world, handover_state=HandoverState.BOT_ACTIVE)
    assert (await get_conversation(db, world)).flow_step == fr.STEP_MANAGE_CONFIRM

    await wa_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    adjustment = await adjustment_of(db, adjustment_id)
    _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id, salt=1)


async def test_other_time_again_after_the_handover_is_released_rebooks(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
    monkeypatch,
):
    """Reviewer probe (I1): handed over after a compensated failure, given back to the bot, the
    patient taps "Outro horário" again and picks the same time: it stands."""
    from secretaria.models import HandoverState
    from tests._reminders_r3 import set_conversation

    world, adjustment_id = await ua3_world(db)
    await wa_rebook_to_first_slot(db, world, adjustment_id)
    _fail_first_write(monkeypatch)
    await wa_confirm(db, world)
    await set_conversation(db, world, handover_state=HandoverState.BOT_ACTIVE)

    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    await wa_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    adjustment = await adjustment_of(db, adjustment_id)
    _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id, salt=1)


async def test_a_leftover_event_at_one_window_never_binds_another(db, agenda, ua2, spies):  # noqa: F811
    """Reviewer probe (I1): a `difmove` left live at W (a crash between create and commit) and a
    confirm at W' - the row moves to a NEW event at W', never the one at W."""
    world, adjustment_id = await ua3_world(db)
    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    leftover_at = new_start + timedelta(hours=1)  # W
    leftover = _move_id(adjustment_id, agenda, leftover_at)
    agenda.add(leftover, leftover_at, leftover_at + timedelta(minutes=30))

    await wa_confirm(db, world)  # W' = new_start

    row = await reload_appointment(db, world.appointment.id)
    adjustment = await adjustment_of(db, adjustment_id)
    _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id)
    assert row.google_event_id != leftover


async def test_an_answer_at_another_window_is_never_bound_nor_deleted(
    db,  # noqa: F811
    agenda,  # noqa: F811
    ua2,  # noqa: F811
    spies,
):
    """Belt: the agenda answers OUR id with another window (somebody's - a twin's?): nothing
    written, nothing deleted, the patient is asked to try again."""
    world, adjustment_id = await ua3_world(db)
    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    elsewhere = new_start + timedelta(days=1)
    wanted = _move_id(adjustment_id, agenda, new_start)
    agenda.add(wanted, elsewhere, elsewhere + timedelta(minutes=30))

    await wa_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-old"
    assert wanted in agenda.live() and agenda.cancelled == []
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    text, card = wa_messages()[-2:]
    assert text[2] == fr.REBOOK_RETRY_TEXT and card[2].startswith("Remarcar para:")
    assert (await get_conversation(db, world)).flow_step == fr.STEP_MANAGE_CONFIRM


def test_the_patient_rebook_id_follows_the_hub_scheme():
    """Same shape as the hub's (`difhub` + 16 hex of the window): prefix kept, `loc_` on the own
    agenda, one window per id, base32hex only, <= 64 chars."""
    from secretaria.services.local_calendar import LocalCalendarService

    adjustment_id = uuid4()
    start = datetime(2030, 10, 20, 13, 0, tzinfo=UTC)
    end = start + timedelta(minutes=30)
    google = fr.patient_rebook_event_id(adjustment_id, calendar=None, start=start, end=end)
    assert google.startswith(f"difmove{adjustment_id.hex}") and len(google) == 7 + 32 + 16
    assert set(google) <= set("0123456789abcdefghijklmnopqrstuv")
    # The same instant on another clock is the same id; another window or salt is another id.
    local_clock = start.astimezone(fr.ZoneInfo("America/Sao_Paulo"))
    assert google == fr.patient_rebook_event_id(
        adjustment_id, calendar=None, start=local_clock, end=end
    )
    assert google != fr.patient_rebook_event_id(
        adjustment_id, calendar=None, start=start + timedelta(minutes=30), end=end
    )
    assert google != fr.patient_rebook_event_id(
        adjustment_id, calendar=None, start=start, end=end, salt=1
    )
    local = fr.patient_rebook_event_id(
        adjustment_id, calendar=object.__new__(LocalCalendarService), start=start, end=end
    )
    assert local == f"loc_{google}" and len(local) <= 64


async def test_the_agenda_down_at_the_create_hands_over(db, agenda, ua2, spies):  # noqa: F811
    from secretaria.services.calendar import CalendarUnavailableError

    world, adjustment_id = await ua3_world(db)
    await wa_rebook_to_first_slot(db, world, adjustment_id)
    agenda.fail_create = CalendarUnavailableError("down")

    await wa_confirm(db, world)

    assert agenda.created == []
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == "evt-old"
    assert CALENDAR_UNAVAILABLE_MESSAGE in [item[2] for item in wa_messages()]


async def test_a_twin_event_the_row_points_at_is_kept(db, agenda):  # noqa: F811
    world, _adjustment_id = await ua3_world(db)
    async with db() as session:
        row = await session.get(type(world.appointment), world.appointment.id)
        row.google_event_id = "difmovewon"
        await session.commit()
    tenant = SimpleNamespace(id=world.tenant.id)

    await flow_runner._discard_rebook_event(
        tenant, {"event_id": "difmovewon", "calendar": agenda, "adjustment_id": uuid4()}
    )
    assert agenda.cancelled == []
    await flow_runner._discard_rebook_event(
        tenant, {"event_id": "difmovelost", "calendar": agenda, "adjustment_id": uuid4()}
    )
    assert agenda.cancelled == ["difmovelost"]


async def test_cancelling_at_the_card_leaves_the_change_open(db, agenda, ua2, spies):  # noqa: F811
    """ "❌ Cancelar" on the confirmation card: back to the menu, nothing moved - the choice is
    still open (the sweep's silence rule decides it later)."""
    world, adjustment_id = await ua3_world(db)
    await wa_rebook_to_first_slot(db, world, adjustment_id)

    await turn(db, world, "❌ Cancelar", interactive_reply_id="confirm_no")

    assert agenda.created == []
    assert (await adjustment_of(db, adjustment_id)).status == STATUS_AWAITING_CHOICE
    assert (await get_conversation(db, world)).flow_state == FlowState.MENU


# --------------------------------------------------------------------------------------------
# The router alone: the rebook branch vs. today's reschedule
# --------------------------------------------------------------------------------------------


def _confirm_conversation(managing_id, slot="2030-10-20T10:00"):
    return SimpleNamespace(
        id=uuid4(),
        flow_state=FlowState.MANAGE_BOOKING,
        flow_step=fr.STEP_MANAGE_CONFIRM,
        flow_managing_appointment_id=managing_id,
        flow_selected_day="2030-10-20",
        flow_selected_slot=slot,
        flow_selected_type=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_selected_unit_id=None,
        flow_attendee_name=None,
        flow_draft=None,
        flow_replaces_appointment_id=None,
        flow_edit_draft=None,
    )


def _appt(**extra) -> dict:
    start = datetime(2030, 10, 15, 13, 0, tzinfo=UTC)
    return {
        "id": str(uuid4()),
        "google_event_id": "evt-old",
        "appointment_type": "Consulta",
        "insurance": None,
        "attendee_name": None,
        "start_at": start,
        "end_at": start + timedelta(minutes=30),
        "professional_id": None,
        **extra,
    }


def _router_tenant():
    return SimpleNamespace(
        initial_flows={},
        appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
        appointment_duration_min=30,
        business_hours={},
        timezone="America/Sao_Paulo",
        address=None,
        collect_insurance=False,
        insurance_mode=None,
        insurances=[],
        insurance_plans=None,
        insurance_accepted_by={},
        reminders_v2_enabled=False,
    )


async def test_router_reschedule_without_rebook_still_moves_the_event(monkeypatch):
    """Characterization: a visible consultation moves its OWN event (update), as today."""
    cal = Agenda()
    cal.add(
        "evt-old",
        datetime(2030, 10, 15, 13, 0, tzinfo=UTC),
        datetime(2030, 10, 15, 13, 30, tzinfo=UTC),
    )
    appt = _appt()

    async def _never(*args, **kwargs):
        raise AssertionError("no database step on today's reschedule")

    monkeypatch.setattr(fr, "_ensure_rebook_old_event_gone", _never)
    result = await fr._manage_reschedule(
        _confirm_conversation(appt["id"]), _router_tenant(), cal, "Confirmar", [appt]
    )
    assert cal.created == [] and [u[0] for u in cal.updated] == ["evt-old"]
    assert set(result.appointment_reschedule) == {"google_event_id", "start_at", "end_at"}


async def test_router_rebook_creates_the_new_event_under_its_own_id(monkeypatch):
    cal = Agenda()
    adjustment_id, tenant_id = uuid4(), uuid4()
    appt = _appt(rebook_adjustment_id=str(adjustment_id), rebook_tenant_id=str(tenant_id))
    seen = []

    async def _ensure(tid, aid):
        seen.append((tid, aid))
        return True

    monkeypatch.setattr(fr, "_ensure_rebook_old_event_gone", _ensure)
    result = await fr._manage_reschedule(
        _confirm_conversation(appt["id"]),
        _router_tenant(),
        cal,
        "Confirmar",
        [appt],
        patient_name="Maria",
    )
    assert seen == [(tenant_id, adjustment_id)]
    start = datetime(2030, 10, 20, 10, 0, tzinfo=fr.ZoneInfo("America/Sao_Paulo"))
    event_id = fr.patient_rebook_event_id(
        adjustment_id, calendar=cal, start=start, end=start + timedelta(minutes=30)
    )
    [created] = cal.created
    assert created["id"] == event_id and cal.updated == []
    assert created["summary"] == "Consulta - Maria"
    assert "Consulta" in created["description"]
    resched = result.appointment_reschedule
    assert resched["google_event_id"] == "evt-old"  # the row is found by the event it had
    assert resched["rebook"] == {
        "adjustment_id": adjustment_id,
        "event_id": event_id,
        "event_link": f"https://cal/{event_id}",
        "calendar": cal,
    }
    assert result.flow_state == FlowState.IDLE


async def test_router_rebook_never_ignores_an_event_in_the_new_window(monkeypatch):
    """No `ignore_event_id`: the consultation has no event on the agenda now - anything in the
    window is somebody else's (even an event still called like the old one)."""
    cal = Agenda()
    appt = _appt(rebook_adjustment_id=str(uuid4()), rebook_tenant_id=str(uuid4()))
    start = datetime(2030, 10, 20, 10, 0, tzinfo=fr.ZoneInfo("America/Sao_Paulo"))
    cal.add("evt-old", start, start + timedelta(minutes=30))

    async def _ensure(tid, aid):
        return True

    async def _reoffer(*args, **kwargs):
        return fr.FlowRouterResult(action="reply", bubbles=[], flow_state=FlowState.MANAGE_BOOKING)

    monkeypatch.setattr(fr, "_ensure_rebook_old_event_gone", _ensure)
    monkeypatch.setattr(fr, "_reoffer_taken_slot", _reoffer)
    result = await fr._manage_reschedule(
        _confirm_conversation(appt["id"]), _router_tenant(), cal, "Confirmar", [appt]
    )
    assert cal.created == [] and result.appointment_reschedule is None


async def test_router_rebook_ensure_never_raises(monkeypatch):
    async def _boom(session, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(old_slot, "ensure_old_event_gone", _boom)
    assert await fr._ensure_rebook_old_event_gone(uuid4(), uuid4()) is False


def test_rebook_ids_need_both_marks():
    adjustment_id, tenant_id = uuid4(), uuid4()
    assert fr._rebook_ids({}) is None
    assert fr._rebook_ids({"rebook_adjustment_id": str(adjustment_id)}) is None
    assert fr._rebook_ids({"rebook_adjustment_id": "x", "rebook_tenant_id": "y"}) is None
    assert fr._rebook_ids(
        {"rebook_adjustment_id": str(adjustment_id), "rebook_tenant_id": str(tenant_id)}
    ) == (tenant_id, adjustment_id)


async def test_the_status_left_by_the_rebook_is_terminal_for_the_list(db, agenda, ua2, spies):  # noqa: F811
    """After the move the consultation is an ordinary visible one (no rebook marks)."""
    world, adjustment_id = await ua3_world(db, status=STATUS_OLD_SLOT_LOST)
    async with db() as session:
        await session.execute(
            update(DepositAdjustment)
            .where(DepositAdjustment.id == adjustment_id)
            .values(old_slot=OLD_SLOT_RELEASED)
        )
        await session.commit()
    await wa_rebook_to_first_slot(db, world, adjustment_id)
    await wa_confirm(db, world)
    from secretaria.services.patient_context import load_upcoming_appointments

    async with db() as session:
        rows = await load_upcoming_appointments(
            session, world.tenant.id, world.patient.id, rebooking_id=world.appointment.id
        )
    assert [row["id"] for row in rows] == [str(world.appointment.id)]
    assert "rebook_adjustment_id" not in rows[0]
