"""POST /tenants/me/simulation/turn: faithful, validated and side-effect free (TASK-027)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from unittest.mock import AsyncMock  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastapi import Depends  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Message, Professional, Tenant  # noqa: E402
from secretaria.workers.shared import flow_simulation  # noqa: E402

URL = "/tenants/me/simulation/turn"
DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
EIGHT_TO_TWELVE = {d: [{"start": "08:00", "end": "12:00"}] for d in DAYS}
NINE_TO_TEN = {d: [{"start": "09:00", "end": "10:00"}] for d in DAYS}
SERVICE = {"name": "Primeira Consulta", "duration_min": 30, "is_active": True, "sort_order": 0}


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


@pytest_asyncio.fixture
async def tenant(db) -> Tenant:
    async with db() as session:
        t = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=None,
            business_hours=EIGHT_TO_TWELVE,
            appointment_types=[SERVICE],
        )
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest.fixture(autouse=True)
def _override(db, tenant):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


@pytest.fixture(autouse=True)
def _arq_pool():
    from secretaria.main import app

    pool = AsyncMock()
    app.state.arq_pool = pool
    yield pool
    app.state.arq_pool = None


async def _walk(client: AsyncClient, draft=None, limit: int = 12) -> list[dict]:
    body: dict = {"draft": draft} if draft else {}
    turns = [(await client.post(URL, json=body)).json()]
    while not turns[-1]["finished"] and len(turns) < limit:
        option = turns[-1]["bubbles"][-1]["options"][0]
        step = {"state": turns[-1]["state"], "tap": {"id": option["id"], "title": option["title"]}}
        if draft:
            step["draft"] = draft
        turns.append((await client.post(URL, json=step)).json())
    return turns


def _slots(turns) -> list[str]:
    return [
        o["id"]
        for t in turns
        for b in t["bubbles"]
        if b["kind"] == "list"
        for o in b["options"]
        if o["id"].startswith("slot|")
    ]


async def test_the_flow_can_be_walked_over_http_to_a_simulated_booking(client: AsyncClient):
    turns = await _walk(client)
    assert turns[-1]["finished"] and "nada foi agendado" in turns[-1]["note"]
    assert {b["kind"] for t in turns for b in t["bubbles"]} >= {"buttons", "list"}


async def test_the_unsaved_draft_is_what_the_manager_sees(client: AsyncClient):
    slots = _slots(await _walk(client, draft={"tenant": {"business_hours": NINE_TO_TEN}}))
    assert slots and all("T09:" in s for s in slots)


async def test_no_draft_means_the_saved_clinic(client: AsyncClient):
    assert any("T09:" not in s for s in _slots(await _walk(client)))


async def test_a_simulation_writes_and_enqueues_nothing(client, db, tenant, _arq_pool):
    async with db() as s:
        before = (await s.get(Tenant, tenant.id)).business_hours
        messages = await s.scalar(select(func.count()).select_from(Message))
    await _walk(client, draft={"tenant": {"business_hours": NINE_TO_TEN}})
    async with db() as s:
        assert (await s.get(Tenant, tenant.id)).business_hours == before == EIGHT_TO_TWELVE
        assert await s.scalar(select(func.count()).select_from(Message)) == messages == 0
    _arq_pool.enqueue_job.assert_not_awaited()


async def test_the_only_calendar_write_is_the_in_memory_fake(client, monkeypatch):
    writes: list[str] = []
    original = flow_simulation.SimCalendar.create_event

    async def spy(self, *args, **kwargs):
        writes.append("create_event")
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(flow_simulation.SimCalendar, "create_event", spy)
    await _walk(client)
    assert writes == ["create_event"]


async def test_an_invalid_draft_is_refused_like_the_save_would_refuse_it(client: AsyncClient):
    bad = {"tenant": {"business_hours": {"monday": [{"start": "25:99", "end": "x"}]}}}
    assert (await client.post(URL, json={"draft": bad})).status_code == 422


async def test_unknown_draft_fields_are_refused(client: AsyncClient):
    assert (await client.post(URL, json={"draft": {"tenant": {}, "hack": 1}})).status_code == 422


@pytest.mark.parametrize(
    "state",
    [
        {"flow_state": "bogus"},
        {"flow_selected_professional_id": "not-a-uuid"},
        {"flow_step": "x" * 500},
        {"unknown_field": 1},
    ],
)
async def test_a_tampered_state_is_refused(client: AsyncClient, state):
    assert (await client.post(URL, json={"state": state})).status_code == 422


async def test_a_professional_of_another_clinic_is_a_404(client, db):
    async with db() as s:
        other = Tenant(id=uuid4(), clinic_name="Outra", phone_number_id=None)
        s.add(other)
        stranger = Professional(id=uuid4(), tenant_id=other.id, name="Dr. X", is_active=True)
        s.add(stranger)
        await s.commit()
    draft = {"professional_id": str(stranger.id), "professional": {"business_hours": NINE_TO_TEN}}
    assert (await client.post(URL, json={"draft": draft})).status_code == 404


async def test_free_text_that_would_reach_the_llm_is_a_note_not_a_model_call(client):
    first = (await client.post(URL, json={"text": "olá", "state": {}})).json()
    again = {"text": "qual o endereço?", "state": first["state"]}
    second = (await client.post(URL, json=again)).json()
    assert second["note"] and "IA livre" in second["note"]
