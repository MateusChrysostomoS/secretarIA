"""The agent's tools respect the units a professional attends at (TASK-083 spec §8).

`create_event_at_unit` refuses a unit the clinic's doctor does not attend at; the
professional-aware tools book / read at ONE of the doctor's units (named, or implied when there is
one; two or more and none named is a recoverable error); the base tools of a one-doctor clinic do
the same and send a multi-unit booking to the guided flow. A doctor without links behaves exactly
as before (tests/test_multi_unit_plugin.py, tests/test_multi_professional_plugin.py).
"""

import os

os.environ.setdefault("APP_ENV", "test")

from contextlib import contextmanager  # noqa: E402
from datetime import UTC, date  # noqa: E402
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

from secretaria.ai import (
    availability_tool as at,  # noqa: E402
    tools as ai_tools,  # noqa: E402
)
from secretaria.core import database as core_database  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Appointment, Professional, Tenant, Unit  # noqa: E402
from secretaria.models.professional_unit import ProfessionalUnit  # noqa: E402
from secretaria.plugins import (
    multi_professional as mp,  # noqa: E402
    multi_unit as mu,  # noqa: E402
)
from secretaria.services import (
    agenda as agenda_service,  # noqa: E402
    local_calendar,  # noqa: E402
)
from secretaria.services.booking_scope import BOOKING_TOPOLOGY_SOLE  # noqa: E402
from secretaria.services.local_calendar import LocalCalendarService  # noqa: E402
from secretaria.services.tenant_config import TenantRuntimeConfig  # noqa: E402

# 2026-07-10 is a Friday; 2026-07-13 a Monday.
CENTRO_HOURS = {"friday": [{"start": "08:00", "end": "12:00"}]}
SUL_HOURS = {"monday": [{"start": "14:00", "end": "18:00"}]}


class _Agenda:
    """A fake agenda with the `with_unit` seam: each scoped copy records its unit."""

    def __init__(self, log=None, unit=None):
        self.tzinfo = UTC
        self.log = log if log is not None else {"scoped": [], "created": [], "listed": []}
        self.unit = unit

    def with_unit(self, unit_id, location, *, business_hours=None):
        self.log["scoped"].append((unit_id, location, business_hours))
        return _Agenda(self.log, (unit_id, location, business_hours))

    async def list_free_slots(self, day, max_slots=6, **_kw):
        self.log["listed"].append(self.unit)
        return [{"start": "2026-07-10T09:00", "end": "2026-07-10T09:30", "label": "09:00"}]

    async def create_event(self, start, end, summary, description=""):
        self.log["created"].append(self.unit)
        return {"id": f"evt-{len(self.log['created'])}", "status": "confirmed", "htmlLink": None}


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):
    monkeypatch.setattr(core_database, "async_session_factory", db)


async def _seed(db, *, links: dict):
    """A clinic, two units, and doctors linked as `links` says ({name: [(unit, hours)]})."""
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
        session.add(tenant)
        await session.flush()
        centro = Unit(id=uuid4(), tenant_id=tenant.id, name="Centro", address="Rua A, 10")
        sul = Unit(id=uuid4(), tenant_id=tenant.id, name="Zona Sul", address="Av. B, 20")
        session.add_all([centro, sul])
        await session.flush()
        units = {"Centro": (centro, CENTRO_HOURS), "Zona Sul": (sul, SUL_HOURS)}
        doctors = {}
        for name, linked in links.items():
            doctor = Professional(id=uuid4(), tenant_id=tenant.id, name=name, is_active=True)
            session.add(doctor)
            await session.flush()
            for position, unit_name in enumerate(linked):
                unit, hours = units[unit_name]
                session.add(
                    ProfessionalUnit(
                        professional_id=doctor.id, unit_id=unit.id, business_hours=hours,
                        sort_order=position,
                    )
                )
            doctors[name] = doctor
        await session.commit()
        return tenant, centro, sul, doctors


