"""cancel_replaced_appointment: guards, money hook, transition (TASK-032 R3, spec §4.3)."""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.services import appointment_replacement as replacement
from secretaria.services.payments import deposit_lifecycle
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment, seed_paid_deposit, seed_world


def _future(days: int = 3) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def _new_booking(session, world) -> Appointment:
    start = _future(5)
    new = Appointment(
        tenant_id=world.tenant.id,
        patient_id=world.patient.id,
        conversation_id=world.conversation.id,
        google_event_id="evt-new",
        appointment_type="Consulta",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(new)
    await session.flush()
    return new


async def _replace(db, world, *, replaced_id=None, patient_id=None, tenant=None):  # noqa: F811
    async with db() as session:
        async with session.begin():
            new = await _new_booking(session, world)
            return await replacement.cancel_replaced_appointment(
                session,
                tenant=tenant or await session.get(Tenant, world.tenant.id),
                patient_id=patient_id or world.patient.id,
                replaced_id=replaced_id or world.appointment.id,
                new_appointment_id=new.id,
            )


async def test_a_live_future_original_is_cancelled(db):  # noqa: F811
    world = await seed_world(db, start_at=_future(), google_event_id="evt-old")

    replaced = await _replace(db, world)

    assert replaced.appointment_id == world.appointment.id
    assert replaced.google_event_id == "evt-old"
    assert replaced.money_note is None
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED


async def test_another_patients_appointment_is_never_cancelled(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    async with db() as session:
        other = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900003333")
        session.add(other)
        await session.commit()

    assert await _replace(db, world, patient_id=other.id) is None
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_another_clinics_appointment_is_never_cancelled(db):  # noqa: F811
    mine = await seed_world(db, start_at=_future())
    theirs = await seed_world(db, start_at=_future(), phone_number_id="pnid-2")

    assert await _replace(db, mine, replaced_id=theirs.appointment.id) is None
    assert (
        await reload_appointment(db, theirs.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_a_cancelled_or_past_original_is_left_alone(db):  # noqa: F811
    cancelled = await seed_world(db, start_at=_future(), status=AppointmentStatus.CANCELLED)
    past = await seed_world(
        db, start_at=datetime.now(UTC) - timedelta(hours=2), phone_number_id="pnid-2"
    )

    assert await _replace(db, cancelled) is None
    assert await _replace(db, past) is None
    assert (await reload_appointment(db, past.appointment.id)).status == AppointmentStatus.SCHEDULED


async def test_the_new_booking_never_replaces_itself(db):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    async with db() as session:
        async with session.begin():
            tenant = await session.get(Tenant, world.tenant.id)
            result = await replacement.cancel_replaced_appointment(
                session,
                tenant=tenant,
                patient_id=world.patient.id,
                replaced_id=world.appointment.id,
                new_appointment_id=world.appointment.id,
            )
    assert result is None
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.SCHEDULED


async def test_the_money_hook_runs_and_its_notice_is_returned(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, start_at=_future())
    await seed_paid_deposit(db, world)
    calls = []

    async def _hook(session, *, tenant, appointment, waba_token=None, now=None):
        calls.append(appointment.id)
        return "retained"

    monkeypatch.setattr(deposit_lifecycle, "on_appointment_cancelled", _hook)

    replaced = await _replace(db, world)

    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, world.appointment.id)
        expected = deposit_lifecycle.cancellation_notice("retained", tenant, deposit)
    assert calls == [world.appointment.id]
    assert replaced.money_note == expected


async def test_a_failing_money_hook_still_cancels_the_original(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, start_at=_future())

    async def _boom(session, **kwargs):
        raise RuntimeError("asaas down")

    monkeypatch.setattr(deposit_lifecycle, "on_appointment_cancelled", _boom)

    replaced = await _replace(db, world)

    assert replaced is not None and replaced.money_note is None
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED
