"""schedule_reminders plans the extra reminder on the clinic's local day and hour (TASK-048 R9)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

from secretaria.models import Appointment, Tenant
from secretaria.services.reminder_schedule import _as_utc, schedule_reminders
from tests._reminder_fixtures import NOW, db, make_appointment, tenant  # noqa: F401


async def _configure(db, tenant_id, **fields) -> None:
    async with db() as session:
        row = await session.get(Tenant, tenant_id)
        for name, value in fields.items():
            setattr(row, name, value)
        await session.commit()


async def _schedule(db, tenant_id, appointment_id):
    async with db() as session:
        clinic = await session.get(Tenant, tenant_id)
        appointment = await session.get(Appointment, appointment_id)
        created = await schedule_reminders(session, appointment, clinic, now=NOW)
        await session.commit()
        return created


async def test_the_extra_reminder_leaves_n_days_before_at_the_chosen_local_time(db, tenant):
    await _configure(db, tenant.id, reminder_extra_send_time="07:15")
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))

    created = await _schedule(db, tenant.id, appointment.id)

    assert [r.kind for r in created] == ["custom", "day", "hour"]
    custom = created[0]
    assert _as_utc(custom.due_at) == datetime(2026, 10, 2, 10, 15, tzinfo=UTC)  # 07:15 São Paulo
    assert _as_utc(custom.warn_due_at) == datetime(2026, 10, 2, 12, 15, tzinfo=UTC)


async def test_a_late_night_appointment_counts_days_on_the_local_calendar(db, tenant):
    await _configure(db, tenant.id, reminder_extra_days_before=2)
    start = datetime(2026, 10, 9, 1, 30, tzinfo=UTC)  # 2026-10-08 22:30 in São Paulo
    appointment = await make_appointment(db, tenant, start_at=start)

    created = await _schedule(db, tenant.id, appointment.id)

    custom = next(r for r in created if r.kind == "custom")
    assert _as_utc(custom.due_at) == datetime(2026, 10, 6, 12, 0, tzinfo=UTC)  # 06/10 09:00 local


async def test_a_day_already_past_gets_no_extra_reminder(db, tenant):
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=3))

    created = await _schedule(db, tenant.id, appointment.id)

    assert [r.kind for r in created] == ["day", "hour"]


async def test_a_clinic_without_an_extra_reminder_plans_only_day_and_hour(db, tenant):
    await _configure(db, tenant.id, reminder_extra_days_before=None, reminder_extra_send_time=None)
    appointment = await make_appointment(db, tenant, start_at=NOW + timedelta(days=10))

    created = await _schedule(db, tenant.id, appointment.id)

    assert [r.kind for r in created] == ["day", "hour"]
