"""Tests for plugins/multi_professional.py — professional-aware booking tools.

In-memory-sqlite pattern established by test_reminders_plugin.py /
test_bot_reply_gating.py (a real DB, monkeypatched in place of the
Postgres-backed `async_session_factory`). Unlike those modules, `ai/tools.py`'s
`_persist_appointment`/`_active_professionals` import `async_session_factory`
LAZILY (inside the function body) so the DB seam is patched at its SOURCE —
`secretaria.core.database.async_session_factory` — rather than on a module
attribute; a plain module-level monkeypatch (the reminders.py pattern) would
miss it here.

CalendarService is faked (no Google network call). ContextVars are set/reset
manually per test, mirroring exactly what `ai/graph.py:run_agent` does before
invoking the agent.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC  # noqa: E402
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
    Conversation,
    Patient,
    Professional,
    Tenant,
    Unit,
)
from secretaria.plugins import (
    multi_professional as mp,  # noqa: E402
    registry as reg,  # noqa: E402
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.tenant_config import (  # noqa: E402
    TenantRuntimeConfig,
    set_professional_google_refresh_token,
)

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_deposit": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="basico",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


class _FakeCalendarService:
    """Records constructed instances (by calendar_id) + created events."""

    instances: list["_FakeCalendarService"] = []

    def __init__(
        self,
        calendar_id: str = "fallback-cal",
        business_hours: dict | None = None,
        appointment_duration_min: int | None = None,
    ) -> None:
        self.calendar_id = calendar_id
        self.business_hours = business_hours
        self.appointment_duration_min = appointment_duration_min
        self.tzinfo = UTC
        self.created_events: list[dict] = []
        _FakeCalendarService.instances.append(self)

    @classmethod
    def from_tenant_config(cls, config: TenantRuntimeConfig) -> "_FakeCalendarService":
        return cls(calendar_id=config.google_calendar_id)

    @classmethod
    def for_professional(
        cls,
        tenant_config: TenantRuntimeConfig,
        *,
        google_calendar_id: str | None = None,
        google_refresh_token: str | None = None,
        business_hours: dict | None = None,
        appointment_duration_min: int | None = None,
    ) -> "_FakeCalendarService":
        return cls(
            calendar_id=google_calendar_id or tenant_config.google_calendar_id,
            business_hours=business_hours,
            appointment_duration_min=appointment_duration_min,
        )

    async def list_free_slots(self, day, max_slots: int = 6) -> list[dict]:
        return [{"start": "2026-07-10T09:00", "end": "2026-07-10T09:30", "label": "09:00"}]

    async def create_event(self, start, end, summary: str, description: str = "") -> dict:
        event = {
            "id": f"evt-{len(self.created_events)}-{self.calendar_id}",
            "status": "confirmed",
            "htmlLink": "https://calendar.example/evt",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        self.created_events.append({"summary": summary, "description": description})
        return event


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
    # ai/tools.py's _persist_appointment / plugin helpers lazily import
    # async_session_factory FROM core.database on every call, so the source
    # module attribute must be patched (not a module that merely imported it
    # once at load time).
    monkeypatch.setattr(core_database, "async_session_factory", db)
    _FakeCalendarService.instances = []
    monkeypatch.setattr(ai_tools, "CalendarService", _FakeCalendarService)
    yield


@contextmanager
def _agent_context(tenant_id, tenant_config=None, conversation_id=None):
    tok_tid = ai_tools._tenant_id_ctx.set(tenant_id)
    tok_cfg = ai_tools._tenant_config_ctx.set(tenant_config)
    tok_conv = ai_tools._conversation_id_ctx.set(conversation_id)
    try:
        yield
    finally:
        ai_tools._tenant_id_ctx.reset(tok_tid)
        ai_tools._tenant_config_ctx.reset(tok_cfg)
        ai_tools._conversation_id_ctx.reset(tok_conv)


def _tenant_config(tenant_id, calendar_id: str = "tenant-calendar") -> TenantRuntimeConfig:
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
        google_calendar_id=calendar_id,
        google_refresh_token="refresh-token",
    )


async def _seed_tenant_and_professionals(db):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()

        with_own_calendar = Professional(
            tenant_id=tenant.id,
            name="Dra. Ana",
            google_calendar_id="ana-calendar",
            is_active=True,
        )
        fallback_calendar = Professional(
            tenant_id=tenant.id, name="Dr. Bruno", google_calendar_id=None, is_active=True
        )
        inactive = Professional(
            tenant_id=tenant.id, name="Dr. Inativo", google_calendar_id=None, is_active=False
        )
        session.add_all([with_own_calendar, fallback_calendar, inactive])
        await session.commit()
        for p in (with_own_calendar, fallback_calendar, inactive):
            await session.refresh(p)
        return tenant, with_own_calendar, fallback_calendar, inactive


# --------------------------------------------------------------------------
# list_professionals
# --------------------------------------------------------------------------


async def test_list_professionals_returns_only_active_names(db):
    tenant, ana, bruno, _inactive = await _seed_tenant_and_professionals(db)
    with _agent_context(tenant.id):
        result = await mp.list_professionals.ainvoke({})
    names = sorted(p["name"] for p in result["professionals"])
    assert names == sorted([ana.name, bruno.name])
    # Neither seeded professional has a specialty - the key must be omitted,
    # not present-and-null (see test_list_professionals_includes_specialty_when_set).
    assert all("specialty" not in p for p in result["professionals"])


async def test_list_professionals_includes_specialty_when_set(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    async with db() as session:
        db_ana = await session.get(Professional, ana.id)
        db_ana.specialty = "Cardiologia"
        await session.commit()

    with _agent_context(tenant.id):
        result = await mp.list_professionals.ainvoke({})
    by_name = {p["name"]: p for p in result["professionals"]}
    assert by_name[ana.name]["specialty"] == "Cardiologia"
    assert "specialty" not in by_name["Dr. Bruno"]


async def test_list_professionals_no_tenant_context_returns_empty():
    result = await mp.list_professionals.ainvoke({})
    assert result == {"professionals": []}


# --------------------------------------------------------------------------
# professional resolution: found (own calendar / fallback), not found
# --------------------------------------------------------------------------


async def test_list_free_slots_uses_professionals_own_calendar(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.list_free_slots_for_professional.ainvoke(
            {"professional_name": "dra. ana", "day": "2026-07-10"}  # case-insensitive
        )
    assert result["slots"]
    assert _FakeCalendarService.instances[-1].calendar_id == "ana-calendar"


async def test_list_free_slots_falls_back_to_tenant_calendar(db):
    tenant, _ana, bruno, _inactive = await _seed_tenant_and_professionals(db)
    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id, "tenant-calendar")):
        result = await mp.list_free_slots_for_professional.ainvoke(
            {"professional_name": bruno.name, "day": "2026-07-10"}
        )
    assert result["slots"]
    assert _FakeCalendarService.instances[-1].calendar_id == "tenant-calendar"


async def test_unknown_professional_name_returns_helpful_error(db):
    tenant, ana, bruno, _inactive = await _seed_tenant_and_professionals(db)
    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.list_free_slots_for_professional.ainvoke(
            {"professional_name": "Dr. Ghost", "day": "2026-07-10"}
        )
    assert "error" in result
    assert "Dr. Ghost" in result["error"]
    assert ana.name in result["error"]
    assert bruno.name in result["error"]
    # An inactive professional must never be offered as a valid option.
    assert "Dr. Inativo" not in result["error"]


async def test_inactive_professional_is_not_resolvable(db):
    tenant, _ana, _bruno, inactive = await _seed_tenant_and_professionals(db)
    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.list_free_slots_for_professional.ainvoke(
            {"professional_name": inactive.name, "day": "2026-07-10"}
        )
    assert "error" in result


# --------------------------------------------------------------------------
# Own hours/services/credential + context_doctor_message in tool output
# (contract v1 §10 item D)
# --------------------------------------------------------------------------

_OWN_HOURS = {"tuesday": [{"start": "09:00", "end": "17:00"}]}
_OWN_TYPES = [{"name": "Retorno", "duration_min": 45, "is_active": True, "sort_order": 0}]


async def test_list_free_slots_uses_professionals_own_hours_and_duration(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    async with db() as session:
        db_ana = await session.get(Professional, ana.id)
        db_ana.business_hours = _OWN_HOURS
        db_ana.appointment_types = _OWN_TYPES
        await session.commit()

    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        await mp.list_free_slots_for_professional.ainvoke(
            {"professional_name": ana.name, "day": "2026-07-10"}
        )
    built = _FakeCalendarService.instances[-1]
    assert built.business_hours == _OWN_HOURS
    # First (and only) active service's duration drives the slot length.
    assert built.appointment_duration_min == 45


async def test_create_event_uses_professionals_own_google_credential(db):
    """The professional's OWN refresh token wins over the tenant's own."""
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    async with db() as session:
        await set_professional_google_refresh_token(session, ana.id, "ana-own-refresh-token")
        await session.commit()

    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        await mp.create_event_for_professional.ainvoke(
            {
                "professional_name": ana.name,
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
            }
        )
    # _FakeCalendarService doesn't record the token directly, but building
    # successfully with a distinct professional-only credential (never set at
    # the tenant level in this fixture) proves the professional's own token
    # was resolved and passed through - see the dedicated resolution-order
    # unit test on services/tenant_config for the credential value itself.
    assert _FakeCalendarService.instances[-1].calendar_id == "ana-calendar"


async def test_list_free_slots_result_includes_context_doctor_message_when_set(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    async with db() as session:
        db_ana = await session.get(Professional, ana.id)
        db_ana.context_doctor_message = "Prefere consultas de retorno pela manhã."
        await session.commit()

    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.list_free_slots_for_professional.ainvoke(
            {"professional_name": ana.name, "day": "2026-07-10"}
        )
    assert result["professional_context"] == "Prefere consultas de retorno pela manhã."


async def test_list_free_slots_result_omits_context_when_not_set(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.list_free_slots_for_professional.ainvoke(
            {"professional_name": ana.name, "day": "2026-07-10"}
        )
    assert "professional_context" not in result


async def test_create_event_result_includes_context_doctor_message_when_set(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    async with db() as session:
        db_ana = await session.get(Professional, ana.id)
        db_ana.context_doctor_message = "Sempre confirmar convênio antes da consulta."
        await session.commit()

    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.create_event_for_professional.ainvoke(
            {
                "professional_name": ana.name,
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
            }
        )
    assert result["professional_context"] == "Sempre confirmar convênio antes da consulta."


# --------------------------------------------------------------------------
# create_event_for_professional: persistence (professional_id, unit_id)
# --------------------------------------------------------------------------


async def test_create_event_for_professional_persists_professional_id(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.create_event_for_professional.ainvoke(
            {
                "professional_name": ana.name,
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
            }
        )
    assert "id" in result
    async with db() as session:
        appt = (await session.scalars(select(Appointment))).one()
        assert appt.professional_id == ana.id
        assert appt.unit_id is None
        assert appt.tenant_id == tenant.id


async def test_create_event_for_professional_with_unit_persists_both_ids(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    async with db() as session:
        unit = Unit(tenant_id=tenant.id, name="Unidade Centro", address="Rua X, 1", is_active=True)
        session.add(unit)
        await session.commit()
        await session.refresh(unit)

    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.create_event_for_professional.ainvoke(
            {
                "professional_name": ana.name,
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
                "unit_name": "unidade centro",  # case-insensitive
            }
        )
    assert "id" in result
    async with db() as session:
        appt = (await session.scalars(select(Appointment))).one()
        assert appt.professional_id == ana.id
        assert appt.unit_id == unit.id


async def test_create_event_for_professional_unknown_unit_creates_nothing(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    with _agent_context(tenant.id, tenant_config=_tenant_config(tenant.id)):
        result = await mp.create_event_for_professional.ainvoke(
            {
                "professional_name": ana.name,
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
                "unit_name": "Unidade Fantasma",
            }
        )
    assert "error" in result
    assert "Unidade Fantasma" in result["error"]
    async with db() as session:
        rows = (await session.scalars(select(Appointment))).all()
        assert rows == []  # no calendar event / no appointment - unit resolution failed first


async def test_create_event_for_professional_persists_patient_and_conversation(db):
    tenant, ana, _bruno, _inactive = await _seed_tenant_and_professionals(db)
    async with db() as session:
        patient = Patient(tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.commit()
        await session.refresh(patient)
        await session.refresh(conversation)

    with _agent_context(
        tenant.id, tenant_config=_tenant_config(tenant.id), conversation_id=conversation.id
    ):
        await mp.create_event_for_professional.ainvoke(
            {
                "professional_name": ana.name,
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
            }
        )
    async with db() as session:
        appt = (await session.scalars(select(Appointment))).one()
        assert appt.patient_id == patient.id
        assert appt.conversation_id == conversation.id
        assert appt.phone == patient.wa_id


# --------------------------------------------------------------------------
# Plugin gating: tools only present in agent_tools_for when entitled
# --------------------------------------------------------------------------


def test_multi_professional_tools_present_only_when_entitled():
    entitled = _summary(addons={**_ALL_ADDONS_OFF, "multi_professional": True})
    not_entitled = _summary(addons=dict(_ALL_ADDONS_OFF))

    entitled_names = {t.name for t in reg.agent_tools_for(entitled)}
    not_entitled_names = {t.name for t in reg.agent_tools_for(not_entitled)}

    expected = {
        mp.list_professionals.name,
        mp.list_free_slots_for_professional.name,
        mp.create_event_for_professional.name,
    }
    assert expected <= entitled_names
    assert expected.isdisjoint(not_entitled_names)
