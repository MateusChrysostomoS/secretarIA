"""TASK-085 R S5 - a consultation SUSPENDED by a change waiting for the Pix of its difference is
hidden from everything the patient sees (upcoming lists, the AI context, the greeting, the
reminders); the clinic hub keeps seeing it. Rows without a `suspend` adjustment (clinics
without sinal, legacy `mode IS NULL`, `applied`, closed changes) behave exactly as before.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import Appointment, AppointmentStatus, PixDeposit, Tenant
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    MODE_APPLIED,
    MODE_SUSPEND,
    STATUS_APPLIED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_KEPT_OLD,
    DepositAdjustment,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.plugins import reminders
from secretaria.services import patient_context, reminder_opening, reminder_schedule
from tests.before_support import make_world
from tests.payments_support import make_sessionmaker
from tests.test_reminders_plugin import (
    _entitled_fake,
    _fake_get_waba_token,
    _FakeWhatsAppClient,
)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _appointment(session, world, *, start, mode=None, status=None) -> Appointment:
    appointment = Appointment(
        id=uuid4(),
        tenant_id=world.tenant_id,
        patient_id=world.patient.id,
        google_event_id=f"evt-{uuid4().hex[:8]}",
        phone=world.patient.wa_id,
        appointment_type="Consulta",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(appointment)
    await session.flush()
    if status is None:
        return appointment
    deposit = PixDeposit(
        tenant_id=world.tenant_id,
        appointment_id=appointment.id,
        patient_id=world.patient.id,
        amount_cents=6000,
        percent_applied=30,
        status=PixDepositStatus.PAID,
        provider="asaas",
        provider_charge_id=f"pay_{uuid4().hex[:8]}",
        deposit_timing="after",
    )
    session.add(deposit)
    await session.flush()
    session.add(
        DepositAdjustment(
            id=uuid4(),
            tenant_id=world.tenant_id,
            deposit_id=deposit.id,
            appointment_id=appointment.id,
            kind=KIND_INCREASE,
            mode=mode,
            status=status,
            old_service="Consulta",
            new_service="Avaliação",
            diff_cents=3000,
        )
    )
    await session.flush()
    return appointment


def _days(n: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=n)


# --- patient lists / AI context / greeting -------------------------------------------------


async def test_a_suspended_consultation_leaves_the_patient_upcoming_list(db):
    async with db() as session:
        world = await make_world(session, timing="after")
        suspended = await _appointment(
            session, world, start=_days(2), mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT
        )
        choosing = await _appointment(
            session, world, start=_days(3), mode=MODE_SUSPEND, status=STATUS_AWAITING_CHOICE
        )
        plain = await _appointment(session, world, start=_days(4))
        await session.commit()
    async with db() as session:
        rows = await patient_context.load_upcoming_appointments(
            session, world.tenant_id, world.patient.id
        )
        # the clinic hub reads the table directly: the rows are all still there
        stored = set(await session.scalars(select(Appointment.id)))
    assert [r["id"] for r in rows] == [str(plain.id)]
    assert {suspended.id, choosing.id, plain.id} <= stored


async def test_only_a_suspended_consultation_means_no_upcoming_for_the_greeting(db):
    async with db() as session:
        world = await make_world(session, timing="after")
        await _appointment(
            session, world, start=_days(2), mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT
        )
        await session.commit()
    async with db() as session:
        assert not await patient_context.has_upcoming_appointment(
            session, world.tenant_id, world.patient.id
        )
        assert (
            await patient_context.load_upcoming_appointments(
                session, world.tenant_id, world.patient.id
            )
            == []
        )


@pytest.mark.parametrize(
    ("mode", "status"),
    [
        (None, None),  # clinic without sinal / no change
        (None, STATUS_AWAITING_PAYMENT),  # legacy row, today semantics
        (MODE_APPLIED, STATUS_AWAITING_PAYMENT),  # D-R5: already written, patient sees it
        (MODE_SUSPEND, STATUS_APPLIED),  # closed changes
        (MODE_SUSPEND, STATUS_KEPT_OLD),
    ],
)
async def test_characterization_nothing_else_is_hidden(db, mode, status):
    async with db() as session:
        world = await make_world(session, timing="after")
        appointment = await _appointment(session, world, start=_days(2), mode=mode, status=status)
        await session.commit()
    async with db() as session:
        rows = await patient_context.load_upcoming_appointments(
            session, world.tenant_id, world.patient.id
        )
        assert [r["id"] for r in rows] == [str(appointment.id)]
        assert await patient_context.has_upcoming_appointment(
            session, world.tenant_id, world.patient.id
        )
        nearest = await reminder_opening.nearest_live_appointment(
            session, world.tenant_id, world.patient.id, now=datetime.now(UTC)
        )
        assert nearest is not None and nearest.id == appointment.id


async def test_a_suspension_of_another_clinic_hides_nothing(db):
    async with db() as session:
        world = await make_world(session, timing="after")
        other = await make_world(session, timing="after")
        await _appointment(
            session, other, start=_days(2), mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT
        )
        mine = await _appointment(session, world, start=_days(2))
        await session.commit()
    async with db() as session:
        rows = await patient_context.load_upcoming_appointments(
            session, world.tenant_id, world.patient.id
        )
    assert [r["id"] for r in rows] == [str(mine.id)]


# --- reminder opening / planner ------------------------------------------------------------


async def test_the_reminder_opening_skips_a_suspended_nearest_consultation(db):
    async with db() as session:
        world = await make_world(session, timing="after")
        await _appointment(
            session, world, start=_days(1), mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT
        )
        later = await _appointment(session, world, start=_days(5))
        await session.commit()
    async with db() as session:
        nearest = await reminder_opening.nearest_live_appointment(
            session, world.tenant_id, world.patient.id, now=datetime.now(UTC)
        )
    assert nearest is not None and nearest.id == later.id


async def test_the_replan_reads_ignore_a_suspended_consultation(db):
    async with db() as session:
        world = await make_world(session, timing="after", reminders_v2_enabled=True)
        await _appointment(
            session, world, start=_days(4), mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT
        )
        await session.commit()
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant_id)
        tenant.reminder_extra_days_before = 1
        tenant.reminder_extra_send_time = "09:00"
        result = await reminder_schedule.replan_custom_reminders(
            session, tenant, now=datetime.now(UTC)
        )
        assert (result.moved, result.cancelled, result.created) == (0, 0, 0)
        assert not await reminder_schedule.pending_custom_reminders_need_replan(
            session, tenant, now=datetime.now(UTC)
        )


# --- the reminder cron -----------------------------------------------------------------------


@pytest.fixture
def _cron(monkeypatch, db):
    monkeypatch.setattr(reminders, "async_session_factory", db)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(reminders, "WhatsAppClient", _FakeWhatsAppClient)
    monkeypatch.setattr(reminders, "get_waba_token", _fake_get_waba_token)
    monkeypatch.setattr(reminders, "get_entitlements", _entitled_fake())


async def _cron_world(db, *, mode, status):
    async with db() as session:
        world = await make_world(session, timing="after")
        await _appointment(
            session,
            world,
            start=datetime.now(UTC) + timedelta(hours=24),
            mode=mode,
            status=status,
        )
        await session.commit()


async def test_the_reminder_cron_skips_a_suspended_consultation(db, _cron):
    await _cron_world(db, mode=MODE_SUSPEND, status=STATUS_AWAITING_PAYMENT)
    await reminders.send_appointment_reminders({"redis": None})
    assert not [c for c in _FakeWhatsAppClient.created if c.sent]


async def test_the_reminder_cron_still_reminds_everything_else(db, _cron):
    await _cron_world(db, mode=MODE_APPLIED, status=STATUS_AWAITING_PAYMENT)
    await reminders.send_appointment_reminders({"redis": None})
    assert [c for c in _FakeWhatsAppClient.created if c.sent]
