"""AppointmentReminder model + the new columns (TASK-032 R1, spec 4.1/4.5)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from secretaria.models import Appointment, AppointmentReminder, Tenant, appointment_reminder as ar
from tests._reminder_fixtures import NOW, db, make_appointment, tenant  # noqa: F401


def test_string_constants_are_the_spec_vocabulary():
    assert ar.REMINDER_KINDS == ("custom", "day", "hour", "chat", "staff_confirm", "staff_edit")
    assert ar.REMINDER_KINDS_STAFF == ("staff_confirm", "staff_edit")
    assert ar.REMINDER_KINDS_UNPLANNED == ("chat", "staff_confirm", "staff_edit")
    assert all(len(kind) <= 16 for kind in ar.REMINDER_KINDS)  # kind is VARCHAR(16)
    assert ar.REMINDER_STATUSES == (
        "pending",
        "sending",
        "sent",
        "failed",
        "skipped",
        "cancelled",
    )
    assert ar.REMINDER_CHANNELS == ("whatsapp", "email", "chat")
    assert ar.REMINDER_ANSWERS == ("confirm", "cancel", "other")
    assert ar.REMINDER_WARN_KINDS == ("unconfirmed", "delivery_failed")


async def test_new_columns_have_safe_defaults(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=3))
    async with db() as session:
        fresh_appt = await session.get(Appointment, appt.id)
        fresh_tenant = await session.get(Tenant, tenant.id)
    assert fresh_appt.confirmation_count == 0
    assert fresh_appt.first_confirmed_at is None
    assert fresh_appt.last_confirmed_at is None
    # `tenant` fixture sets the flag explicitly; a bare Tenant must default OFF.
    async with db() as session:
        bare = Tenant(id=uuid4(), clinic_name="Bare", phone_number_id=None)
        session.add(bare)
        await session.commit()
        await session.refresh(bare)
    assert bare.reminders_v2_enabled is False
    assert bare.reminder_extra_lead_minutes is None
    assert fresh_tenant.reminders_v2_enabled is True


async def test_reminder_row_defaults(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=3))
    async with db() as session:
        row = AppointmentReminder(
            tenant_id=tenant.id,
            appointment_id=appt.id,
            patient_id=appt.patient_id,
            kind=ar.REMINDER_KIND_DAY,
            appointment_start_at=appt.start_at,
            due_at=appt.start_at - timedelta(days=1),
        )
        session.add(row)
        await session.commit()
        loaded = (await session.scalars(select(AppointmentReminder))).one()
    assert loaded.status == ar.REMINDER_STATUS_PENDING
    assert loaded.channel == ar.REMINDER_CHANNEL_WHATSAPP
    assert loaded.with_prompt is True
    assert loaded.attempts == 0
    assert loaded.sent_at is None and loaded.answered_at is None and loaded.answer is None
    assert loaded.warn_due_at is None and loaded.warned_at is None and loaded.warn_kind is None


async def test_one_row_per_appointment_kind_and_version(db, tenant):  # noqa: F811
    appt = await make_appointment(db, tenant, start_at=NOW + timedelta(days=3))

    def _row(start: datetime) -> AppointmentReminder:
        return AppointmentReminder(
            tenant_id=tenant.id,
            appointment_id=appt.id,
            patient_id=appt.patient_id,
            kind=ar.REMINDER_KIND_HOUR,
            appointment_start_at=start,
            due_at=start - timedelta(hours=1),
        )

    start = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    async with db() as session:
        session.add(_row(start))
        await session.commit()
    async with db() as session:
        session.add(_row(start))  # same (appointment, kind, version)
        with pytest.raises(IntegrityError):
            await session.commit()
    async with db() as session:
        session.add(_row(start + timedelta(days=1)))  # a new version is fine
        await session.commit()
