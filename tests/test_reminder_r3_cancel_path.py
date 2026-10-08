"""Direct Cancelar and legacy edit cards (TASK-032 R3 compatibility after R6).

TASK-032 R3, spec §4.3.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from secretaria.models import (
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    PixDeposit,
    Tenant,
)
from secretaria.services.appointment_edit import LABEL_EDIT_DATE, LABEL_EDIT_TIME
from secretaria.services.reminder_text import give_up_confirm_buttons
from secretaria.workers import tasks
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.deposit import _pix_retention_warning_line
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import add_appointment, get_conversation, sent, wire
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


def _texts() -> list[str]:
    return [item[2] for item in sent() if item[0] == "text"]


async def _deposit_and_tenant(db, world):  # noqa: F811
    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
        return deposit, await session.get(Tenant, world.tenant.id)


async def test_give_up_asks_for_confirmation_first(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remgiveup", rid)

    [(kind, _to, body, buttons)] = sent()
    assert kind == "buttons"
    assert body == ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))
    assert buttons == give_up_confirm_buttons(rid)
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_cancel_asks_directly_and_keeps_appointment_until_yes(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    await _tap(world, "remcancel", rid)
    assert (await get_reminder(db, rid)).answer == "cancel"
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED
    assert len(sent()) == 1 and sent()[0][0] == "buttons"


async def test_cancel_question_shows_the_date_and_both_outcomes(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    await _tap(world, "remcancel", rid)
    assert _when(world.start_at) in sent()[0][2]
    assert sent()[0][3] == [(f"remgiveupyes|{rid}", "Sim, cancelar"),
                            (f"remkeep|{rid}", "Manter consulta")]


async def test_legacy_reschedule_opens_a_draft_for_the_same_appointment(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)
    await _tap(world, "remresched", rid)
    conv = await get_conversation(db, world)
    assert conv.flow_state == FlowState.EDIT_BOOKING
    assert conv.flow_managing_appointment_id == world.appointment.id
    assert conv.flow_edit_draft["appointment_id"] == str(world.appointment.id)
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_legacy_reschedule_still_respects_the_pix_limit(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await seed_paid_deposit(db, world)
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.appointment_id == world.appointment.id)
            .values(reschedule_count=world.tenant.pix_reschedule_limit)
        )
        await session.commit()
    rid = await add_reminder(db, world)
    await _tap(world, "remresched", rid)
    assert sent()[0][0] == "buttons"
    labels = [label for _id, label in sent()[0][3]]
    assert LABEL_EDIT_DATE not in labels and LABEL_EDIT_TIME not in labels
    row = await reload_appointment(db, world.appointment.id)
    assert row.start_at == world.appointment.start_at


async def test_give_up_inside_the_refund_window_warns_about_the_deposit_first(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(hours=5))
    await seed_paid_deposit(db, world)
    rid = await add_reminder(db, world, kind="hour")

    await _tap(world, "remgiveup", rid)

    deposit, tenant = await _deposit_and_tenant(db, world)
    [(_kind, _to, body, _buttons)] = sent()
    warning = _pix_retention_warning_line(tenant, deposit)
    assert body == f"{warning} {ra.GIVE_UP_CONFIRM_TEXT.format(when=_when(world.start_at))}"


async def test_keep_leaves_the_appointment_and_says_so(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remkeep", rid)

    assert _texts() == [ra.KEPT_TEXT.format(when=_when(world.start_at))]
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_legacy_other_opens_the_edit_menu(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    rid = await add_reminder(db, world)

    await _tap(world, "remother", rid)

    assert sent()[0][0] == "list" and sent()[0][2].startswith("*Alterar Dados*")
    assert (await get_conversation(db, world)).flow_state == FlowState.EDIT_BOOKING
    assert (await get_reminder(db, rid)).answer == "other"


async def test_cancel_path_taps_are_checked_against_the_patient_and_the_version(db):  # noqa: F811
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

    for action in ("remgiveup", "remgiveupyes", "remkeep"):
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
    await _tap(world, "remgiveupyes", old)

    assert _texts() == [ra.NOT_FOUND_TEXT] * 3 + [ra.MOVED_TEXT.format(when=_when(world.start_at))]
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED
    assert (await get_conversation(db, world)).flow_state == FlowState.IDLE


async def test_a_chat_card_confirmation_names_the_other_appointments(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    other = await add_appointment(db, world, start_at=_future(hours=24 * 6))
    rid = await add_reminder(db, world, kind="chat")

    await _tap(world, "remconfirm", rid)

    also = ra.ALSO_SCHEDULED_TEXT.format(whens=_when(other.start_at))
    assert _texts() == [f"{ra.CONFIRMED_TEXT.format(when=_when(world.start_at))}\n\n{also}"]
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_a_cron_card_confirmation_stays_as_r2_wrote_it(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await add_appointment(db, world, start_at=_future(hours=24 * 6))
    rid = await add_reminder(db, world, kind="day")

    await _tap(world, "remconfirm", rid)

    assert _texts() == [ra.CONFIRMED_TEXT.format(when=_when(world.start_at))]
