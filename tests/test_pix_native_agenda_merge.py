# ruff: noqa: F811
"""TASK-066: the merged Pix confirmation must work on main's native agenda."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from secretaria.models import CalendarEvent
from secretaria.services import booking_promotion
from secretaria.services.local_calendar import LocalCalendarService, create_event_exclusive
from tests._local_agenda import (
    agenda,
    runtime_config,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):
    wire_local_agenda(monkeypatch, db)


async def test_paid_booking_can_resolve_the_clinic_agenda_without_google(db, monkeypatch):
    tenant = await seed_tenant(db)

    async def config(_session, _tenant):
        return runtime_config(tenant.id)

    monkeypatch.setattr(booking_promotion, "load_tenant_config", config)
    async with db() as session:
        calendar = await booking_promotion.calendar_for_booking(session, tenant, None)
    assert isinstance(calendar, LocalCalendarService)
    assert calendar.tenant_id == tenant.id


async def test_retry_of_paid_local_event_is_idempotent_and_is_not_its_own_conflict(db):
    tenant = await seed_tenant(db)
    calendar = agenda(tenant.id)
    deposit = SimpleNamespace(id=uuid4())
    event_id = booking_promotion.event_id_for(deposit.id, calendar=calendar)
    assert event_id == f"loc_{deposit.id.hex}"
    start = tomorrow_at(10)
    end = start + timedelta(minutes=30)
    first = await create_event_exclusive(
        calendar, start=start, end=end, summary="Consulta", event_id=event_id,
    )
    retry = await create_event_exclusive(
        calendar, start=start, end=end, summary="Consulta", event_id=event_id,
    )
    assert first["id"] == retry["id"] == event_id
    snapshot = SimpleNamespace(
        tenant_id=tenant.id, professional_id=None, start_at=start, end_at=end,
    )
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(CalendarEvent)) == 1
        assert await booking_promotion.slot_conflict(
            session, calendar, snapshot, deposit=deposit,
        ) is None
