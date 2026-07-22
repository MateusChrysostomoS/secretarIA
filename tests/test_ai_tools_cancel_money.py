"""Tests for ai/tools.py's Pix-deposit money hook on the LLM cancel path
(PROMPT S3 section 4, item 2): `_mark_appointment_cancelled` / `cancel_event`.

Same in-memory-sqlite + `_calendar_ctx`/`_tenant_id_ctx` injection pattern as
test_multi_unit_plugin.py (`ai/tools.py` imports `async_session_factory`
lazily, so only `core.database`'s module attribute needs patching).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import tools as ai_tools  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)


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


@pytest.fixture(autouse=True)
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    yield


class _FakeCalendarService:
    """cancel_event is a no-op — this file tests the DB-side money hook
    wiring, not the Calendar call itself (see services/calendar.py's own
    tests for that)."""

    def __init__(self) -> None:
        self.tzinfo = UTC
        self.cancelled: list[str] = []

    async def cancel_event(self, event_id: str) -> None:
        self.cancelled.append(event_id)


@contextmanager
def _agent_context(tenant_id, calendar=None):
    tok_tid = ai_tools._tenant_id_ctx.set(tenant_id)
    tok_cal = ai_tools._calendar_ctx.set(calendar)
    try:
        yield
    finally:
        ai_tools._tenant_id_ctx.reset(tok_tid)
        ai_tools._calendar_ctx.reset(tok_cal)


async def _seed(
    db,
    *,
    appointment_start_at: datetime | None = None,
    pix_refund_window_hours: int = 24,
    pix_retention_policy: str = "total",
):
    start_at = appointment_start_at or (datetime.now(UTC) + timedelta(hours=1))
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            pix_deposit_enabled=True,
            pix_refund_window_hours=pix_refund_window_hours,
            pix_retention_policy=pix_retention_policy,
        )
        session.add(tenant)
        await session.flush()
        appointment = Appointment(
            tenant_id=tenant.id,
            google_event_id="evt-cancel-1",
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(appointment)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(appointment)
        return tenant, appointment


async def _seed_deposit(db, appointment, *, status=PixDepositStatus.PAID) -> PixDeposit:
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=appointment.tenant_id,
            appointment_id=appointment.id,
            patient_id=None,
            asaas_payment_id=f"pay-{uuid4()}",
            amount_cents=10000,
            percent_applied=30,
            status=status,
        )
        session.add(deposit)
        await session.commit()
        await session.refresh(deposit)
        return deposit


# --------------------------------------------------------------------------
# cancel_event tool: notice relayed via the "note" field
# --------------------------------------------------------------------------


async def test_cancel_event_appends_deposit_notice_to_note_field(db):
    tenant, appt = await _seed(db, appointment_start_at=datetime.now(UTC) + timedelta(hours=1))
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)
    calendar = _FakeCalendarService()

    with _agent_context(tenant.id, calendar=calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-cancel-1"})

    assert result["status"] == "cancelled"
    assert "retido pela clínica" in result["note"]
    assert calendar.cancelled == ["evt-cancel-1"]

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appt.id)
        )
        assert deposit.status == PixDepositStatus.CANCELLED_RETAINED


async def test_cancel_event_without_deposit_has_no_note_field(db):
    tenant, appt = await _seed(db)
    calendar = _FakeCalendarService()

    with _agent_context(tenant.id, calendar=calendar):
        result = await ai_tools.cancel_event.ainvoke({"event_id": "evt-cancel-1"})

    assert result == {"status": "cancelled"}
    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        assert refreshed.status == AppointmentStatus.CANCELLED


async def test_mark_appointment_cancelled_no_tenant_context_returns_none(db):
    """Dev-script scaffolding (no tenant context) never crashes and never
    issues a tenant-unscoped UPDATE."""
    notice = await ai_tools._mark_appointment_cancelled("evt-cancel-1")
    assert notice is None
