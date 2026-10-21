"""Shared helpers for the native-agenda tests (services/local_calendar.py, 2026-10-20).

`db` is the usual in-memory SQLite on a StaticPool; `wire_local_agenda` points the
local agenda's own session factory at it (the service opens its own sessions, exactly
as a Google call happens outside the caller's transaction).
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from secretaria.models import Professional, Tenant
from secretaria.services import local_calendar
from secretaria.services.local_calendar import LocalCalendarService
from secretaria.services.tenant_config import TenantRuntimeConfig

TZ_NAME = "America/Sao_Paulo"
WEEK_HOURS = {
    day: [{"start": "08:00", "end": "18:00"}]
    for day in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
}


def wire_local_agenda(monkeypatch, db) -> None:
    monkeypatch.setattr(local_calendar, "async_session_factory", db)


async def seed_tenant(db, *, hours: dict | None = None, name: str = "Clínica Local") -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name=name,
            phone_number_id=None,
            is_active=True,
            timezone=TZ_NAME,
            business_hours=WEEK_HOURS if hours is None else hours,
            appointment_types=[{"name": "Consulta", "duration_min": 30, "is_active": True}],
            reminders_v2_enabled=False,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def seed_professional(db, tenant: Tenant, name: str) -> Professional:
    async with db() as session:
        professional = Professional(id=uuid4(), tenant_id=tenant.id, name=name, is_active=True)
        session.add(professional)
        await session.commit()
        await session.refresh(professional)
        return professional


def runtime_config(tenant_id: UUID, *, token: str | None = None, professional_id=None):
    return TenantRuntimeConfig(
        tenant_id=tenant_id,
        clinic_name="Clínica Local",
        language="pt-BR",
        timezone=TZ_NAME,
        appointment_duration_min=30,
        appointment_types=[],
        business_hours=WEEK_HOURS,
        google_calendar_id="primary",
        google_refresh_token=token,
        professional_id=professional_id,
    )


def agenda(tenant_id: UUID, professional_id: UUID | None = None) -> LocalCalendarService:
    return LocalCalendarService.for_config(
        runtime_config(tenant_id), professional_id=professional_id
    )


def tomorrow_at(hour: int, minute: int = 0) -> datetime:
    """A clinic-local wall time tomorrow, as an aware UTC instant (São Paulo is UTC-3)."""
    day = (datetime.now(UTC) - timedelta(hours=3)).date() + timedelta(days=1)
    return datetime(day.year, day.month, day.day, hour + 3, minute, tzinfo=UTC)