def _config(tenant_id, professional_id=None) -> TenantRuntimeConfig:
    return TenantRuntimeConfig(
        tenant_id=tenant_id, clinic_name="Clinic", language="pt-BR", timezone="UTC",
        appointment_duration_min=30, appointment_types=[], business_hours={},
        google_calendar_id="primary", google_refresh_token=None,
        professional_id=professional_id,
    )


@contextmanager
def _context(tenant_id, *, calendar=None, sole=None):
    tokens = [
        (ai_tools._tenant_id_ctx, ai_tools._tenant_id_ctx.set(tenant_id)),
        (ai_tools._calendar_ctx, ai_tools._calendar_ctx.set(calendar)),
        (ai_tools._tenant_config_ctx, ai_tools._tenant_config_ctx.set(_config(tenant_id, sole))),
    ]
    if sole is not None:
        tokens.append(
            (
                ai_tools._booking_topology_ctx,
                ai_tools._booking_topology_ctx.set(BOOKING_TOPOLOGY_SOLE),
            )
        )
    try:
        yield
    finally:
        for var, token in reversed(tokens):
            var.reset(token)


async def _appointments(db):
    async with db() as session:
        return list(await session.scalars(select(Appointment)))


WINDOW = {"start": "2026-07-10T09:00:00", "end": "2026-07-10T09:30:00", "summary": "Consulta"}


async def test_create_event_at_unit_refuses_a_unit_the_doctor_does_not_attend_at(db):
    tenant, centro, sul, doctors = await _seed(db, links={"Dra. Ana": ["Centro"]})
    agenda = _Agenda()
    with _context(tenant.id, calendar=agenda, sole=doctors["Dra. Ana"].id):
        refused = await mu.create_event_at_unit.ainvoke({"unit_name": "Zona Sul", **WINDOW})
        booked = await mu.create_event_at_unit.ainvoke({"unit_name": "centro", **WINDOW})
    assert "não atende na unidade 'Zona Sul'" in refused["error"]
    assert "Centro" in refused["error"]
    assert "id" in booked
    [appointment] = await _appointments(db)
    assert appointment.unit_id == centro.id
    # Written through the unit's agenda: its location on the event.
    assert agenda.log["created"] == [(centro.id, "Centro — Rua A, 10", CENTRO_HOURS)]


async def test_create_event_at_unit_refuses_a_time_outside_the_units_hours(db):
    tenant, _centro, _sul, doctors = await _seed(db, links={"Dra. Ana": ["Centro"]})
    with _context(tenant.id, calendar=_Agenda(), sole=doctors["Dra. Ana"].id):
        refused = await mu.create_event_at_unit.ainvoke(
            {"unit_name": "Centro", "start": "2026-07-10T15:00:00",
             "end": "2026-07-10T15:30:00", "summary": "Consulta"}
        )
    assert refused["error"].startswith("Esse horário está fora do atendimento")
    assert await _appointments(db) == []


async def test_list_units_offers_only_the_doctors_units(db):
    tenant, _centro, _sul, doctors = await _seed(db, links={"Dra. Ana": ["Zona Sul"]})
    with _context(tenant.id, sole=doctors["Dra. Ana"].id):
        result = await mu.list_units.ainvoke({})
    assert [u["name"] for u in result["units"]] == ["Zona Sul"]


