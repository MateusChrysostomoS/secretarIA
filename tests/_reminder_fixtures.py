"""Shared in-memory fixtures for the reminder foundation tests (TASK-032 R1).

Import the fixtures into a test module with
`from tests._reminder_fixtures import db, tenant, other_tenant  # noqa: F401`.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant  # noqa: E402

# A fixed "now" so every schedule assertion is deterministic.
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _make_tenant(db, name: str, *, enabled: bool, extra_days: int | None) -> Tenant:
    async with db() as session:
        t = Tenant(
            id=uuid4(),
            clinic_name=name,
            phone_number_id=None,
            reminders_v2_enabled=enabled,
            reminder_extra_days_before=extra_days,
            reminder_extra_send_time="09:00" if extra_days else None,
            reminder_extra_lead_minutes=extra_days * 1440 if extra_days else None,
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    """Clinic with the feature ON and an extra reminder 5 days before at 09:00 (São Paulo).

    NOW is 12:00 UTC = 09:00 in São Paulo, so for an appointment at NOW + k days the extra
    reminder is exactly start - 5 days (the R1 assertions written in minutes still hold).
    """
    return await _make_tenant(db, "Clinic", enabled=True, extra_days=5)


@pytest_asyncio.fixture
async def other_tenant(db) -> Tenant:
    return await _make_tenant(db, "Other clinic", enabled=True, extra_days=5)


async def make_patient(db, tenant: Tenant) -> Patient:
    async with db() as session:
        p = Patient(tenant_id=tenant.id, wa_id=f"55119{uuid4().int % 10**8:08d}")
        session.add(p)
        await session.commit()
        await session.refresh(p)
        return p


async def make_appointment(
    db,
    tenant: Tenant,
    *,
    start_at: datetime,
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    with_patient: bool = True,
    google_event_id: str | None = None,
) -> Appointment:
    patient = await make_patient(db, tenant) if with_patient else None
    async with db() as session:
        appt = Appointment(
            tenant_id=tenant.id,
            patient_id=patient.id if patient else None,
            google_event_id=google_event_id or f"evt-{uuid4()}",
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
            phone="5511999999999",
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt
