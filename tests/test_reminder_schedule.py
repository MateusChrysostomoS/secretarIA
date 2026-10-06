"""schedule_reminders: which rows exist for an appointment (TASK-032 R1, spec 4.2)."""

# Imported pytest fixtures are deliberately reused as injected parameters.
# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.models import AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services.reminder_schedule import (
    cancel_reminders,
    reschedule_reminders,
    schedule_reminders,
)
from tests._reminder_fixtures import (  # noqa: F401
    NOW,
    db,
    make_appointment,
    other_tenant,
    tenant,
)


def _utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


async def _schedule(db, tenant_id, appt_id, *, now=NOW):  # noqa: F811
    async with db() as session:
        t = await session.get(Tenant, tenant_id)
        from secretaria.models import Appointment

        a = await session.get(Appointment, appt_id)
        rows = await schedule_reminders(session, a, t, now=now)
        await session.commit()
        return rows


async def _all_rows(db, appt_id):  # noqa: F811
    async with db() as session:
        return list(
            await session.scalars(
                select(AppointmentReminder)
                .where(AppointmentReminder.appointment_id == appt_id)
                .order_by(AppointmentReminder.due_at)
            )
        )


async def test_far_appointment_gets_custom_day_and_hour_with_warn_deadlines(db, tenant):  # noqa: F811
    start = NOW + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start)

    created = await _schedule(db, tenant.id, appt.id)

    assert [r.kind for r in created] == ["custom", "day", "hour"]
    rows = {r.kind: r for r in await _all_rows(db, appt.id)}
    assert _utc(rows["custom"].due_at) == start - timedelta(minutes=7200)
    assert _utc(rows["day"].due_at) == start - timedelta(hours=24)
    assert _utc(rows["hour"].due_at) == start - timedelta(hours=1)
    assert _utc(rows["custom"].warn_due_at) == _utc(rows["custom"].due_at) + timedelta(hours=2)
    assert _utc(rows["day"].warn_due_at) == _utc(rows["day"].due_at) + timedelta(hours=2)
    assert _utc(rows["hour"].warn_due_at) == _utc(rows["hour"].due_at) + timedelta(minutes=20)
    for r in rows.values():
        assert r.status == "pending" and r.with_prompt is True
        assert r.tenant_id == tenant.id and r.patient_id == appt.patient_id
        assert _utc(r.appointment_start_at) == start


async def test_no_custom_row_when_the_clinic_did_not_configure_one(db, tenant):  # noqa: F811
    async with db() as session:
        t = await session.get(Tenant, tenant.id)
        t.reminder_extra_lead_minutes = None
        await session.commit()
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))

    created = await _schedule(db, tenant.id, appt.id)

    assert [r.kind for r in created] == ["day", "hour"]


@pytest.mark.parametrize(
    ("lead", "expected"),
    [
        (timedelta(days=2), ["day", "hour"]),  # custom (5 d) already past
        (timedelta(hours=20), ["hour"]),  # day and custom already past
        (timedelta(hours=3), ["hour"]),
        (timedelta(minutes=30), []),  # booked too late for any reminder
        (timedelta(hours=1), []),  # hour reminder due exactly now: not in the future
    ],
)
async def test_reminders_already_due_are_skipped_not_created(db, tenant, lead, expected):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + lead)

    created = await _schedule(db, tenant.id, appt.id)

    assert [r.kind for r in created] == expected
    assert [r.kind for r in await _all_rows(db, appt.id)] == expected


async def test_scheduling_twice_is_idempotent(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    first = await _schedule(db, tenant.id, appt.id)
    second = await _schedule(db, tenant.id, appt.id)

    assert len(first) == 3 and second == []
    assert len(await _all_rows(db, appt.id)) == 3


async def test_clinic_with_the_switch_off_gets_nothing(db, tenant):  # noqa: F811
    async with db() as session:
        t = await session.get(Tenant, tenant.id)
        t.reminders_v2_enabled = False
        await session.commit()
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))

    assert await _schedule(db, tenant.id, appt.id) == []
    assert await _all_rows(db, appt.id) == []


async def test_appointment_of_another_clinic_is_refused(db, tenant, other_tenant):  # noqa: F811
    appt = await make_appointment(db, other_tenant, start_at=NOW + timedelta(days=6))
    async with db() as session:
        from secretaria.models import Appointment

        a = await session.get(Appointment, appt.id)
        t = await session.get(Tenant, tenant.id)
        with pytest.raises(ValueError):
            await schedule_reminders(session, a, t, now=NOW)
    assert await _all_rows(db, appt.id) == []