async def test_the_professional_tools_need_the_unit_when_the_doctor_has_several(db, monkeypatch):
    tenant, centro, sul, doctors = await _seed(
        db, links={"Dra. Ana": ["Centro", "Zona Sul"], "Dr. Bruno": []}
    )
    agenda = _Agenda()

    async def _calendar(tenant_id, professional):
        return agenda

    monkeypatch.setattr(mp, "_professional_calendar", _calendar)
    with _context(tenant.id):
        asked = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dra. Ana", **WINDOW}
        )
        listed = await mp.list_free_slots_for_professional.ainvoke(
            {"professional_name": "Dra. Ana", "day": "2026-07-13", "unit_name": "Zona Sul"}
        )
        booked = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dra. Ana", "unit_name": "Centro", **WINDOW}
        )
        # A doctor without links: any active unit of the clinic may still be named (as before).
        free = await mp.create_event_for_professional.ainvoke(
            {"professional_name": "Dr. Bruno", "unit_name": "Zona Sul", **WINDOW}
        )
    assert "mais de uma unidade" in asked["error"]
    assert "Centro" in asked["error"] and "Zona Sul" in asked["error"]
    assert listed["slots"] and agenda.log["listed"] == [(sul.id, "Zona Sul — Av. B, 20", SUL_HOURS)]
    assert "id" in booked and "id" in free
    by_doctor = {a.professional_id: a.unit_id for a in await _appointments(db)}
    assert by_doctor == {doctors["Dra. Ana"].id: centro.id, doctors["Dr. Bruno"].id: sul.id}


async def test_the_base_tools_send_a_multi_unit_booking_to_the_guided_flow(db):
    tenant, _centro, _sul, doctors = await _seed(db, links={"Dra. Ana": ["Centro", "Zona Sul"]})
    with _context(tenant.id, calendar=_Agenda(), sole=doctors["Dra. Ana"].id):
        refused = await ai_tools.create_event.ainvoke(WINDOW)
        listed = await ai_tools.list_free_slots.ainvoke({"day": "2026-07-10"})
    assert "start_guided_booking" in refused["error"]
    assert "start_guided_booking" in listed["error"]
    assert await _appointments(db) == []


async def test_the_base_tools_book_at_the_doctors_only_unit(db):
    tenant, centro, _sul, doctors = await _seed(db, links={"Dra. Ana": ["Centro"]})
    agenda = _Agenda()
    with _context(tenant.id, calendar=agenda, sole=doctors["Dra. Ana"].id):
        booked = await ai_tools.create_event.ainvoke(WINDOW)
    assert "id" in booked
    [appointment] = await _appointments(db)
    assert appointment.unit_id == centro.id
    assert agenda.log["created"] == [(centro.id, "Centro — Rua A, 10", CENTRO_HOURS)]


async def test_get_availability_reads_each_unit_within_its_own_hours(db, monkeypatch):
    """The real secretarIA agenda (08-18 every day) under a doctor at two units: every free
    window is inside ONE unit's hours and says which unit it is at."""
    monkeypatch.setattr(local_calendar, "async_session_factory", db)
    monkeypatch.setattr(agenda_service, "async_session_factory", db)
    week = {
        d: [{"start": "08:00", "end": "18:00"}]
        for d in ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
    }
    tenant, _centro, _sul, doctors = await _seed(db, links={"Dra. Ana": ["Centro", "Zona Sul"]})
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.business_hours = week
        await session.commit()
    config = _config(tenant.id, doctors["Dra. Ana"].id)
    config = TenantRuntimeConfig(**{**config.__dict__, "business_hours": week})
    agenda = LocalCalendarService.for_config(config, professional_id=doctors["Dra. Ana"].id)
    with _context(tenant.id, calendar=agenda, sole=doctors["Dra. Ana"].id):
        token = ai_tools._tenant_config_ctx.set(config)
        try:
            result = await at.get_availability.ainvoke({})
        finally:
            ai_tools._tenant_config_ctx.reset(token)
    assert result["windows"], result
    for window in result["windows"]:
        weekday = date.fromisoformat(window["day"]).weekday()
        if window["unit"] == "Centro":
            assert weekday == 4 and "08:00" <= window["start"] and window["end"] <= "12:00"
        else:
            assert window["unit"] == "Zona Sul"
            assert weekday == 0 and "14:00" <= window["start"] and window["end"] <= "18:00"
