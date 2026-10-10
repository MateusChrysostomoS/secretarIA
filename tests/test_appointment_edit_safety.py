"""Safety at the edit's Calendar/database and stale-card boundaries."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import update

from secretaria.models import Appointment, AppointmentStatus, FlowState, Patient, PixDeposit
from secretaria.services import (
    appointment_edit as ae,
    appointment_edit_write as writer,
    flow_router as fr,
)
from secretaria.services.appointment_edit_flow import edit_step
from secretaria.workers import tasks
from tests._edit_flow_support import (
    DOCTOR_A,
    DOCTOR_B,
    TZ,
    EditCalendar,
    appointment,
    context,
    conversation,
    draft_for,
    professionals,
    tenant,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import get_conversation, sent, set_conversation, wire
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    reload_appointment,
    seed_paid_deposit,
    seed_world,
)
from tests.test_appointment_edit_apply import _apply, _confirm, _edit, _reply


@pytest.fixture(autouse=True)
def isolated(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)
    monkeypatch.setattr(fr, "_hold_windows", AsyncMock(return_value=[]))


def _view(world):
    row = world.appointment
    return {
        "id": str(row.id),
        "google_event_id": row.google_event_id,
        "appointment_type": row.appointment_type,
        "professional_id": None,
        "start_at": row.start_at,
        "end_at": row.end_at,
        "insurance": row.insurance,
        "attendee_name": row.attendee_name,
    }


@pytest.mark.parametrize("doctor_change", [False, True])
async def test_database_failure_compensates_calendar_and_keeps_the_draft(
    db,  # noqa: F811
    monkeypatch,
    doctor_change,  # noqa: F811
):
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    view = _view(world)
    original = draft_for(view)
    changes = {"insurance": "Amil"}
    if doctor_change:
        changes["professional_id"] = str(DOCTOR_B)
    draft = original.with_changes(**changes)
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=fr.STEP_EDIT_CONFIRM,
        flow_edit_draft=draft.to_json(),
        flow_managing_appointment_id=world.appointment.id,
    )
    cal = EditCalendar()
    result = await edit_step(
        conversation(draft, fr.STEP_EDIT_CONFIRM, patient_id=world.patient.id),
        world.tenant,
        fr.LABEL_CONFIRM,
        [view],
        professionals(),
        context(calendars={"tenant": EditCalendar() if doctor_change else cal, str(DOCTOR_B): cal}),
        "Maria",
    )
    assert result.appointment_edit is not None
    monkeypatch.setattr(
        writer,
        "resolve_booking_plan_ids",
        AsyncMock(side_effect=RuntimeError("db failed")),
    )
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, waba_token="t"
    )
    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance is None and row.google_event_id == view["google_event_id"]
    conv = await get_conversation(db, world)
    assert conv.flow_edit_draft == draft.to_json()
    assert not any(ae.EDIT_APPLIED in item[2] for item in sent())
    if doctor_change:
        assert cal.cancelled == ["evt123"]
    else:
        assert len(cal.detail_updates) == 2
        assert "Convênio: Amil" not in cal.detail_updates[-1][4]


async def test_worker_refuses_another_patient_in_the_same_tenant(db):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    async with db() as session:
        other = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900001234")
        session.add(other)
        await session.flush()
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(patient_id=other.id)
        )
        await session.commit()
    await _apply(world, _edit(world, insurance="Amil"))
    assert (await reload_appointment(db, world.appointment.id)).insurance is None
    assert not any(item[0] == "buttons" and item[2] == "menu" for item in sent())


async def test_worker_refuses_past_and_cancelled_appointments_without_success(db):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) - timedelta(days=1))
    await _apply(world, _edit(world, insurance="Amil"))
    assert (await reload_appointment(db, world.appointment.id)).insurance is None
    assert not any(item[0] == "buttons" and item[2] == "menu" for item in sent())


async def test_a_concurrently_changed_appointment_is_not_overwritten():
    appt = appointment()
    draft = draft_for(appt).with_changes(insurance="Amil")
    updated = {**appt, "insurance": "Outro plano"}
    cal = EditCalendar()
    result = await _confirm(draft, ctx=context(cal), appt=updated)
    assert result.appointment_edit is None and not cal.detail_updates
    assert result.flow_state == FlowState.MENU


@pytest.mark.parametrize("change", ["insurance", "professional_id", "service", "start_at"])
async def test_new_pix_restrictions_block_confirmation_of_a_parked_draft(change):
    appt = appointment()
    values = {
        "insurance": "Amil",
        "professional_id": str(DOCTOR_B),
        "service": "Retorno",
        "start_at": "2030-10-16T15:20",
    }
    draft = draft_for(appt).with_changes(**{change: values[change]})
    if change == "start_at":
        draft = draft.with_changes(end_at="2030-10-16T16:00")
    cal = EditCalendar()
    result = await _confirm(
        draft,
        appt=appt,
        ctx=context(
            calendars={str(DOCTOR_A): cal, str(DOCTOR_B): cal},
            paid_deposit=True,
            reschedule_blocked=True,
        ),
    )
    assert result.appointment_edit is None
    assert not cal.created and not cal.detail_updates


async def test_exhausted_pix_limit_cannot_commit_a_time_change(db):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    await seed_paid_deposit(db, world)
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.appointment_id == world.appointment.id)
            .values(reschedule_count=world.tenant.pix_reschedule_limit)
        )
        await session.commit()
    await _apply(world, _edit(world))
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED
    assert row.start_at.year != 2030


async def test_a_hold_acquired_after_the_picker_blocks_confirm(monkeypatch):
    appt = appointment()
    draft = draft_for(appt).with_changes(start_at="2030-10-16T09:00", end_at="2030-10-16T09:40")
    monkeypatch.setattr(
        fr,
        "_hold_windows",
        AsyncMock(
            return_value=[
                (
                    datetime(2030, 10, 16, 9, tzinfo=TZ),
                    datetime(2030, 10, 16, 10, tzinfo=TZ),
                )
            ]
        ),
    )
    cal = EditCalendar()
    result = await _confirm(draft, ctx=context(cal), appt=appt)
    assert result.appointment_edit is None and not cal.detail_updates
    assert result.flow_step == fr.STEP_EDIT_DAY


async def test_an_unsupported_service_cannot_be_confirmed_after_switching_doctor():
    appt = appointment(appointment_type="Retorno")
    draft = draft_for(appt).with_changes(professional_id=str(DOCTOR_B))
    cal = EditCalendar()
    result = await _confirm(draft, appt=appt, ctx=context(calendars={str(DOCTOR_B): cal}))
    assert result.appointment_edit is None and not cal.created
    assert result.flow_step == fr.STEP_EDIT_SERVICE


async def test_switch_off_cannot_open_an_edit_from_a_legacy_reminder(db):  # noqa: F811
    world = await seed_world(db, v2=False, start_at=datetime.now(UTC) + timedelta(days=3))
    rid = await add_reminder(db, world)
    await tasks._handle_action_button(_reply(world), "remother", str(rid))
    assert (await get_conversation(db, world)).flow_state != FlowState.EDIT_BOOKING
    assert not any(item[2].startswith("*Alterar Dados*") for item in sent())


async def test_switch_off_discards_an_edit_without_writing_calendar():
    appt = appointment()
    draft = draft_for(appt).with_changes(insurance="Amil")
    cal = EditCalendar()
    result = await edit_step(
        conversation(draft, fr.STEP_EDIT_CONFIRM),
        tenant(reminders_v2_enabled=False),
        fr.LABEL_CONFIRM,
        [appt],
        professionals(),
        context(cal),
    )
    assert result.flow_state == FlowState.MENU
    assert result.appointment_edit is None and not cal.detail_updates


async def test_next_day_page_keeps_the_appointment_anchor():
    appt = appointment()
    draft = draft_for(appt).with_stage(mode="date")
    days = [datetime(2030, 10, day) for day in range(8, 28)]
    cal = EditCalendar(days=days)
    conv = conversation(draft, fr.STEP_EDIT_DAY)
    first = await edit_step(
        conv, tenant(), "Ver mais dias (1)", [appt], professionals(), context(cal)
    )
    assert first.flow_step == fr.STEP_EDIT_DAY
    assert cal.day_scans[0][0].date() == datetime(2030, 10, 5).date()
    ids = [row[0] for row in first.bubbles[0].rows if row[0].startswith("day|")]
    assert ids == [f"day|2030-10-{day:02d}|1" for day in (8, 9, 10, 19, 20, 21, 22, 23)]


async def test_an_existing_attendee_is_masked_during_edit_and_after_discard(db, monkeypatch):  # noqa: F811
    from secretaria.services import pii_pseudonymization as pii

    monkeypatch.setattr(pii, "async_session_factory", db)
    world = await seed_world(
        db, attendee_name="Ana Souza", start_at=datetime.now(UTC) + timedelta(days=3)
    )
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(conversation_id=None)
        )
        await session.commit()
    draft = draft_for(_view(world))
    result = fr.FlowRouterResult(
        action="reply",
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=fr.STEP_EDIT_MENU,
        flow_edit_draft=draft.to_json(),
    )
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, waba_token="t"
    )
    masker = await pii.load_pseudonymizer(world.conversation.id)
    assert "Ana Souza" not in masker.scrub("Paciente: Ana Souza")
    await tasks._apply_flow_result(
        _reply(world),
        fr.FlowRouterResult(action="reply", flow_state=FlowState.MENU),
        WA_ID,
        tenant=world.tenant,
        waba_token="t",
    )
    masker = await pii.load_pseudonymizer(world.conversation.id)
    assert "Ana Souza" not in masker.scrub("Paciente: Ana Souza")


async def test_changing_doctor_on_a_shared_calendar_keeps_the_same_event():
    appt = appointment()
    draft = draft_for(appt).with_changes(professional_id=str(DOCTOR_B), end_at="2030-10-15T15:50")
    cal = EditCalendar()
    result = await _confirm(
        draft, appt=appt, ctx=context(calendars={str(DOCTOR_A): cal, str(DOCTOR_B): cal})
    )
    assert result.appointment_edit is not None
    assert result.appointment_edit["doctor_changed"] is True
    assert result.appointment_edit["google_event_id"] == "evt-old"
    assert not cal.created and len(cal.detail_updates) == 1
    assert cal.checked[0][2] == "evt-old"


async def test_a_clinic_change_after_calendar_write_wins_and_is_restored(db):  # noqa: F811
    world = await seed_world(db, start_at=datetime.now(UTC) + timedelta(days=3))
    view = _view(world)
    draft = draft_for(view).with_changes(insurance="Amil")
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=fr.STEP_EDIT_CONFIRM,
        flow_edit_draft=draft.to_json(),
        flow_managing_appointment_id=world.appointment.id,
    )
    cal = EditCalendar()
    result = await edit_step(
        conversation(draft, fr.STEP_EDIT_CONFIRM, patient_id=world.patient.id),
        world.tenant,
        fr.LABEL_CONFIRM,
        [view],
        professionals(),
        context(calendars={"tenant": cal}),
        "Maria",
    )
    assert len(cal.detail_updates) == 1
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(insurance="Correção da clínica")
        )
        await session.commit()
    await tasks._apply_flow_result(
        _reply(world),
        result,
        WA_ID,
        tenant=world.tenant,
        waba_token="t",
    )
    assert (await reload_appointment(db, world.appointment.id)).insurance == "Correção da clínica"
    assert (await get_conversation(db, world)).flow_edit_draft == draft.to_json()
    assert len(cal.detail_updates) == 2
    assert "Convênio: Correção da clínica" in cal.detail_updates[-1][4]
    assert not any(ae.EDIT_APPLIED in item[2] for item in sent())
