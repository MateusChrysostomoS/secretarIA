"""Tests for ai/tools.py:list_patient_appointments — the read-only agent tool.

Mirrors test_precheck_handoff_tool.py's pattern: ContextVars
(_tenant_id_ctx/_conversation_id_ctx) are set/reset manually exactly like
ai/graph.py:run_agent does, and an in-memory sqlite DB backs the
conversation/patient/appointment resolution. `async_session_factory` is
imported lazily inside the tool (same seam as `_resolve_patient_phone`), so
the SOURCE module attribute (`core.database`) is what must be patched.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import json  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import graph, tools as ai_tools  # noqa: E402
from secretaria.ai.prompts import secretary_system_prompt  # noqa: E402
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    AppointmentStatus,
    Conversation,
    Patient,
    Tenant,
)
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

SAO_PAULO = ZoneInfo("America/Sao_Paulo")


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
    # The tool imports async_session_factory lazily FROM core.database, so the
    # source module attribute must be patched (test_precheck_handoff_tool.py's
    # identical note). workers.tasks is patched too for parity with the rest
    # of the suite, even though this file never exercises it directly.
    monkeypatch.setattr(core_database, "async_session_factory", db)
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


@contextmanager
def _agent_context(tenant_id=None, conversation_id=None):
    tok_tid = ai_tools._tenant_id_ctx.set(tenant_id)
    tok_conv = ai_tools._conversation_id_ctx.set(conversation_id)
    try:
        yield
    finally:
        ai_tools._tenant_id_ctx.reset(tok_tid)
        ai_tools._conversation_id_ctx.reset(tok_conv)


def _tenant_config(tenant_id) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id,
        clinic_name="Clinic",
        greeting_message=None,
        persona_notes=None,
        language="pt-BR",
        timezone="America/Sao_Paulo",
        appointment_duration_min=30,
        appointment_types=[],
        business_hours={},
        google_calendar_id="tenant-calendar",
        google_refresh_token=None,
    )


async def _seed_with_appointments(db) -> tuple[Tenant, Patient, Conversation, datetime]:
    """Tenant + patient + conversation, with a mix of appointments:

    future SCHEDULED (+24h), future CONFIRMED (+72h), future CANCELLED
    (+48h, must never be listed), past SCHEDULED (-24h, must never be listed
    either — this tool is future-only, mirroring load_upcoming_appointments).
    """
    now = datetime.now(UTC)
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        session.add_all(
            [
                Appointment(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    google_event_id=str(uuid4()),
                    appointment_type="Retorno",
                    start_at=now + timedelta(hours=24),
                    end_at=now + timedelta(hours=24, minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                ),
                Appointment(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    google_event_id=str(uuid4()),
                    appointment_type="Consulta",
                    start_at=now + timedelta(hours=72),
                    end_at=now + timedelta(hours=72, minutes=30),
                    status=AppointmentStatus.CONFIRMED,
                ),
                Appointment(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    google_event_id=str(uuid4()),
                    appointment_type="Cancelada",
                    start_at=now + timedelta(hours=48),
                    end_at=now + timedelta(hours=48, minutes=30),
                    status=AppointmentStatus.CANCELLED,
                ),
                Appointment(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    google_event_id=str(uuid4()),
                    appointment_type="Passada",
                    start_at=now - timedelta(hours=24),
                    end_at=now - timedelta(hours=23, minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                ),
            ]
        )
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(patient)
        await session.refresh(conversation)
        return tenant, patient, conversation, now


# --------------------------------------------------------------------------
# Behavior: future-only, nearest first, tz-rendered, no ids in the payload
# --------------------------------------------------------------------------


async def test_returns_futures_only(db) -> None:
    tenant, _patient, conversation, now = await _seed_with_appointments(db)

    with _agent_context(tenant_id=tenant.id, conversation_id=conversation.id):
        result = await ai_tools.list_patient_appointments.ainvoke({})

    assert result["count"] == 2
    appointments = result["appointments"]
    assert len(appointments) == 2

    # Nearest first: the +24h SCHEDULED row before the +72h CONFIRMED row. The
    # +48h CANCELLED and the -24h past row are never listed.
    assert [a["tipo"] for a in appointments] == ["Retorno", "Consulta"]
    fmt = "%d/%m/%Y às %H:%M"
    expected_first = (now + timedelta(hours=24)).astimezone(SAO_PAULO).strftime(fmt)
    expected_second = (now + timedelta(hours=72)).astimezone(SAO_PAULO).strftime(fmt)
    assert appointments[0]["quando"] == expected_first
    assert appointments[1]["quando"] == expected_second

    # Only "quando"/"tipo" per row — no id, no google_event_id, anywhere.
    for appt in appointments:
        assert set(appt.keys()) == {"quando", "tipo"}
    payload = json.dumps(result)
    assert "google_event_id" not in payload
    assert '"id"' not in payload


async def test_read_only(db) -> None:
    tenant, patient, conversation, _now = await _seed_with_appointments(db)

    async with db() as session:
        before = {
            a.id: a.status
            for a in (
                await session.scalars(
                    select(Appointment).where(Appointment.patient_id == patient.id)
                )
            ).all()
        }

    with _agent_context(tenant_id=tenant.id, conversation_id=conversation.id):
        await ai_tools.list_patient_appointments.ainvoke({})

    async with db() as session:
        after = {
            a.id: a.status
            for a in (
                await session.scalars(
                    select(Appointment).where(Appointment.patient_id == patient.id)
                )
            ).all()
        }

    assert after == before
    assert len(after) == 4


# --------------------------------------------------------------------------
# Unresolved patient: an error, never an empty/"no appointments" answer
# --------------------------------------------------------------------------


async def test_unresolved_patient_is_error_not_empty(db) -> None:
    tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
    async with db() as session:
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)

    # (a) No conversation context set at all.
    with _agent_context(tenant_id=tenant.id, conversation_id=None):
        result = await ai_tools.list_patient_appointments.ainvoke({})
    assert "error" in result
    assert "appointments" not in result

    # (b) A conversation_id that resolves to nothing (patient unresolved).
    with _agent_context(tenant_id=tenant.id, conversation_id=uuid4()):
        result = await ai_tools.list_patient_appointments.ainvoke({})
    assert "error" in result
    assert "appointments" not in result


# --------------------------------------------------------------------------
# Registration: base tools + prompt teaches the tool AND the menu routing
# --------------------------------------------------------------------------


def test_registered_in_base_tools_with_menu_routing() -> None:
    tool_names = {t.name for t in graph._BASE_TOOLS}
    assert "list_patient_appointments" in tool_names
    assert "show_main_menu" in tool_names

    prompt = secretary_system_prompt(_tenant_config(uuid4()))
    assert "list_patient_appointments" in prompt
    assert "show_main_menu" in prompt

    # The tool's own description teaches the model to route management
    # actions back through show_main_menu instead of acting on what it lists.
    assert "show_main_menu" in ai_tools.list_patient_appointments.description
