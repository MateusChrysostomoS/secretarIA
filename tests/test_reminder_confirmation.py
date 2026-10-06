"""register_confirmation + display_state (TASK-032 R1, spec 4.1 'Regra de contagem')."""

# Imported pytest fixtures are deliberately reused as injected parameters.
# ruff: noqa: F811

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from secretaria.models import Appointment, AppointmentReminder, AppointmentStatus, Tenant
from secretaria.services.reminder_schedule import (
    ReminderMismatchError,
    display_state,
    register_confirmation,
    schedule_reminders,
)
from tests._reminder_fixtures import (  # noqa: F401
    NOW,
    db,
    make_appointment,
    other_tenant,
    tenant,
)

BUTTON = "reminder_button"


async def _booked(db, tenant, **kw):  # noqa: F811
    """An appointment 6 days out with its 3 reminder rows; returns (appt, {kind: id})."""
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=6), **kw)
    async with db() as session:
        a = await session.get(Appointment, appt.id)
        t = await session.get(Tenant, tenant.id)
        rows = await schedule_reminders(session, a, t, now=NOW)
        await session.commit()
        return appt, {r.kind: r.id for r in rows}


async def _confirm(db, appt_id, reminder_id, *, source=BUTTON):  # noqa: F811
    async with db() as session:
        a = await session.get(Appointment, appt_id)
        count = await register_confirmation(
            session, appointment=a, reminder_id=reminder_id, source=source, now=NOW
        )
        await session.commit()
        return count


async def _appt(db, appt_id) -> Appointment:  # noqa: F811
    async with db() as session:
        return await session.get(Appointment, appt_id)


async def test_first_confirmation_counts_one_and_marks_confirmed(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)

    assert await _confirm(db, appt.id, ids["day"]) == 1

    a = await _appt(db, appt.id)
    assert a.confirmation_count == 1 and a.status == AppointmentStatus.CONFIRMED
    assert a.first_confirmed_at is not None and a.last_confirmed_at is not None
    async with db() as session:
        row = await session.get(AppointmentReminder, ids["day"])
    assert row.answer == "confirm" and row.answered_at is not None


async def test_the_same_reminder_confirmed_twice_counts_once(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    assert await _confirm(db, appt.id, ids["day"]) == 1
    assert await _confirm(db, appt.id, ids["day"]) == 1
    assert (await _appt(db, appt.id)).confirmation_count == 1


async def test_two_distinct_reminders_count_two_and_a_third_stays_at_two(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    assert await _confirm(db, appt.id, ids["custom"]) == 1
    assert await _confirm(db, appt.id, ids["day"]) == 2
    assert await _confirm(db, appt.id, ids["hour"]) == 2  # confirmation after the cap
    a = await _appt(db, appt.id)
    assert a.confirmation_count == 2 and a.status == AppointmentStatus.CONFIRMED
    async with db() as session:
        row = await session.get(AppointmentReminder, ids["hour"])
    assert row.answer == "confirm"  # the tap is recorded, only the counter is capped


async def test_staff_confirmation_without_a_row_counts_one_and_is_idempotent(db, tenant):  # noqa: F811
    appt, _ = await _booked(db, tenant)
    assert await _confirm(db, appt.id, None, source="staff") == 1
    assert await _confirm(db, appt.id, None, source="staff") == 1


async def test_rescheduled_status_becomes_confirmed(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant, status=AppointmentStatus.RESCHEDULED)
    await _confirm(db, appt.id, ids["day"])
    assert (await _appt(db, appt.id)).status == AppointmentStatus.CONFIRMED


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW],
)
async def test_terminal_appointment_is_never_confirmed(db, tenant, status):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    async with db() as session:
        (await session.get(Appointment, appt.id)).status = status
        await session.commit()

    assert await _confirm(db, appt.id, ids["day"]) == 0

    a = await _appt(db, appt.id)
    assert a.confirmation_count == 0 and a.status == status


