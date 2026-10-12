"""Confirming the edit: calendar, the same row, reminders, money (TASK-032 R6, spec §5.6)."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from secretaria.models import AppointmentStatus, FlowState
from secretaria.services import appointment_edit as ae, flow_router as fr
from secretaria.services.appointment_edit_flow import edit_step
from secretaria.workers import tasks
from secretaria.workers.shared import appointment_edit_support as support
from tests._edit_flow_support import (
    DOCTOR_A,
    DOCTOR_B,
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    original_appointment,
    professionals,
    tenant,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import (
    consent,
    get_conversation,
    reminder_rows,
    sent,
    turn,
    wire,
)
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    get_reminder,
    reload_appointment,
    seed_paid_deposit,
    seed_world,
)


def _moved(draft):
    return draft.with_changes(start_at="2030-10-16T09:00", end_at="2030-10-16T09:40")


async def _confirm(draft, *, ctx, appt=None, patient_name="Maria"):
    appt = appt or original_appointment(draft)
    conv = conversation(draft, fr.STEP_EDIT_CONFIRM)
    return await edit_step(
        conv, tenant(), fr.LABEL_CONFIRM, [appt], professionals(), ctx, patient_name
    )


# --- The router: what Confirmar asks of the calendar -----------------------------------


async def test_confirming_a_time_change_patches_the_event_and_returns_the_edit():
    calendar = EditCalendar()
    appt = appointment()
    result = await _confirm(_moved(draft_for(appt)), ctx=context(calendar), appt=appt)

    [(event_id, start, end, summary, description)] = calendar.detail_updates
    assert event_id == "evt-old"
    assert (start.hour, start.minute, end.hour, end.minute) == (9, 0, 9, 40)
    assert summary == "Consulta - Maria"
    assert "Convênio: Unimed" in description
    edit = result.appointment_edit
    assert edit["time_changed"] is True and edit["doctor_changed"] is False
    assert edit["google_event_id"] == "evt-old" and edit["old_google_event_id"] == "evt-old"
    assert result.flow_state == FlowState.MENU and result.flow_edit_draft is None
    assert result.bubbles[0].body.startswith(ae.EDIT_APPLIED)
    assert isinstance(result.bubbles[1], fr.MenuBubble)  # the returning-patient menu


async def test_the_draft_never_touches_the_appointment_before_confirm():
    calendar = EditCalendar()
    appt = appointment()
    draft = _moved(draft_for(appt))
    conv = conversation(draft, fr.STEP_EDIT_CONFIRM)

    for body in (ae.LABEL_EDIT_MORE_DATA, fr.LABEL_CANCEL):
        result = await edit_step(conv, tenant(), body, [appt], professionals(), context(calendar))
        assert result.appointment_edit is None
    assert calendar.detail_updates == [] and calendar.created == [] and calendar.cancelled == []


async def test_a_doctor_change_creates_on_the_new_agenda_and_never_deletes_here():
    old, new = EditCalendar(), EditCalendar(free=True)
    appt = appointment()
    draft = draft_for(appt).with_changes(professional_id=str(DOCTOR_B), end_at="2030-10-15T15:50")

    result = await _confirm(
        draft, ctx=context(calendars={str(DOCTOR_A): old, str(DOCTOR_B): new}), appt=appt
    )

    assert len(new.created) == 1 and new.detail_updates == []
    edit = result.appointment_edit
    assert edit["doctor_changed"] is True
    assert edit["google_event_id"] == "evt123" and edit["old_google_event_id"] == "evt-old"
    assert edit["old_professional_id"] == DOCTOR_A and edit["professional_id"] == DOCTOR_B
    # The old event is deleted by the worker AFTER the commit, never here.
    assert old.cancelled == [] and old.detail_updates == []


async def test_a_calendar_failure_changes_nothing_and_keeps_the_draft():
    calendar = EditCalendar(fail_update=True)
    appt = appointment()
    draft = _moved(draft_for(appt))

    result = await _confirm(draft, ctx=context(calendar), appt=appt)

    assert result.action == "calendar_unavailable"
    assert result.appointment_edit is None
    assert result.flow_state == FlowState.EDIT_BOOKING and result.flow_step == fr.STEP_EDIT_CONFIRM
    assert result.flow_edit_draft == draft.to_json()


async def test_a_slot_taken_in_the_meantime_goes_back_to_day_and_time():
    calendar = EditCalendar(free=False)
    appt = appointment()

    result = await _confirm(_moved(draft_for(appt)), ctx=context(calendar), appt=appt)

    assert result.appointment_edit is None
    assert result.flow_step == fr.STEP_EDIT_DAY
    assert result.bubbles[0].body == ae.EDIT_RESLOT_NOTICE
    assert calendar.detail_updates == []


async def test_confirming_without_any_change_shows_the_menu_instead():
    appt = appointment()
    result = await _confirm(draft_for(appt), ctx=context(EditCalendar()), appt=appt)
    assert result.appointment_edit is None
    assert result.bubbles[0].body == ae.EDIT_NOTHING_CHANGED
    assert result.flow_step == fr.STEP_EDIT_MENU


async def test_only_the_service_changing_does_not_ask_for_a_free_slot_check():
    calendar = EditCalendar(free=False)  # would fail a slot check; none is needed
    appt = appointment()
    draft = draft_for(appt).with_changes(insurance="Amil")

    result = await _confirm(draft, ctx=context(calendar), appt=appt)

    assert result.appointment_edit is not None
    assert result.appointment_edit["time_changed"] is False
    assert calendar.checked == []


# --- The worker: the same row, the old event, the reminders ----------------------------


@pytest.fixture
def calendar(monkeypatch, db):  # noqa: F811
    return wire(monkeypatch, db)


def _reply(world):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


def _edit(world, **kw) -> dict:
    start = datetime(2030, 10, 16, 12, 0, tzinfo=UTC)
    base = {
        "appointment_id": world.appointment.id,
        "old_google_event_id": "evt-old",
        "google_event_id": "evt-old",
        "google_event_link": None,
        "appointment_type": "Consulta",
        "professional_id": None,
        "old_professional_id": None,
        "insurance": world.appointment.insurance,
        "attendee_name": None,
        "start_at": start,
        "end_at": start + timedelta(minutes=40),
        "time_changed": True,
        "doctor_changed": False,
    }
    base.update(kw)
    return base


async def _apply(world, edit):
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[fr.MenuBubble(body="menu", labels=["Agendar", "Outro"])],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, waba_token="t"
    )


async def test_a_time_change_updates_the_same_row_and_replans_the_reminders(db, calendar):  # noqa: F811
    world = await seed_world(
        db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-old"
    )
    old_row = await add_reminder(
        db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2)
    )

    await _apply(world, _edit(world))

    row = await reload_appointment(db, world.appointment.id)
    assert row.id == world.appointment.id
    assert row.status == AppointmentStatus.RESCHEDULED
    assert (row.start_at.hour, row.end_at.minute) == (12, 40)
    assert (await get_reminder(db, old_row)).status == "cancelled"
    assert any(r.status == "pending" for r in await reminder_rows(db, world.appointment.id))
    assert calendar.cancelled == []  # same doctor: nothing to delete


async def test_a_doctor_change_creates_the_new_event_then_deletes_the_old_one_after_commit(
    db,  # noqa: F811
    calendar,  # noqa: F811
):
    world = await seed_world(
        db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-old"
    )
    new_doctor = uuid4()

    await _apply(
        world,
        _edit(
            world,
            professional_id=new_doctor,
            google_event_id="evt-new",
            doctor_changed=True,
            time_changed=False,
        ),
    )

    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-new" and row.professional_id == new_doctor
    assert row.status == AppointmentStatus.SCHEDULED  # same time: not "rescheduled"
    assert calendar.cancelled == ["evt-old"]


async def test_service_convenio_and_patient_are_written_to_the_row(db, calendar):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))

    await _apply(
        world,
        _edit(
            world,
            time_changed=False,
            start_at=world.start_at,
            end_at=world.start_at + timedelta(minutes=20),
            appointment_type="Retorno",
            insurance="Amil",
            attendee_name="Ana Souza",
            google_event_id=world.appointment.google_event_id,
            old_google_event_id=world.appointment.google_event_id,
        ),
    )

    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.insurance, row.attendee_name) == (
        "Retorno",
        "Amil",
        "Ana Souza",
    )
    assert row.status == AppointmentStatus.SCHEDULED


async def test_a_cancelled_appointment_is_never_edited(db, calendar):  # noqa: F811
    world = await seed_world(
        db,
        start_at=datetime.now(UTC) + timedelta(days=3),
        status=AppointmentStatus.CANCELLED,
    )
    await _apply(world, _edit(world))
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED


async def test_a_time_change_counts_against_the_pix_reschedule_limit(db, calendar):  # noqa: F811
    from sqlalchemy import select

    from secretaria.models import PixDeposit

    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    await seed_paid_deposit(db, world)

    await _apply(world, _edit(world))

    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
        )
    assert deposit.reschedule_count == 1


# --- The guards and the context -----------------------------------------------------------


async def test_edit_guards_read_the_deposit_and_the_limit(db):  # noqa: F811
    from sqlalchemy import update

    from secretaria.models import Appointment, PixDeposit, Tenant

    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    async with db() as session:
        appointment_row = await session.get(Appointment, world.appointment.id)
        tenant_row = await session.get(Tenant, world.tenant.id)
        assert await support.edit_guards(session, tenant_row, appointment_row) == (False, False)

    await seed_paid_deposit(db, world)
    async with db() as session:
        appointment_row = await session.get(Appointment, world.appointment.id)
        tenant_row = await session.get(Tenant, world.tenant.id)
        assert await support.edit_guards(session, tenant_row, appointment_row) == (True, False)
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.appointment_id == world.appointment.id)
            .values(reschedule_count=tenant_row.pix_reschedule_limit)
        )
        await session.commit()
    async with db() as session:
        appointment_row = await session.get(Appointment, world.appointment.id)
        tenant_row = await session.get(Tenant, world.tenant.id)
        assert await support.edit_guards(session, tenant_row, appointment_row) == (True, True)


# --- End to end through a real turn -------------------------------------------------------


@pytest.fixture
def edit_calendar(monkeypatch, db):  # noqa: F811
    cal = EditCalendar(
        slots=[{"start": "2030-10-15T16:00", "end": "2030-10-15T16:40", "label": "16:00"}]
    )

    class _Service:
        @classmethod
        def from_tenant_config(cls, config):
            return cal

    monkeypatch.setattr(support, "CalendarService", _Service)
    monkeypatch.setattr(fr, "_hold_windows", AsyncMock(return_value=[]))
    wire(monkeypatch, db)
    return cal


async def _edit_world(db):  # noqa: F811
    world = await seed_world(
        db,
        start_at=datetime.now(UTC) + timedelta(days=3),
        last_inbound_at=datetime.now(UTC) - timedelta(minutes=1),
        google_event_id="evt-old",
    )
    await consent(db, world)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2))
    return world, rid


async def _tap(world, action, rid):
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )
    await tasks._handle_action_button(reply, action, str(rid))


async def test_alterar_dados_opens_the_edit_menu_as_a_list_on_whatsapp(db, edit_calendar):  # noqa: F811
    world, rid = await _edit_world(db)

    await _tap(world, "remedit", rid)

    [(kind, _to, body, *_rest)] = sent()
    assert kind == "list" and body.startswith("*Alterar Dados*")
    conversation_row = await get_conversation(db, world)
    assert conversation_row.flow_state == FlowState.EDIT_BOOKING
    assert conversation_row.flow_step == fr.STEP_EDIT_MENU
    assert (await get_reminder(db, rid)).answer == "other"


async def test_cancel_on_the_final_card_keeps_the_appointment(db, edit_calendar):  # noqa: F811
    world, rid = await _edit_world(db)
    await _tap(world, "remedit", rid)
    await turn(db, world, ae.LABEL_EDIT_TIME)
    await turn(db, world, "16:00", interactive_reply_id="slot|2030-10-15T16:00")
    assert (await get_conversation(db, world)).flow_step == fr.STEP_EDIT_CONFIRM

    await turn(db, world, fr.LABEL_CANCEL)

    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED and row.start_at.year != 2030
    assert edit_calendar.detail_updates == []
    assert (await get_conversation(db, world)).flow_edit_draft is None


async def test_the_whole_edit_confirms_and_applies(db, edit_calendar):  # noqa: F811
    world, rid = await _edit_world(db)
    await _tap(world, "remedit", rid)
    await turn(db, world, ae.LABEL_EDIT_TIME)
    await turn(db, world, "16:00", interactive_reply_id="slot|2030-10-15T16:00")

    await turn(db, world, fr.LABEL_CONFIRM)

    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.RESCHEDULED
    assert (row.start_at.year, row.start_at.hour) == (2030, 19)  # 16:00 São Paulo = 19:00 UTC
    [(event_id, *_rest)] = edit_calendar.detail_updates
    assert event_id == "evt-old"
    texts = [item[2] for item in sent() if item[0] in ("text", "buttons")]
    assert texts[-2].startswith(ae.EDIT_APPLIED)
    conversation_row = await get_conversation(db, world)
    assert (
        conversation_row.flow_edit_draft is None and conversation_row.flow_state == FlowState.MENU
    )


async def test_a_foreign_remedit_opens_nothing(db, edit_calendar):  # noqa: F811
    world, rid = await _edit_world(db)
    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900002222")

    await _tap(other, "remedit", rid)

    assert (await get_conversation(db, other)).flow_state == FlowState.IDLE