async def test_block_slot_without_patient_and_terminal_status_get_nothing(db, tenant):  # noqa: F811
    block = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6), with_patient=False)
    cancelled = await make_appointment(
        db, tenant, start_at=NOW + timedelta(days=6), status=AppointmentStatus.CANCELLED
    )
    assert await _schedule(db, tenant.id, block.id) == []
    assert await _schedule(db, tenant.id, cancelled.id) == []


async def test_naive_start_and_aware_now_give_the_same_rows(db, tenant):  # noqa: F811
    """SQLite hands back naive datetimes; the schedule must not shift or crash."""
    start = NOW + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start)
    async with db() as session:
        from secretaria.models import Appointment

        a = await session.get(Appointment, appt.id)
        a.start_at = start.replace(tzinfo=None)  # what a SQLite read-back looks like
        t = await session.get(Tenant, tenant.id)
        created = await schedule_reminders(session, a, t, now=NOW.replace(tzinfo=None))
        await session.commit()
    due = {r.kind: _utc(r.due_at) for r in created}
    assert due["day"] == start - timedelta(hours=24)
    assert due["hour"] == start - timedelta(hours=1)


async def test_with_prompt_is_false_after_two_confirmations(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    async with db() as session:
        from secretaria.models import Appointment

        a = await session.get(Appointment, appt.id)
        a.confirmation_count = 2
        t = await session.get(Tenant, tenant.id)
        created = await schedule_reminders(session, a, t, now=NOW)
        await session.commit()
    assert created and all(r.with_prompt is False for r in created)


async def _mark_sent(db, appt_id, kind):  # noqa: F811
    async with db() as session:
        row = await session.scalar(
            select(AppointmentReminder).where(
                AppointmentReminder.appointment_id == appt_id,
                AppointmentReminder.kind == kind,
            )
        )
        row.status = "sent"
        row.sent_at = NOW
        await session.commit()


async def test_cancel_cancels_pending_rows_and_only_for_that_appointment(db, tenant):  # noqa: F811
    a = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    b = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _schedule(db, tenant.id, a.id)
    await _schedule(db, tenant.id, b.id)

    async with db() as session:
        changed = await cancel_reminders(session, a.id, reason="cancelled")
        await session.commit()

    assert changed == 3
    assert {r.status for r in await _all_rows(db, a.id)} == {"cancelled"}
    assert {r.status for r in await _all_rows(db, b.id)} == {"pending"}


async def test_cancel_keeps_sent_history_but_clears_the_pending_warning(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _schedule(db, tenant.id, appt.id)
    await _mark_sent(db, appt.id, "day")

    async with db() as session:
        changed = await cancel_reminders(session, appt.id, reason="attended")
        await session.commit()

    rows = {r.kind: r for r in await _all_rows(db, appt.id)}
    assert changed == 2  # custom + hour; the sent one is history, not cancelled
    assert rows["day"].status == "sent" and rows["day"].warn_due_at is None
    assert rows["custom"].status == "cancelled" and rows["hour"].status == "cancelled"


async def test_cancel_with_nothing_pending_is_a_noop(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    async with db() as session:
        assert await cancel_reminders(session, appt.id, reason="cancelled") == 0


async def test_reschedule_zeroes_confirmation_cancels_old_rows_and_recreates(db, tenant):  # noqa: F811
    from secretaria.models import Appointment

    start = NOW + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start)
    await _schedule(db, tenant.id, appt.id)
    await _mark_sent(db, appt.id, "custom")
    new_start = NOW + timedelta(days=9)

    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.confirmation_count = 2
        a.first_confirmed_at = NOW
        a.last_confirmed_at = NOW
        a.start_at = new_start
        t = await session.get(Tenant, tenant.id)
        created = await reschedule_reminders(session, a, t, now=NOW)
        await session.commit()

    assert [r.kind for r in created] == ["custom", "day", "hour"]
    assert all(r.with_prompt is True for r in created)  # counter was zeroed first
    async with db() as session:
        a = await session.get(Appointment, appt.id)
    assert a.confirmation_count == 0
    assert a.first_confirmed_at is None and a.last_confirmed_at is None
    rows = await _all_rows(db, appt.id)
    old = [r for r in rows if _utc(r.appointment_start_at) == start]
    new = [r for r in rows if _utc(r.appointment_start_at) == new_start]
    assert len(old) == 3 and len(new) == 3
    by_kind = {r.kind: r.status for r in old}
    assert by_kind == {"custom": "sent", "day": "cancelled", "hour": "cancelled"}
    assert {r.status for r in new} == {"pending"}


async def test_reschedule_to_the_same_start_creates_fresh_rows(db, tenant):  # noqa: F811
    from secretaria.models import Appointment

    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    first = await _schedule(db, tenant.id, appt.id)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        t = await session.get(Tenant, tenant.id)
        again = await reschedule_reminders(session, a, t, now=NOW)  # no unique violation
        await session.commit()

    assert {r.id for r in again}.isdisjoint({r.id for r in first})
    assert {r.status for r in await _all_rows(db, appt.id)} == {"pending", "cancelled"}
    old_ids = {r.id for r in first}
    history = [r for r in await _all_rows(db, appt.id) if r.id in old_ids]
    assert len(history) == 3 and all(r.invalidated_at is not None for r in history)


async def test_reschedule_too_close_cancels_old_rows_and_creates_none(db, tenant):  # noqa: F811
    from secretaria.models import Appointment

    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _schedule(db, tenant.id, appt.id)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.start_at = NOW + timedelta(minutes=30)
        t = await session.get(Tenant, tenant.id)
        assert await reschedule_reminders(session, a, t, now=NOW) == []
        await session.commit()
    assert {r.status for r in await _all_rows(db, appt.id)} == {"cancelled"}


async def test_reschedule_on_a_disabled_clinic_still_zeroes_the_counter(db, tenant):  # noqa: F811
    from secretaria.models import Appointment

    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.confirmation_count = 1
        t = await session.get(Tenant, tenant.id)
        t.reminders_v2_enabled = False
        assert await reschedule_reminders(session, a, t, now=NOW) == []
        assert a.confirmation_count == 0


@pytest.mark.parametrize("foreign_status", ["pending", "sent"])
async def test_cancel_never_changes_a_reminder_owned_by_another_tenant(
    db,
    tenant,
    other_tenant,
    foreign_status,  # noqa: F811
):
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _schedule(db, tenant.id, appt.id)
    async with db() as session:
        foreign = await session.scalar(
            select(AppointmentReminder).where(
                AppointmentReminder.appointment_id == appt.id,
                AppointmentReminder.kind == "day",
            )
        )
        foreign.tenant_id = other_tenant.id
        foreign.status = foreign_status
        await session.commit()
        foreign_id = foreign.id
    async with db() as session:
        await cancel_reminders(session, appt.id, reason="cancelled")
        await session.commit()
    async with db() as session:
        foreign = await session.get(AppointmentReminder, foreign_id)
        assert foreign.status == foreign_status
        assert foreign.warn_due_at is not None


async def test_foreign_reschedule_is_rejected_before_any_mutation(db, tenant, other_tenant):  # noqa: F811
    from secretaria.models import Appointment

    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6))
    await _schedule(db, tenant.id, appt.id)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.confirmation_count = 1
        await session.commit()
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        with pytest.raises(ValueError):
            await reschedule_reminders(session, a, other_tenant, now=NOW)
        await session.commit()
    async with db() as session:
        assert (await session.get(Appointment, appt.id)).confirmation_count == 1
    assert {r.status for r in await _all_rows(db, appt.id)} == {"pending"}


