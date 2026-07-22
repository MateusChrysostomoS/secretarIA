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
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Patient,
    Professional,
    Tenant,
)
from secretaria.services import patient_context  # noqa: E402
from secretaria.services.flow_router import (  # noqa: E402
    LABEL_CANCEL_APPT,
    LABEL_OTHER,
    LABEL_RESCHEDULE,
    menu_buttons_for,
)
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
    professional_id=None,
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
            professional_id=professional_id,
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
# _adapt_greeting_to_state: HAS_UPCOMING(_SOON) full detail + brief blocks
# --------------------------------------------------------------------------


def _tz_when(start_at: datetime) -> str:
    return start_at.astimezone(ZoneInfo("America/Sao_Paulo")).strftime("%d/%m às %H:%M")


def _greeting_tenant(**kwargs) -> Tenant:
    return Tenant(id=uuid4(), clinic_name="Clinic", timezone="America/Sao_Paulo", **kwargs)


def test_upcoming_greeting_full_detail_single_appointment() -> None:
    """One future appointment: full detail (when/doctor/service/price/description/
    orientações) and NO brief block."""
    tenant = _greeting_tenant()
    start_at = NOW + timedelta(hours=24)
    prof_id = str(uuid4())
    context = PatientOpeningContext(
        state=PatientOpeningState.HAS_UPCOMING_SOON,
        future_appointments=[
            {
                "id": str(uuid4()),
                "appointment_type": "Limpeza de Pele",
                "start_at": start_at,
                "professional_id": prof_id,
            }
        ],
    )
    data = tasks._UpcomingGreetingData(
        professional_names={prof_id: "Dra. Ana Souza"},
        nearest_service={
            "name": "Limpeza de Pele",
            "price": "R$ 250,00",
            "description": "Sessão de limpeza profunda.",
            "requirements": ["Jejum de 8 horas", "Trazer exames anteriores"],
        },
    )

    result = tasks._adapt_greeting_to_state("Olá!", context, tenant, data)

    assert result.startswith("Olá!")
    assert _tz_when(start_at) in result
    assert "com Dra. Ana Souza" in result
    assert "Limpeza de Pele — R$ 250,00" in result
    assert "Sessão de limpeza profunda." in result
    assert tasks.GREETING_REQUIREMENTS_HEADER in result
    assert "• Jejum de 8 horas" in result
    assert "• Trazer exames anteriores" in result
    assert tasks.GREETING_ACTION_HINT in result
    assert tasks.GREETING_BRIEF_HEADER not in result


def test_upcoming_greeting_brief_block_date_service_doctor_only() -> None:
    """2+ future appointments: nearest gets the detail block, the others get ONE
    brief line each — date + service + doctor ONLY (price/orientações appear
    exactly once, in the detail block)."""
    tenant = _greeting_tenant()
    prof_id = str(uuid4())
    nearest_start = NOW + timedelta(hours=24)
    second_start = NOW + timedelta(hours=90)
    third_start = NOW + timedelta(hours=200)
    context = PatientOpeningContext(
        state=PatientOpeningState.HAS_UPCOMING_SOON,
        future_appointments=[
            {
                "appointment_type": "Consulta",
                "start_at": nearest_start,
                "professional_id": prof_id,
            },
            {
                "appointment_type": "Retorno",
                "start_at": second_start,
                "professional_id": prof_id,
            },
            {
                "appointment_type": "Avaliação",
                "start_at": third_start,
                "professional_id": None,
            },
        ],
    )
    data = tasks._UpcomingGreetingData(
        professional_names={prof_id: "Dr. Bruno Lima"},
        nearest_service={
            "name": "Consulta",
            "price": "R$ 300,00",
            "requirements": ["Chegar 10 minutos antes"],
        },
    )

    result = tasks._adapt_greeting_to_state("Olá!", context, tenant, data)

    assert tasks.GREETING_BRIEF_HEADER in result
    assert f"• {_tz_when(second_start)} — Retorno — Dr. Bruno Lima" in result
    assert f"• {_tz_when(third_start)} — Avaliação" in result
    assert result.count("R$ 300,00") == 1
    assert result.count(tasks.GREETING_REQUIREMENTS_HEADER) == 1
    assert result.count("Chegar 10 minutos antes") == 1


