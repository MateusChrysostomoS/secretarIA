"""Tests for services/patient_context.py — the opening-state resolver + the
shared `load_upcoming_appointments` query — and its wiring into
workers/tasks.py (the conversation-opening greeting adapter + the
`_persist_inbound_message` gate) and the webhook ACK path.

DB-backed pieces mirror the in-memory-sqlite pattern from
test_agent_menu_tools.py: a real aiosqlite engine on StaticPool, monkeypatched
in place of the Postgres-backed `async_session_factory` on both
`core.database` and `workers.tasks`. All resolver calls pass an explicit
tz-aware `now=` so nothing depends on wall-clock.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api import webhook as webhook_api  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Appointment, AppointmentStatus, Patient, Tenant  # noqa: E402
from secretaria.services import patient_context  # noqa: E402
from secretaria.services.patient_context import (  # noqa: E402
    PatientOpeningContext,
    PatientOpeningState,
    resolve_patient_opening_state,
)
from secretaria.workers import tasks  # noqa: E402

# Fixed reference "now" for every resolver test below — tz-aware UTC, nothing
# depends on wall-clock. UPCOMING_SOON_HOURS / POST_CONSULT_WINDOW_HOURS both
# default to 48 (config.py), which every test below relies on.
NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


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
def _patch_session_factory(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


async def _seed_tenant_and_patient(db, *, wa_id: str = "5511999990000") -> tuple[Tenant, Patient]:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            timezone="America/Sao_Paulo",
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=wa_id, name="Maria")
        session.add(patient)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(patient)
        return tenant, patient


async def _add_appointment(
    db,
    tenant_id,
    patient_id,
    *,
    status: AppointmentStatus,
    start_at: datetime,
    end_at: datetime | None = None,
    appointment_type: str = "Consulta",
) -> Appointment:
    async with db() as session:
        appt = Appointment(
            id=uuid4(),
            tenant_id=tenant_id,
            patient_id=patient_id,
            google_event_id=str(uuid4()),
            appointment_type=appointment_type,
            start_at=start_at,
            end_at=end_at or (start_at + timedelta(minutes=30)),
            status=status,
        )
        session.add(appt)
        await session.commit()
        await session.refresh(appt)
        return appt


async def _resolve(db, tenant_id, patient_id, *, now: datetime) -> PatientOpeningContext | None:
    async with db() as session:
        return await resolve_patient_opening_state(session, tenant_id, patient_id, now=now)


# --------------------------------------------------------------------------
# resolve_patient_opening_state: the five states
# --------------------------------------------------------------------------


async def test_state_has_upcoming_soon(db) -> None:
    tenant, patient = await _seed_tenant_and_patient(db)
    nearer = await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.SCHEDULED,
        start_at=NOW + timedelta(hours=24),
    )
    await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.CONFIRMED,
        start_at=NOW + timedelta(hours=30),
    )

    context = await _resolve(db, tenant.id, patient.id, now=NOW)

    assert context.state is PatientOpeningState.HAS_UPCOMING_SOON
    assert len(context.future_appointments) == 2
    assert context.future_appointments[0]["id"] == str(nearer.id)  # nearest first


async def test_state_has_upcoming(db) -> None:
    """Beyond the 48h soon window, and CONFIRMED counts as upcoming too."""
    tenant, patient = await _seed_tenant_and_patient(db)
    await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.CONFIRMED,
        start_at=NOW + timedelta(hours=120),
    )

    context = await _resolve(db, tenant.id, patient.id, now=NOW)

    assert context.state is PatientOpeningState.HAS_UPCOMING
    assert len(context.future_appointments) == 1


async def test_state_just_had_consult(db) -> None:
    tenant, patient = await _seed_tenant_and_patient(db)
    past = await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.SCHEDULED,
        start_at=NOW - timedelta(hours=24),
    )

    context = await _resolve(db, tenant.id, patient.id, now=NOW)

    assert context.state is PatientOpeningState.JUST_HAD_CONSULT
    assert len(context.recent_past_appointments) == 1
    assert context.recent_past_appointments[0]["id"] == str(past.id)


async def test_state_returning_no_appointment_stuck_past_excluded(db) -> None:
    """A SCHEDULED row 10 days in the past (beyond the 48h lookback) must NOT
    read as "just had a consult" — but it DOES count toward has_history."""
    tenant, patient = await _seed_tenant_and_patient(db)
    await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.SCHEDULED,
        start_at=NOW - timedelta(hours=240),
    )

    context = await _resolve(db, tenant.id, patient.id, now=NOW)

    assert context.state is PatientOpeningState.RETURNING_NO_APPOINTMENT
    assert context.has_history is True
    assert context.recent_past_appointments == []


async def test_state_new(db) -> None:
    tenant, patient = await _seed_tenant_and_patient(db)

    context = await _resolve(db, tenant.id, patient.id, now=NOW)

    assert context.state is PatientOpeningState.NEW
    assert context.has_history is False


async def test_cancelled_rows_never_count(db) -> None:
    """A cancelled future row never triggers HAS_UPCOMING*, and a cancelled
    past row never triggers JUST_HAD_CONSULT — but it DOES count toward
    has_history (the patient did exist before), landing on
    RETURNING_NO_APPOINTMENT."""
    tenant, patient = await _seed_tenant_and_patient(db)
    await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.CANCELLED,
        start_at=NOW + timedelta(hours=24),
    )
    await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.CANCELLED,
        start_at=NOW - timedelta(hours=24),
    )

    context = await _resolve(db, tenant.id, patient.id, now=NOW)

    assert context.state not in (
        PatientOpeningState.HAS_UPCOMING_SOON,
        PatientOpeningState.HAS_UPCOMING,
        PatientOpeningState.JUST_HAD_CONSULT,
    )
    assert context.state is PatientOpeningState.RETURNING_NO_APPOINTMENT
    assert context.has_history is True


async def test_unresolved_patient_returns_none_and_greeting_unchanged(db) -> None:
    async with db() as session:
        result = await resolve_patient_opening_state(session, uuid4(), None, now=NOW)
    assert result is None

    tenant = Tenant(id=uuid4(), clinic_name="Clinic", timezone="America/Sao_Paulo")
    assert tasks._adapt_greeting_to_state("Olá!", None, tenant) == "Olá!"


# --------------------------------------------------------------------------
# _adapt_greeting_to_state: MVP copy per state
# --------------------------------------------------------------------------


def test_adapt_greeting_upcoming_line() -> None:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", timezone="America/Sao_Paulo")
    start_at = NOW + timedelta(hours=24)
    context = PatientOpeningContext(
        state=PatientOpeningState.HAS_UPCOMING_SOON,
        future_appointments=[{"start_at": start_at}],
    )

    result = tasks._adapt_greeting_to_state("Olá!", context, tenant)

    assert "Olá!" in result
    assert "consulta marcada para" in result
    expected_when = start_at.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m às %H:%M")
    assert expected_when in result


def test_just_had_consult_neutral_vs_attended_copy() -> None:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", timezone="America/Sao_Paulo")

    neutral_context = PatientOpeningContext(
        state=PatientOpeningState.JUST_HAD_CONSULT,
        recent_past_appointments=[{"status": AppointmentStatus.SCHEDULED}],
    )
    neutral_result = tasks._adapt_greeting_to_state("Olá!", neutral_context, tenant)
    assert tasks.JUST_HAD_CONSULT_NEUTRAL_LINE in neutral_result
    assert "Como foi sua consulta" not in neutral_result

    attended_context = PatientOpeningContext(
        state=PatientOpeningState.JUST_HAD_CONSULT,
        recent_past_appointments=[{"status": AppointmentStatus.ATTENDED}],
    )
    attended_result = tasks._adapt_greeting_to_state("Olá!", attended_context, tenant)
    assert "Como foi sua consulta" in attended_result


# --------------------------------------------------------------------------
# Wiring: the gate fires only on the opening message, and only there
# --------------------------------------------------------------------------


async def test_opening_router_only_on_opening_message(db, monkeypatch: pytest.MonkeyPatch) -> None:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
            initial_flows={},
            greeting_message="Olá! Bem-vindo à Clínica.",
        )
        session.add(tenant)
        await session.flush()
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511988887777", name="Maria")
        session.add(patient)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(patient)

    calls: list[tuple] = []

    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        calls.append((tenant_id, patient_id))
        return None

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _fake_resolve)

    reply1 = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=patient.wa_id,
        patient_name="Maria",
        wam_id="wamid.open.1",
        body="oi",
    )
    assert calls == [(tenant.id, patient.id)]
    assert reply1 is not None
    assert reply1.greeting_override == tenant.greeting_message

    reply2 = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=patient.wa_id,
        patient_name="Maria",
        wam_id="wamid.open.2",
        body="quero remarcar",
    )
    assert reply2 is not None
    assert calls == [(tenant.id, patient.id)]  # NOT called again on a follow-up turn


def test_manage_trigger_uses_shared_query() -> None:
    """One source of truth: the manage-flow trigger uses the exact same query
    function as the opening resolver and the agent tool — and the old
    private, tenant-scoped-only helper is gone from tasks.py."""
    assert tasks.load_upcoming_appointments is patient_context.load_upcoming_appointments
    assert not hasattr(tasks, "_load_upcoming_appointments")


# --------------------------------------------------------------------------
# ACK path: resolution is worker-side only, never on the webhook request path
# --------------------------------------------------------------------------


class _FakeArqPool:
    """Records every enqueue_job call; installed on app.state.arq_pool."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))


def _sign(body: bytes) -> str:
    digest = hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def test_ack_path_untouched(client, db, monkeypatch: pytest.MonkeyPatch) -> None:
    from secretaria.main import app as fastapi_app

    # The webhook API's own idempotency check needs a DB too — wire it to the
    # same in-memory sqlite (mirrors test_audio_transcription.py).
    monkeypatch.setattr(webhook_api, "async_session_factory", db)

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    resolve_calls: list = []

    async def _fake_resolve(*args, **kwargs):
        resolve_calls.append((args, kwargs))
        return None

    monkeypatch.setattr(patient_context, "resolve_patient_opening_state", _fake_resolve)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "contacts": [{"wa_id": "5511999998888", "profile": {"name": "Ana"}}],
                            "messages": [
                                {
                                    "id": "wamid.ACK1",
                                    "from": "5511999998888",
                                    "type": "text",
                                    "text": {"body": "oi"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()

    response = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)},
    )

    assert response.status_code == 200
    assert [c[0] for c in fake_pool.calls] == ["process_webhook_event"]
    # The opening-state resolver belongs to the arq worker's own message
    # processing (workers/tasks.py), never to the synchronous ACK request.
    assert resolve_calls == []