@pytest.mark.parametrize("return_to_start", [False, True])
async def test_reschedule_recreates_sent_kinds_and_old_taps_and_warnings_stay_invalid(
    db,
    tenant,
    return_to_start,  # noqa: F811
):
    from secretaria.models import Appointment
    from secretaria.services.reminder_schedule import display_state, register_confirmation

    start = NOW + timedelta(days=6)
    appt = await make_appointment(db, tenant, start_at=start)
    first = await _schedule(db, tenant.id, appt.id)
    await _mark_sent(db, appt.id, "day")
    old_day_id = next(r.id for r in first if r.kind == "day")
    async with db() as session:
        old_day = await session.get(AppointmentReminder, old_day_id)
        old_day.warned_at = NOW
        old_day.warn_kind = "unconfirmed"
        await session.commit()
    if return_to_start:
        async with db() as session:
            a = await session.get(Appointment, appt.id)
            a.start_at = start + timedelta(days=3)
            a.status = AppointmentStatus.RESCHEDULED
            await reschedule_reminders(session, a, await session.get(Tenant, tenant.id), now=NOW)
            await session.commit()
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        a.start_at = start
        a.status = AppointmentStatus.RESCHEDULED
        fresh = await reschedule_reminders(
            session, a, await session.get(Tenant, tenant.id), now=NOW
        )
        await session.commit()
        assert [r.kind for r in fresh] == ["custom", "day", "hour"]
        assert {r.id for r in fresh}.isdisjoint({r.id for r in first})
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        assert (
            await register_confirmation(
                session, appointment=a, reminder_id=old_day_id, source="reminder_button", now=NOW
            )
            == 0
        )
        old_day = await session.get(AppointmentReminder, old_day_id)
        assert old_day.status == "sent" and old_day.sent_at is not None
        assert display_state(a, [old_day]) == "unconfirmed"
        await session.commit()