def test_upcoming_greeting_degrades_without_catalog_match() -> None:
    """No catalog match (stale/renamed service) and no doctor: the detail block
    still renders when + service name, with no price/description/orientações."""
    tenant = _greeting_tenant()
    start_at = NOW + timedelta(hours=24)
    context = PatientOpeningContext(
        state=PatientOpeningState.HAS_UPCOMING,
        future_appointments=[
            {"appointment_type": "Consulta antiga", "start_at": start_at, "professional_id": None}
        ],
    )

    result = tasks._adapt_greeting_to_state(
        "Olá!", context, tenant, tasks._UpcomingGreetingData()
    )

    assert "Consulta antiga" in result
    assert _tz_when(start_at) in result
    assert ", com " not in result
    assert "R$" not in result
    assert tasks.GREETING_REQUIREMENTS_HEADER not in result
    assert tasks.GREETING_ACTION_HINT in result


# --------------------------------------------------------------------------
# _compose_upcoming_greeting_body: assembly + size-trim order
# --------------------------------------------------------------------------


def test_compose_under_budget_untouched() -> None:
    body = tasks._compose_upcoming_greeting_body(
        "G", "I", "D", ["r1", "r2"], ["b1", "b2"], max_chars=10_000
    )
    assert body == (
        "G\n\nI\n\nD\n\n"
        f"{tasks.GREETING_REQUIREMENTS_HEADER}\n• r1\n• r2\n\n"
        f"{tasks.GREETING_BRIEF_HEADER}\n• b1\n• b2\n\n"
        f"{tasks.GREETING_ACTION_HINT}"
    )


def test_compose_trim_drops_description_first() -> None:
    """First trim stage: only the description is dropped — brief lines and
    orientações stay complete."""
    without_description = tasks._compose_upcoming_greeting_body(
        "G", "I", None, ["r1"], ["b1"], max_chars=10_000
    )

    body = tasks._compose_upcoming_greeting_body(
        "G", "I", "DESCRIÇÃO " * 200, ["r1"], ["b1"], max_chars=len(without_description)
    )

    assert body == without_description
    assert "DESCRIÇÃO" not in body


def test_compose_trim_caps_brief_then_requirements_at_worst() -> None:
    """Maximally trimmed: description gone, brief lines capped with the
    '… e mais N' tail, orientações capped with the '…' tail."""
    briefs = [f"linha {i}" for i in range(1, 7)]
    reqs = [f"regra {i}" for i in range(1, 7)]

    body = tasks._compose_upcoming_greeting_body(
        "G", "I", "DESCRIÇÃO LONGA", reqs, briefs, max_chars=0
    )

    assert "DESCRIÇÃO" not in body
    assert f"• linha {tasks.GREETING_BRIEF_KEEP}" in body
    assert f"• linha {tasks.GREETING_BRIEF_KEEP + 1}" not in body
    assert f"… e mais {len(briefs) - tasks.GREETING_BRIEF_KEEP} consultas" in body
    assert f"• regra {tasks.GREETING_REQUIREMENTS_KEEP}\n…" in body
    assert f"• regra {tasks.GREETING_REQUIREMENTS_KEEP + 1}" not in body

    # Singular tail when exactly one brief line is dropped.
    singular = tasks._compose_upcoming_greeting_body(
        "G", "I", None, [], briefs[: tasks.GREETING_BRIEF_KEEP + 1], max_chars=0
    )
    assert "… e mais 1 consulta" in singular


# --------------------------------------------------------------------------
# _greeting_buttons_for: the three button sets
# --------------------------------------------------------------------------


def test_greeting_buttons_upcoming_trio_wins() -> None:
    """HAS_UPCOMING(_SOON) + flows enabled: [Remarcar] [Cancelar] [Outro] —
    on single- AND multi-doctor tenants alike."""
    tenant = _greeting_tenant(initial_flows={"enabled": True})
    trio = [LABEL_RESCHEDULE, LABEL_CANCEL_APPT, LABEL_OTHER]
    for state in (PatientOpeningState.HAS_UPCOMING_SOON, PatientOpeningState.HAS_UPCOMING):
        context = PatientOpeningContext(
            state=state, future_appointments=[{"start_at": NOW + timedelta(hours=24)}]
        )
        assert tasks._greeting_buttons_for(tenant, "Olá!", False, context) == trio
        assert tasks._greeting_buttons_for(tenant, "Olá!", True, context) == trio


