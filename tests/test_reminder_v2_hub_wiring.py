"""Hub calendar endpoints call the reminder hooks (TASK-032 R2). Harness copied from
tests/test_hub_calendar_money.py: in-memory DB, fake Calendar, fake arq pool."""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from fastapi import Depends
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub import calendar as hub_calendar
from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant
from secretaria.services import reminder_hooks
from secretaria.services.tenant_config import set_google_refresh_token
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import HookSpy

CALENDAR = "/tenants/me/calendar"


class _FakeCalendarService:
    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def cancel_event(self, event_id: str) -> None:
        return None

    async def update_event(self, event_id: str, start: datetime, end: datetime) -> dict:
        return {"id": event_id}

    async def create_event(self, start, end, summary, description="") -> dict:
        return {"id": f"evt-{uuid4()}", "htmlLink": "https://calendar.example/evt"}


class _FakeArqPool:
    async def enqueue_job(self, name: str, *args) -> None:
        return None


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:  # noqa: F811
    async with db() as session:
        row = Tenant(
            id=uuid4(), clinic_name="Clinic", phone_number_id=None, reminders_v2_enabled=True
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
    async with db() as session:
        await set_google_refresh_token(session, row.id, "fake-refresh-token")
        await session.commit()
    return row


@pytest.fixture(autouse=True)
def _override(db, tenant, monkeypatch):  # noqa: F811
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    monkeypatch.setattr(hub_calendar, "CalendarService", _FakeCalendarService)
    app.state.arq_pool = _FakeArqPool()
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)
    app.state.arq_pool = None


@pytest.fixture
def spy(monkeypatch) -> HookSpy:
    return HookSpy().install(monkeypatch, reminder_hooks)


async def _switch(db, tenant, on: bool) -> None:  # noqa: F811
    async with db() as session:
        (await session.get(Tenant, tenant.id)).reminders_v2_enabled = on
        await session.commit()


async def _patient(db, tenant) -> Patient:  # noqa: F811
    async with db() as session:
        row = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511977776666", name="Maria")
        session.add(row)
        await session.commit()
        return row


async def _appointment(db, tenant, *, confirmation_count: int = 0) -> Appointment:  # noqa: F811
    patient = await _patient(db, tenant)
    start = datetime.now(UTC) + timedelta(days=3)
    async with db() as session:
        row = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id,
            google_event_id=f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start,
            end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
            phone="5511977776666",
            confirmation_count=confirmation_count,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row


def _window(days: int = 3) -> dict:
    start = datetime.now(UTC) + timedelta(days=days)
    return {"start": start.isoformat(), "end": (start + timedelta(minutes=30)).isoformat()}


# ---- create (Task 10) ---------------------------------------------------------


async def test_hub_booking_for_a_patient_plans_reminders(client: AsyncClient, db, tenant, spy):  # noqa: F811
    patient = await _patient(db, tenant)

    response = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "patient_id": str(patient.id)},
    )

    assert response.status_code == 201
    assert spy.calls == [("booked", UUID(response.json()["id"]))]


async def test_hub_booking_with_the_switch_off_plans_nothing(client: AsyncClient, db, tenant, spy):  # noqa: F811
    await _switch(db, tenant, False)
    patient = await _patient(db, tenant)

    response = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "patient_id": str(patient.id)},
    )

    assert response.status_code == 201
    assert spy.calls == []


async def test_hub_booking_without_a_patient_and_blocks_plan_nothing(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,
    spy,
):
    phone_only = await client.post(
        f"{CALENDAR}/appointments",
        json={**_window(), "summary": "Consulta", "phone": "5511900001111"},
    )
    block = await client.post(f"{CALENDAR}/blocks", json={**_window(4), "summary": "Almoço"})

    assert (phone_only.status_code, block.status_code) == (201, 201)
    assert spy.calls == []


# ---- cancel and reschedule (Task 11) ----------------------------------------


async def test_hub_cancel_cancels_the_reminders(client: AsyncClient, db, tenant, spy):  # noqa: F811
    appt = await _appointment(db, tenant)

    response = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True}
    )

    assert response.status_code == 200
    assert spy.calls == [("closed", appt.id, "cancelled")]


async def test_hub_cancel_with_the_switch_off_touches_nothing(client: AsyncClient, db, tenant, spy):  # noqa: F811
    await _switch(db, tenant, False)
    appt = await _appointment(db, tenant)

    await client.post(f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True})

    assert spy.calls == []


@pytest.mark.parametrize(
    ("on", "confirmations", "expected"),
    [(True, 0, True), (False, 0, False), (False, 1, True)],
)
async def test_hub_reschedule_replans_when_on_or_when_there_is_a_count_to_zero(
    client: AsyncClient,
    db,  # noqa: F811
    tenant,
    spy,
    on,
    confirmations,
    expected,  # noqa: F811
):
    await _switch(db, tenant, on)
    appt = await _appointment(db, tenant, confirmation_count=confirmations)
    new_start = datetime.now(UTC) + timedelta(days=6)

    response = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/reschedule",
        json={
            "new_start": new_start.isoformat(),
            "new_end": (new_start + timedelta(minutes=30)).isoformat(),
        },
    )

    assert response.status_code == 200
    assert spy.calls == ([("moved", appt.id)] if expected else [])
