"""A new extra-reminder lead replans the still-pending extra reminders (TASK-044 R7, §5.B)."""

# ruff: noqa: F811

from datetime import timedelta

from sqlalchemy import select

from secretaria.models import Appointment, AppointmentReminder, Tenant
from secretaria.services import reminder_schedule
from secretaria.services.reminder_schedule import CustomReplan, _as_utc
from tests._reminder_fixtures import NOW, db, make_appointment, other_tenant, tenant  # noqa: F401


async def _set_tenant(db, tenant_id, **fields) -> None:
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _plan(db, tenant_id, appointment_id) -> None:
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        appointment = await session.get(Appointment, appointment_id)
        await reminder_schedule.schedule_reminders(session, appointment, clinic, now=NOW)
        await session.commit()


async def _rows(db, appointment_id, kind: str) -> list[AppointmentReminder]:
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder)
                .where(
                    AppointmentReminder.appointment_id == appointment_id,
                    AppointmentReminder.kind == kind,
                )
                .order_by(AppointmentReminder.created_at)
            )
        )


async def _row(db, appointment_id, kind: str) -> AppointmentReminder:
    rows = await _rows(db, appointment_id, kind)
    assert len(rows) == 1, rows
    return rows[0]


async def _replan(db, tenant_id, lead: int | None) -> CustomReplan:
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        clinic.reminder_extra_lead_minutes = lead
        result = await reminder_schedule.replan_custom_reminders(session, clinic, now=NOW)
        await session.commit()
        return result


async def test_a_new_lead_moves_the_pending_extra_reminder_and_nothing_else(db, tenant):
    start = NOW + timedelta(days=10)
    appointment = await make_appointment(db, tenant, start_at=start)
    await _plan(db, tenant.id, appointment.id)
    day_before = await _row(db, appointment.id, "day")
    custom_before = await _row(db, appointment.id, "custom")

    result = await _replan(db, tenant.id, 2880)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan(moved=1)
    assert custom.id == custom_before.id and custom.status == "pending"
    assert _as_utc(custom.due_at) == start - timedelta(minutes=2880)
    assert _as_utc(custom.warn_due_at) == start - timedelta(minutes=2880) + timedelta(hours=2)
    day = await _row(db, appointment.id, "day")
    assert (day.id, _as_utc(day.due_at)) == (day_before.id, _as_utc(day_before.due_at))


async def test_switching_the_extra_reminder_off_cancels_it(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, appointment.id)

    result = await _replan(db, tenant.id, None)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan(cancelled=1)
    assert custom.status == "cancelled"
    assert custom.invalidated_at is not None and custom.warn_due_at is None
    assert (await _row(db, appointment.id, "day")).status == "pending"


async def test_a_new_time_already_past_cancels_instead_of_sending_late(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _plan(db, tenant.id, appointment.id)  # 5-day lead: due tomorrow

    result = await _replan(db, tenant.id, 8640)  # 6 days: due = now

    assert result == CustomReplan(cancelled=1)
    assert (await _row(db, appointment.id, "custom")).status == "cancelled"


async def test_an_extra_reminder_already_sent_stays_as_history(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, appointment.id)
    sent_row = await _row(db, appointment.id, "custom")
    async with db() as session:
        row = await session.get(AppointmentReminder, sent_row.id)
        row.status = "sent"
        await session.commit()

    result = await _replan(db, tenant.id, 2880)

    custom = await _row(db, appointment.id, "custom")
    assert result == CustomReplan()
    assert custom.status == "sent" and _as_utc(custom.due_at) == _as_utc(sent_row.due_at)


async def test_turning_it_on_adds_it_only_where_a_plan_already_exists(db, tenant):
    await _set_tenant(db, tenant.id, reminder_extra_lead_minutes=None)
    planned = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))
    await _plan(db, tenant.id, planned.id)  # day + hour only
    never_planned = await make_appointment(db, tenant, start_at=NOW + timedelta(days=12))

    result = await _replan(db, tenant.id, 2880)

    assert result == CustomReplan(created=1)
    custom = await _row(db, planned.id, "custom")
    assert custom.status == "pending" and custom.with_prompt is True
    assert _as_utc(custom.due_at) == NOW + timedelta(days=10) - timedelta(minutes=2880)
    assert await _rows(db, never_planned.id, "custom") == []  # left to the reconcile cron


async def test_another_clinic_and_a_switched_off_clinic_are_untouched(db, tenant, other_tenant):
    start = NOW + timedelta(days=10)
    mine = await make_appointment(db, tenant, start_at=start)
    theirs = await make_appointment(db, other_tenant, start_at=start)
    await _plan(db, tenant.id, mine.id)
    await _plan(db, other_tenant.id, theirs.id)

    await _replan(db, tenant.id, 2880)

    assert _as_utc((await _row(db, theirs.id, "custom")).due_at) == start - timedelta(minutes=7200)

    await _set_tenant(db, tenant.id, reminders_v2_enabled=False)
    result = await _replan(db, tenant.id, 4320)

    assert result == CustomReplan()
    assert _as_utc((await _row(db, mine.id, "custom")).due_at) == start - timedelta(minutes=2880)
