"""Tests for api/hub/calendar.py's Pix-deposit money hooks (PROMPT S3
section 4, items 3/4) + deposit visibility (section 5).

api/hub/calendar.py had ZERO test coverage before this file — these are the
first. Same db/tenant/_override pattern as test_hub_config_pix.py; Calendar
is faked (see `_FakeCalendarService`) so no real Google API call is ever
attempted, and a fake arq pool on `app.state` lets the custom_message
notification path be asserted on without a real Redis/worker.
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

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub import calendar as hub_calendar  # noqa: E402
from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Patient,
    PixDeposit,
    PixDepositStatus,
    Tenant,
)
from secretaria.services.patient_context import load_upcoming_appointments  # noqa: E402
from secretaria.services.tenant_config import set_google_refresh_token  # noqa: E402

CALENDAR = "/tenants/me/calendar"


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


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    async with db() as session:
        t = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=None,
            pix_deposit_enabled=True,
            pix_refund_window_hours=24,
            pix_retention_policy="total",
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


class _FakeCalendarService:
    """cancel_event/update_event are no-ops — this file tests the money-hook
    + response-shape wiring, not the Calendar call itself."""

    def __init__(self) -> None:
        self.cancelled: list[str] = []
        self.updated: list[tuple] = []

    @classmethod
    def from_tenant_config(cls, config):
        return cls()

    async def cancel_event(self, event_id: str) -> None:
        self.cancelled.append(event_id)

    async def update_event(self, event_id: str, start: datetime, end: datetime) -> dict:
        self.updated.append((event_id, start, end))
        return {"id": event_id}

    async def create_event(self, start, end, summary, description="") -> dict:
        return {"id": f"evt-{uuid4()}", "htmlLink": "https://calendar.example/evt-new"}

    async def check_availability(self, start, end):
        return []


class _FakeArqPool:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def enqueue_job(self, name: str, *args) -> None:
        self.calls.append((name, *args))


@pytest.fixture(autouse=True)
def _override(db, tenant, monkeypatch: pytest.MonkeyPatch):
    from fastapi import Depends

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


async def _connect_calendar(db, tenant) -> None:
    async with db() as session:
        await set_google_refresh_token(session, tenant.id, "fake-refresh-token")
        await session.commit()


async def _seed_appointment(
    db,
    tenant,
    *,
    start_at: datetime,
    google_event_id: str = "evt-1",
    status: AppointmentStatus = AppointmentStatus.SCHEDULED,
    phone: str | None = "5511999999999",
) -> Appointment:
    async with db() as session:
        appt = Appointment(
            tenant_id=tenant.id,
            google_event_id=google_event_id,
            appointment_type="Consulta",
            start_at=start_at,
            end_at=start_at + timedelta(minutes=30),
            status=status,
            phone=phone,
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt


async def _seed_deposit(
    db,
    appointment,
    *,
    status: PixDepositStatus = PixDepositStatus.PAID,
    reschedule_count: int = 0,
) -> PixDeposit:
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
            reschedule_count=reschedule_count,
        )
        session.add(deposit)
        await session.commit()
        await session.refresh(deposit)
        return deposit


# --------------------------------------------------------------------------
# POST /appointments/{id}/cancel
# --------------------------------------------------------------------------


async def test_post_cancel_triggers_lifecycle_and_returns_deposit_outcome(
    client: AsyncClient, db, tenant
):
    await _connect_calendar(db, tenant)
    appt = await _seed_appointment(db, tenant, start_at=datetime.now(UTC) + timedelta(hours=1))
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    response = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["deposit_outcome"] == "retained"  # total policy, inside window
    assert body["deposit_status"] == PixDepositStatus.CANCELLED_RETAINED.value

    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appt.id)
        )
        assert deposit.status == PixDepositStatus.CANCELLED_RETAINED


async def test_post_cancel_without_deposit_returns_none_outcome(client: AsyncClient, db, tenant):
    await _connect_calendar(db, tenant)
    appt = await _seed_appointment(db, tenant, start_at=datetime.now(UTC) + timedelta(hours=1))

    response = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/cancel", json={"confirm": True}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["deposit_outcome"] is None
    assert body["deposit_status"] is None


async def test_post_cancel_passes_deposit_notice_to_the_cancellation_job(
    client: AsyncClient, db, tenant
):
    """The deposit notice still rides along with the cancellation — it just
    rides on the composed notice now, not on a doctor-typed body.

    Was `..._appends_deposit_notice_to_custom_message`, asserting the old
    opt-in contract: `custom_message` WAS the whole message and no message
    meant no notification at all. That field is gone (schemas/calendar.py
    explains why), the notice is composed server-side, and the enqueue is
    unconditional — so this now checks the deposit line is handed to the job
    rather than concatenated here.
    """
    from secretaria.main import app

    await _connect_calendar(db, tenant)
    appt = await _seed_appointment(db, tenant, start_at=datetime.now(UTC) + timedelta(hours=1))
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    response = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/cancel",
        json={"confirm": True, "justification": "Imprevisto do médico"},
    )
    assert response.status_code == 200

    pool: _FakeArqPool = app.state.arq_pool
    assert len(pool.calls) == 1
    (
        name,
        _tenant_id_arg,
        _appointment_id_arg,
        _professional_name,
        justification,
        extra_notice,
        allow_paid,
    ) = pool.calls[0]
    assert name == "send_cancellation_notice"
    assert justification == "Imprevisto do médico"
    assert "retido pela clínica" in extra_notice  # deposit notice rides along
    assert allow_paid is False  # not authorised => never the paid path


# --------------------------------------------------------------------------
# PATCH /appointments/{id}/status
# --------------------------------------------------------------------------


async def test_patch_status_cancelled_triggers_on_appointment_cancelled(
    client: AsyncClient, db, tenant
):
    appt = await _seed_appointment(db, tenant, start_at=datetime.now(UTC) + timedelta(hours=1))
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    response = await client.patch(
        f"{CALENDAR}/appointments/{appt.id}/status", json={"status": "cancelled"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "cancelled"
    assert body["deposit_outcome"] == "retained"
    assert body["deposit_status"] == PixDepositStatus.CANCELLED_RETAINED.value


async def test_patch_status_no_show_triggers_on_no_show(client: AsyncClient, db, tenant):
    appt = await _seed_appointment(db, tenant, start_at=datetime.now(UTC) - timedelta(hours=1))
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    response = await client.patch(
        f"{CALENDAR}/appointments/{appt.id}/status", json={"status": "no_show"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "no_show"
    assert body["deposit_outcome"] == "retained"  # no-show: PAID always retained in full
    assert body["deposit_status"] == PixDepositStatus.NO_SHOW_RETAINED.value


async def test_patch_status_confirmed_leaves_deposit_untouched_but_visible(
    client: AsyncClient, db, tenant
):
    appt = await _seed_appointment(db, tenant, start_at=datetime.now(UTC) + timedelta(hours=1))
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID)

    response = await client.patch(
        f"{CALENDAR}/appointments/{appt.id}/status", json={"status": "confirmed"}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["deposit_outcome"] is None  # no money event for this transition
    assert body["deposit_status"] == PixDepositStatus.PAID.value  # still surfaced


# --------------------------------------------------------------------------
# POST /appointments/{id}/reschedule
# --------------------------------------------------------------------------


async def test_post_reschedule_updates_window_and_does_not_increment_count(
    client: AsyncClient, db, tenant
):
    await _connect_calendar(db, tenant)
    original_start = datetime.now(UTC) + timedelta(days=2)
    appt = await _seed_appointment(db, tenant, start_at=original_start)
    await _seed_deposit(db, appt, status=PixDepositStatus.PAID, reschedule_count=0)

    new_start = datetime.now(UTC) + timedelta(days=5)
    new_end = new_start + timedelta(minutes=45)
    response = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/reschedule",
        json={"new_start": new_start.isoformat(), "new_end": new_end.isoformat()},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "rescheduled"
    assert body["deposit_status"] == PixDepositStatus.PAID.value

    async with db() as session:
        refreshed = await session.get(Appointment, appt.id)
        stored_start = (
            refreshed.start_at
            if refreshed.start_at.tzinfo
            else refreshed.start_at.replace(tzinfo=UTC)
        )
        stored_end = (
            refreshed.end_at if refreshed.end_at.tzinfo else refreshed.end_at.replace(tzinfo=UTC)
        )
        # BUGFIX (PROMPT S3): the row now actually mirrors the moved window.
        assert abs((stored_start - new_start).total_seconds()) < 2
        assert abs((stored_end - new_end).total_seconds()) < 2

        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appt.id)
        )
        # Doctor-initiated (hub) reschedule must NEVER consume the patient's
        # own pix_reschedule_limit allowance.
        assert deposit.reschedule_count == 0


async def test_hub_rescheduled_appointment_stays_upcoming_for_the_patient(
    client: AsyncClient, db, tenant
):
    """PROMPT_FIX_16: the hub and the patient-facing readers must agree.

    A doctor-initiated move used to leave the row in RESCHEDULED, which
    `load_upcoming_appointments` (the ONE query behind the manage flow, the
    greeting opening state and `list_patient_appointments`) treated as gone —
    so the patient could no longer manage the very booking the doctor had just
    moved for them.
    """
    await _connect_calendar(db, tenant)
    appt = await _seed_appointment(db, tenant, start_at=datetime.now(UTC) + timedelta(days=2))
    # A patient-owned booking (the hub's own seed is a block slot).
    async with db() as session:
        patient = Patient(tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add(patient)
        await session.flush()
        row = await session.get(Appointment, appt.id)
        row.patient_id = patient.id
        await session.commit()
        patient_id = patient.id

    new_start = datetime.now(UTC) + timedelta(days=5)
    response = await client.post(
        f"{CALENDAR}/appointments/{appt.id}/reschedule",
        json={
            "new_start": new_start.isoformat(),
            "new_end": (new_start + timedelta(minutes=30)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "rescheduled"

    async with db() as session:
        upcoming = await load_upcoming_appointments(session, tenant.id, patient_id)

    assert [row["id"] for row in upcoming] == [str(appt.id)]


# --------------------------------------------------------------------------
# AppointmentRead.deposit_status on a plain create (no deposit possible yet)
# --------------------------------------------------------------------------


async def test_create_appointment_deposit_status_defaults_to_none(client: AsyncClient, db, tenant):
    await _connect_calendar(db, tenant)
    start = datetime.now(UTC) + timedelta(days=1)
    response = await client.post(
        f"{CALENDAR}/appointments",
        json={
            "start": start.isoformat(),
            "end": (start + timedelta(minutes=30)).isoformat(),
            "summary": "Consulta - Paciente Teste",
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["deposit_status"] is None
    assert body["deposit_outcome"] is None
