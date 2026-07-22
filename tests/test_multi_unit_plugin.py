"""Tests for plugins/multi_unit.py — unit-aware booking tools.

Same in-memory-sqlite + faked-CalendarService pattern as
test_multi_professional_plugin.py. `create_event_at_unit` uses the base
`_get_calendar()` ContextVar path (not a per-professional calendar), so the
CalendarService instance is injected directly via `_calendar_ctx` here,
mirroring exactly what `ai/graph.py:run_agent` does before invoking the agent.
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
from secretaria.models import Appointment, Tenant, Unit  # noqa: E402
from secretaria.plugins import (
    multi_unit as mu,  # noqa: E402
    registry as reg,  # noqa: E402
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402

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
        secretaria_tier="bronze_1",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


class _FakeCalendarService:
    def __init__(self) -> None:
        self.tzinfo = UTC
        self.created_events: list[dict] = []

    async def create_event(self, start, end, summary: str, description: str = "") -> dict:
        event = {
            "id": f"evt-{len(self.created_events)}",
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
    monkeypatch.setattr(core_database, "async_session_factory", db)
    yield


@contextmanager
def _agent_context(tenant_id, calendar=None):
    tok_tid = ai_tools._tenant_id_ctx.set(tenant_id)
    tok_cal = ai_tools._calendar_ctx.set(calendar)
    try:
        yield
    finally:
        ai_tools._tenant_id_ctx.reset(tok_tid)
        ai_tools._calendar_ctx.reset(tok_cal)


async def _seed_tenant_and_units(db):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()

        centro = Unit(
            tenant_id=tenant.id, name="Unidade Centro", address="Rua A, 1", is_active=True
        )
        zona_sul = Unit(
            tenant_id=tenant.id, name="Unidade Zona Sul", address="Rua B, 2", is_active=True
        )
        inactive = Unit(tenant_id=tenant.id, name="Unidade Fechada", address=None, is_active=False)
        session.add_all([centro, zona_sul, inactive])
        await session.commit()
        for u in (centro, zona_sul, inactive):
            await session.refresh(u)
        return tenant, centro, zona_sul, inactive


# --------------------------------------------------------------------------
# list_units
# --------------------------------------------------------------------------


async def test_list_units_returns_only_active_with_address(db):
    tenant, centro, zona_sul, _inactive = await _seed_tenant_and_units(db)
    with _agent_context(tenant.id):
        result = await mu.list_units.ainvoke({})
    names = {u["name"] for u in result["units"]}
    assert names == {centro.name, zona_sul.name}
    by_name = {u["name"]: u["address"] for u in result["units"]}
    assert by_name[centro.name] == centro.address


async def test_list_units_no_tenant_context_returns_empty():
    result = await mu.list_units.ainvoke({})
    assert result == {"units": []}


# --------------------------------------------------------------------------
# create_event_at_unit: resolution + persistence
# --------------------------------------------------------------------------


async def test_create_event_at_unit_persists_unit_id(db):
    tenant, centro, _zona_sul, _inactive = await _seed_tenant_and_units(db)
    with _agent_context(tenant.id, calendar=_FakeCalendarService()):
        result = await mu.create_event_at_unit.ainvoke(
            {
                "unit_name": "unidade centro",  # case-insensitive
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
            }
        )
    assert "id" in result
    async with db() as session:
        appt = (await session.scalars(select(Appointment))).one()
        assert appt.unit_id == centro.id
        assert appt.professional_id is None
        assert appt.tenant_id == tenant.id


async def test_create_event_at_unit_unknown_unit_returns_error_and_persists_nothing(db):
    tenant, centro, zona_sul, _inactive = await _seed_tenant_and_units(db)
    with _agent_context(tenant.id, calendar=_FakeCalendarService()):
        result = await mu.create_event_at_unit.ainvoke(
            {
                "unit_name": "Unidade Fantasma",
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
            }
        )
    assert "error" in result
    assert "Unidade Fantasma" in result["error"]
    assert centro.name in result["error"]
    assert zona_sul.name in result["error"]
    assert "Unidade Fechada" not in result["error"]  # inactive - not a valid option
    async with db() as session:
        rows = (await session.scalars(select(Appointment))).all()
        assert rows == []


async def test_create_event_at_unit_inactive_unit_is_not_resolvable(db):
    tenant, _centro, _zona_sul, inactive = await _seed_tenant_and_units(db)
    with _agent_context(tenant.id, calendar=_FakeCalendarService()):
        result = await mu.create_event_at_unit.ainvoke(
            {
                "unit_name": inactive.name,
                "start": "2026-07-10T14:00:00",
                "end": "2026-07-10T14:30:00",
                "summary": "Consulta - Paciente",
            }
        )
    assert "error" in result


# --------------------------------------------------------------------------
# Plugin gating: tools only present in agent_tools_for when entitled
# --------------------------------------------------------------------------


def test_multi_unit_tools_present_only_when_entitled():
    entitled = _summary(addons={**_ALL_ADDONS_OFF, "multi_unit": True})
    not_entitled = _summary(addons=dict(_ALL_ADDONS_OFF))

    entitled_names = {t.name for t in reg.agent_tools_for(entitled)}
    not_entitled_names = {t.name for t in reg.agent_tools_for(not_entitled)}

    expected = {mu.list_units.name, mu.create_event_at_unit.name}
    assert expected <= entitled_names
    assert expected.isdisjoint(not_entitled_names)