def test_greeting_buttons_other_states_keep_menu() -> None:
    """NEW / RETURNING_NO_APPOINTMENT / JUST_HAD_CONSULT / no context: the
    effective menu buttons, exactly as before."""
    tenant = _greeting_tenant(initial_flows={"enabled": True})
    menu = menu_buttons_for(tenant, False)
    for context in (
        PatientOpeningContext(state=PatientOpeningState.NEW),
        PatientOpeningContext(state=PatientOpeningState.RETURNING_NO_APPOINTMENT, has_history=True),
        PatientOpeningContext(
            state=PatientOpeningState.JUST_HAD_CONSULT,
            recent_past_appointments=[{"status": AppointmentStatus.ATTENDED}],
        ),
        None,
    ):
        assert tasks._greeting_buttons_for(tenant, "Olá!", False, context) == menu


def test_greeting_buttons_flows_disabled_unchanged() -> None:
    """Flow-less (pure LLM) tenants keep their configured quick replies even on
    HAS_UPCOMING — the deterministic trio only exists where route() handles it."""
    tenant = _greeting_tenant(initial_flows={}, greeting_buttons=["Agendar", "Falar com humano"])
    context = PatientOpeningContext(
        state=PatientOpeningState.HAS_UPCOMING_SOON,
        future_appointments=[{"start_at": NOW + timedelta(hours=24)}],
    )
    assert tasks._greeting_buttons_for(tenant, "Olá!", False, context) == [
        "Agendar",
        "Falar com humano",
    ]
    assert tasks._greeting_buttons_for(tenant, None, False, context) == []


# --------------------------------------------------------------------------
# _load_upcoming_greeting_data: names (incl. inactive) + catalog resolution
# --------------------------------------------------------------------------


async def test_load_upcoming_greeting_data_professional_catalog_and_inactive_name(db) -> None:
    """The owning professional's OWN catalog wins (matched casefold), and a
    deactivated professional's name still resolves for display."""
    tenant, patient = await _seed_tenant_and_patient(db)
    prof_id = uuid4()
    async with db() as session:
        session.add(
            Professional(
                id=prof_id,
                tenant_id=tenant.id,
                name="Dra. Carla Nunes",
                is_active=False,
                appointment_types=[
                    {
                        "name": "Consulta",
                        "duration_min": 30,
                        "is_active": True,
                        "sort_order": 0,
                        "price": "R$ 400,00",
                        "requirements": ["Jejum de 8 horas"],
                    }
                ],
            )
        )
        await session.commit()
    await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.SCHEDULED,
        start_at=NOW + timedelta(hours=24),
        appointment_type="consulta",
        professional_id=prof_id,
    )

    context = await _resolve(db, tenant.id, patient.id, now=NOW)
    assert context.future_appointments[0]["professional_id"] == str(prof_id)

    async with db() as session:
        tenant_row = await session.get(Tenant, tenant.id)
        data = await tasks._load_upcoming_greeting_data(
            session, tenant_row, context.future_appointments
        )

    assert data.professional_names == {str(prof_id): "Dra. Carla Nunes"}
    assert data.nearest_service is not None
    assert data.nearest_service["price"] == "R$ 400,00"
    assert data.nearest_service["requirements"] == ["Jejum de 8 horas"]


async def test_load_upcoming_greeting_data_tenant_catalog_and_no_match(db) -> None:
    """No owning professional: the tenant's own catalog resolves the nearest
    service; an unknown service name yields None (graceful degrade)."""
    tenant, patient = await _seed_tenant_and_patient(db)
    async with db() as session:
        tenant_row = await session.get(Tenant, tenant.id)
        tenant_row.appointment_types = [
            {
                "name": "Avaliação",
                "duration_min": 30,
                "is_active": True,
                "sort_order": 0,
                "price": "R$ 150,00",
            }
        ]
        await session.commit()
    await _add_appointment(
        db,
        tenant.id,
        patient.id,
        status=AppointmentStatus.SCHEDULED,
        start_at=NOW + timedelta(hours=24),
        appointment_type="Avaliação",
    )

    context = await _resolve(db, tenant.id, patient.id, now=NOW)

    async with db() as session:
        tenant_row = await session.get(Tenant, tenant.id)
        data = await tasks._load_upcoming_greeting_data(
            session, tenant_row, context.future_appointments
        )
        assert data.professional_names == {}
        assert data.nearest_service is not None
        assert data.nearest_service["price"] == "R$ 150,00"

        unmatched = await tasks._load_upcoming_greeting_data(
            session,
            tenant_row,
            [
                {
                    "appointment_type": "Serviço removido",
                    "start_at": NOW + timedelta(hours=24),
                    "professional_id": None,
                }
            ],
        )
        assert unmatched.nearest_service is None


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
