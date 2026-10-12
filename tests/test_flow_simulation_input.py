"""The simulation reads saved data, overlays an unsaved draft in memory and writes NOTHING."""

from __future__ import annotations

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from secretaria.core import database as core_database
from secretaria.core.database import Base
from secretaria.models import Professional, Tenant
from secretaria.workers.shared.flow_simulation import SimulationTurn, simulate_turn
from secretaria.workers.shared.flow_simulation_input import load_simulation_inputs

DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
EIGHT_TO_TWELVE = {d: [{"start": "08:00", "end": "12:00"}] for d in DAYS}
NINE_TO_TEN = {d: [{"start": "09:00", "end": "10:00"}] for d in DAYS}
SERVICE = {
    "name": "Primeira Consulta",
    "duration_min": 30,
    "is_active": True,
    "sort_order": 0,
    "price": "R$ 250",
}


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
def _no_global_session(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the engine must not open a session of its own")

    monkeypatch.setattr(core_database, "async_session_factory", forbidden)


async def _clinic(db, *, with_professional: bool = False):
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clínica",
            phone_number_id=None,
            business_hours=EIGHT_TO_TWELVE,
            appointment_types=[SERVICE],
            appointment_duration_min=30,
        )
        session.add(tenant)
        professional = None
        if with_professional:
            professional = Professional(
                id=uuid4(),
                tenant_id=tenant.id,
                name="Dra. Ana",
                specialty="Cardiologia",
                is_active=True,
            )
            session.add(professional)
        await session.commit()
        return tenant, professional


async def _walk(inputs, limit: int = 12) -> list[SimulationTurn]:
    async def turn(state=None, tap=None) -> SimulationTurn:
        return await simulate_turn(
            tenant=inputs.tenant,
            professionals=inputs.professionals,
            calendar_for=inputs.calendar_for,
            state=state,
            tap=tap,
        )

    turns = [await turn()]
    while not turns[-1].finished and len(turns) < limit:
        option = turns[-1].bubbles[-1]["options"][0]
        turns.append(await turn(turns[-1].state, {"id": option["id"], "title": option["title"]}))
    return turns


def _slot_ids(turns) -> list[str]:
    return [
        o["id"]
        for t in turns
        for b in t.bubbles
        if b["kind"] == "list"
        for o in b["options"]
        if o["id"].startswith("slot|")
    ]


async def test_saved_hours_drive_the_simulation_when_there_is_no_draft(db):
    tenant, _ = await _clinic(db)
    async with db() as session:
        saved = await session.get(Tenant, tenant.id)
        turns = await _walk(await load_simulation_inputs(session, saved))
    slots = _slot_ids(turns)
    assert turns[-1].finished and slots
    assert any("T09:" not in s for s in slots)  # the saved 08-12 window, not a draft


async def test_the_unsaved_draft_changes_what_is_offered_but_not_what_is_saved(db):
    tenant, _ = await _clinic(db)
    async with db() as session:
        saved = await session.get(Tenant, tenant.id)
        inputs = await load_simulation_inputs(
            session, saved, draft_tenant={"business_hours": NINE_TO_TEN}
        )
        slots = _slot_ids(await _walk(inputs))
        assert slots and all("T09:" in s for s in slots)
        # the ORM object handed in was not touched...
        assert saved.business_hours == EIGHT_TO_TWELVE
        assert not session.dirty
    # ...and neither was the database
    async with db() as session:
        again = await session.get(Tenant, tenant.id)
        assert again.business_hours == EIGHT_TO_TWELVE


async def test_nothing_is_ever_committed_or_added(db, monkeypatch):
    tenant, _ = await _clinic(db)
    async with db() as session:
        saved = await session.get(Tenant, tenant.id)

        async def no_commit():
            raise AssertionError("a simulation must never commit")

        monkeypatch.setattr(session, "commit", no_commit)
        inputs = await load_simulation_inputs(
            session, saved, draft_tenant={"business_hours": NINE_TO_TEN}
        )
        await _walk(inputs)
        assert not session.new and not session.dirty and not session.deleted
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 1


async def test_a_sole_doctors_draft_hours_are_used(db):
    tenant, doctor = await _clinic(db, with_professional=True)
    async with db() as session:
        saved = await session.get(Tenant, tenant.id)
        inputs = await load_simulation_inputs(
            session,
            saved,
            professional_id=doctor.id,
            draft_professional={"business_hours": NINE_TO_TEN},
        )
        slots = _slot_ids(await _walk(inputs))
        assert slots and all("T09:" in s for s in slots)
        saved_doctor = await session.get(Professional, doctor.id)
        assert saved_doctor.business_hours in (None, {})  # the stored doctor is unchanged


async def test_a_professional_id_of_nobody_in_this_clinic_applies_no_draft(db):
    tenant, _ = await _clinic(db, with_professional=True)
    async with db() as session:
        saved = await session.get(Tenant, tenant.id)
        inputs = await load_simulation_inputs(
            session,
            saved,
            professional_id=uuid4(),
            draft_professional={"business_hours": NINE_TO_TEN},
        )
        slots = _slot_ids(await _walk(inputs))
    # An applied draft would offer only 09:xx; the saved 08-12 window offers other hours too.
    # (Not 08:xx: when the first day offered is today, the early hours have passed.)
    assert any("T09:" not in s for s in slots)


async def test_the_draft_service_catalog_is_what_the_patient_is_offered(db):
    tenant, _ = await _clinic(db)
    renamed = [{**SERVICE, "name": "Consulta Especial"}]
    async with db() as session:
        saved = await session.get(Tenant, tenant.id)
        inputs = await load_simulation_inputs(
            session, saved, draft_tenant={"appointment_types": renamed}
        )
        turns = await _walk(inputs)
    text = repr([b for t in turns for b in t.bubbles])
    assert "Consulta Especial" in text and "Primeira Consulta" not in text
