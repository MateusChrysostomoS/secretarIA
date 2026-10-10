"""Review regressions: real two-session interleavings at clinic-action boundaries."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from secretaria.core.database import Base
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.services import clinic_action_notice as notices, reminder_schedule as schedule
from tests._r7_support import (
    acting,  # noqa: F401
    r7,  # noqa: F401
    sent,
    setup_world,
    staff_rows,
)


@pytest_asyncio.fixture
async def db(tmp_path):
    # Independent connections: an injected winner must commit without committing
    # or destroying the losing session's savepoint (StaticPool would share it).
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'races.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def _confirm_notice(db, world):
    async with db() as session:
        return await notices.notify_staff_confirmation(
            session,
            await session.get(Tenant, world.tenant.id),
            await session.get(Appointment, world.appointment.id),
            await session.get(Patient, world.patient.id),
            allow_paid=False,
            now=datetime.now(UTC),
        )


async def test_i1_winner_between_precheck_and_ensure_sends_one_card(db, acting, monkeypatch):
    world = await setup_world(db, acting, status=AppointmentStatus.CONFIRMED)
    original = schedule.current_staff_notice_row
    injected = False

    async def current(session, appointment, **kwargs):
        nonlocal injected
        row = await original(session, appointment, **kwargs)
        if not injected:
            injected = True
            winner = await _confirm_notice(db, world)
            assert winner.delivered
        return row

    monkeypatch.setattr(schedule, "current_staff_notice_row", current)
    loser = await _confirm_notice(db, world)
    assert loser is None
    assert len(sent()) == 1
    assert len(await staff_rows(db, world.appointment.id, "staff_confirm")) == 1


async def test_i1_unique_insert_loser_recovers_without_second_send(db, acting, monkeypatch):
    world = await setup_world(db, acting, status=AppointmentStatus.CONFIRMED)
    from sqlalchemy.ext.asyncio import AsyncSession

    from secretaria.models import AppointmentReminder

    original = AsyncSession.flush
    injected = False

    async def flush(session, *args, **kwargs):
        nonlocal injected
        if not injected and any(isinstance(row, AppointmentReminder) for row in session.new):
            injected = True
            winner = await _confirm_notice(db, world)
            assert winner.delivered
        return await original(session, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", flush)
    loser = await _confirm_notice(db, world)
    assert loser is None
    assert len(sent()) == 1
    assert len(await staff_rows(db, world.appointment.id, "staff_confirm")) == 1


@pytest.mark.parametrize("target", ["scheduled", "attended", "no_show", "confirmed"])
async def test_i2_closed_winner_is_not_overwritten_or_notified(
    client, db, acting, monkeypatch, target
):
    from sqlalchemy import update

    from secretaria.api.hub import calendar as hub
    from tests._r7_support import patch_status
    from tests._reminders_v2 import reload_appointment

    start = (
        datetime.now(UTC) + timedelta(days=3)
        if target == "confirmed"
        else datetime.now(UTC) - timedelta(hours=1)
    )
    world = await setup_world(db, acting, start_at=start)
    original = hub._get_appointment
    money = []

    async def closed_winner(*args, **kwargs):
        stale = await original(*args, **kwargs)
        async with db() as winner:
            await winner.execute(
                update(Appointment)
                .where(Appointment.id == stale.id)
                .values(status=AppointmentStatus.CANCELLED)
            )
            await winner.commit()
        return stale

    async def hook(*args, **kwargs):
        money.append(1)

    monkeypatch.setattr(hub, "_get_appointment", closed_winner)
    monkeypatch.setattr(hub.deposit_lifecycle, "on_no_show", hook)
    response = await patch_status(client, world.appointment.id, target)
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "not_live"
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CANCELLED
    assert money == [] and sent() == []


async def test_i2_future_move_winner_cannot_be_marked_no_show(client, db, acting, monkeypatch):
    from sqlalchemy import update

    from secretaria.api.hub import calendar as hub
    from tests._r7_support import patch_status
    from tests._reminders_v2 import reload_appointment

    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(hours=1))
    future = datetime.now(UTC) + timedelta(days=2)
    original = hub._get_appointment
    money = []

    async def moved_winner(*args, **kwargs):
        stale = await original(*args, **kwargs)
        async with db() as winner:
            await winner.execute(
                update(Appointment)
                .where(Appointment.id == stale.id)
                .values(start_at=future, status=AppointmentStatus.RESCHEDULED)
            )
            await winner.commit()
        return stale

    async def hook(*args, **kwargs):
        money.append(1)

    monkeypatch.setattr(hub, "_get_appointment", moved_winner)
    monkeypatch.setattr(hub.deposit_lifecycle, "on_no_show", hook)
    response = await patch_status(client, world.appointment.id, "no_show")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "no_show_before_start"
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.RESCHEDULED
    assert money == [] and sent() == []


async def test_i2_confirmation_body_cannot_bind_to_a_new_start_after_content_load(
    db, acting, monkeypatch
):
    from sqlalchemy import update

    world = await setup_world(db, acting, status=AppointmentStatus.CONFIRMED)
    original = notices.load_reminder_content

    async def content_before_move(*args, **kwargs):
        content = await original(*args, **kwargs)
        async with db() as winner:
            await winner.execute(
                update(Appointment)
                .where(Appointment.id == world.appointment.id)
                .values(start_at=world.start_at + timedelta(days=1))
            )
            await winner.commit()
        return content

    monkeypatch.setattr(notices, "load_reminder_content", content_before_move)
    assert await _confirm_notice(db, world) is None
    assert sent() == [] and await staff_rows(db, world.appointment.id, "staff_confirm") == []