async def test_a_stale_tap_on_a_cancelled_reminder_does_not_count(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    async with db() as session:
        (await session.get(AppointmentReminder, ids["day"])).status = "cancelled"
        await session.commit()
    assert await _confirm(db, appt.id, ids["day"]) == 0
    assert (await _appt(db, appt.id)).status == AppointmentStatus.SCHEDULED


async def test_a_tap_on_an_old_version_of_a_moved_appointment_does_not_count(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    async with db() as session:
        (await session.get(Appointment, appt.id)).start_at = NOW + timedelta(days=9)
        await session.commit()
    assert await _confirm(db, appt.id, ids["day"]) == 0


async def test_reminder_of_another_clinic_or_appointment_is_refused(db, tenant, other_tenant):  # noqa: F811
    appt, _ = await _booked(db, tenant)
    foreign_appt, foreign_ids = await _booked(db, other_tenant)
    sibling, sibling_ids = await _booked(db, tenant)

    with pytest.raises(ReminderMismatchError):
        await _confirm(db, appt.id, foreign_ids["day"])  # other clinic
    with pytest.raises(ReminderMismatchError):
        await _confirm(db, appt.id, sibling_ids["day"])  # other appointment, same clinic
    with pytest.raises(ReminderMismatchError):
        await _confirm(db, appt.id, uuid4())  # unknown id

    assert (await _appt(db, appt.id)).confirmation_count == 0
    assert (await _appt(db, foreign_appt.id)).confirmation_count == 0
    assert (await _appt(db, sibling.id)).confirmation_count == 0


async def test_unknown_source_is_rejected(db, tenant):  # noqa: F811
    appt, ids = await _booked(db, tenant)
    with pytest.raises(ValueError):
        await _confirm(db, appt.id, ids["day"], source="carrier_pigeon")


@pytest.mark.parametrize(("second_kind", "expected"), [("hour", 2), ("day", 1)])
async def test_confirmation_reloads_stale_appointment_and_reminder(
    db,
    tenant,
    second_kind,
    expected,  # noqa: F811
):
    appt, ids = await _booked(db, tenant)
    async with db() as first, db() as second:
        first_appt = await first.get(Appointment, appt.id)
        stale_appt = await second.get(Appointment, appt.id)
        stale_reminder = await second.get(AppointmentReminder, ids[second_kind])
        assert stale_reminder.answer is None
        await register_confirmation(
            first, appointment=first_appt, reminder_id=ids["day"], source=BUTTON, now=NOW
        )
        await first.commit()
        count = await register_confirmation(
            second, appointment=stale_appt, reminder_id=ids[second_kind], source=BUTTON, now=NOW
        )
        await second.commit()
    assert count == expected
    assert (await _appt(db, appt.id)).confirmation_count == expected


@pytest.mark.parametrize("change", ["cancel", "reschedule", "reset"])
async def test_stale_confirmation_respects_cancel_reschedule_and_reset(db, tenant, change):  # noqa: F811
    from secretaria.services.reminder_schedule import (
        cancel_reminders,
        reschedule_reminders,
        reset_confirmation,
    )

    appt, ids = await _booked(db, tenant)
    await _confirm(db, appt.id, ids["custom"])
    async with db() as stale, db() as writer:
        stale_appt = await stale.get(Appointment, appt.id)
        stale_reminder = await stale.get(AppointmentReminder, ids["day"])
        assert stale_reminder.answer is None
        current = await writer.get(Appointment, appt.id)
        if change == "cancel":
            current.status = AppointmentStatus.CANCELLED
            await cancel_reminders(writer, appt.id, reason="cancelled")
        elif change == "reschedule":
            current.start_at = NOW + timedelta(days=9)
            current.status = AppointmentStatus.RESCHEDULED
            await reschedule_reminders(
                writer, current, await writer.get(Tenant, tenant.id), now=NOW
            )
        else:
            current.status = AppointmentStatus.SCHEDULED
            reset_confirmation(current)
        await writer.commit()
        count = await register_confirmation(
            stale, appointment=stale_appt, reminder_id=ids["day"], source=BUTTON, now=NOW
        )
        await stale.commit()
    fresh = await _appt(db, appt.id)
    assert count == (0 if change == "reschedule" else 1)
    assert (
        fresh.status
        == {
            "cancel": AppointmentStatus.CANCELLED,
            "reschedule": AppointmentStatus.RESCHEDULED,
            "reset": AppointmentStatus.CONFIRMED,
        }[change]
    )


def _appt_row(status=AppointmentStatus.SCHEDULED, count=0, start=None):
    return SimpleNamespace(
        status=status, confirmation_count=count, start_at=start or NOW + timedelta(days=2)
    )


def _rem(warned, start=None):
    return SimpleNamespace(
        warned_at=NOW if warned else None, appointment_start_at=start or NOW + timedelta(days=2)
    )


def test_display_state_covers_the_four_states():
    assert display_state(_appt_row(), []) == "unconfirmed"
    assert display_state(_appt_row(), [_rem(warned=False)]) == "unconfirmed"
    assert display_state(_appt_row(count=1), []) == "confirmed"
    assert display_state(_appt_row(count=2), []) == "confirmed_twice"
    assert display_state(_appt_row(), [_rem(warned=True)]) == "attention"


def test_a_confirmation_beats_an_old_warning():
    assert display_state(_appt_row(count=1), [_rem(warned=True)]) == "confirmed"


def test_a_warning_of_an_older_start_does_not_turn_the_appointment_red():
    old = _rem(warned=True, start=NOW + timedelta(days=1))
    assert display_state(_appt_row(), [old]) == "unconfirmed"


def test_naive_and_aware_starts_are_the_same_version():
    start = NOW + timedelta(days=2)
    warned = _rem(warned=True, start=start.replace(tzinfo=None))
    assert display_state(_appt_row(start=start), [warned]) == "attention"


@pytest.mark.parametrize(
    "status",
    [AppointmentStatus.CANCELLED, AppointmentStatus.ATTENDED, AppointmentStatus.NO_SHOW],
)
def test_terminal_appointments_are_never_attention_or_confirmed(status):
    row = _appt_row(status=status, count=2)
    assert display_state(row, [_rem(warned=True)]) == "unconfirmed"
