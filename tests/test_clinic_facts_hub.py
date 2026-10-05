"""Hub routes for the clinic facts and the completeness checklist (TASK-025).

Same db / tenant / dependency-override pattern as tests/test_hub_config.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastapi import Depends  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Tenant  # noqa: E402

CONFIG = "/tenants/me/config"
CONFIGURATION = "/tenants/me/configuration"
COMPLETENESS = "/tenants/me/context-completeness"

FACTS = {
    "parking": "Estacionamento ao lado.",
    "how_to_arrive": "Metrô, saída B.",
    "payment_methods": ["Pix", "Cartão"],
    "cancellation_policy": "Até 24h antes.",
    "documents_to_bring": ["RG"],
    "accessibility": "Rampa.",
    "faq": [{"question": "Tem wi-fi?", "answer": "Sim."}],
    "notes": "Fechado em feriados.",
}


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
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=None)
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

    # The write routes mutate the tenant and `session.refresh()` it: it must be the SAME ORM
    # instance/session as the route's own `Depends(get_session)`, like production. Re-fetching
    # by id through the overridden dependency reproduces that (same as tests/test_hub_config.py).
    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def test_get_config_has_clinic_facts_null_before_the_first_save(client: AsyncClient) -> None:
    body = (await client.get(CONFIG)).json()
    assert "clinic_facts" in body and body["clinic_facts"] is None


async def test_put_then_get_round_trips_the_facts(client: AsyncClient) -> None:
    put = await client.put(CONFIG, json={"clinic_facts": FACTS})
    assert put.status_code == 200 and put.json()["clinic_facts"]["parking"] == FACTS["parking"]
    got = (await client.get(CONFIG)).json()["clinic_facts"]
    assert got["payment_methods"] == ["Pix", "Cartão"]
    assert got["faq"] == [{"question": "Tem wi-fi?", "answer": "Sim."}]


async def test_configuration_envelope_carries_the_facts_too(client: AsyncClient) -> None:
    put = await client.put(CONFIGURATION, json={"tenant": {"clinic_facts": FACTS}})
    assert put.status_code == 200
    assert put.json()["tenant"]["clinic_facts"]["how_to_arrive"] == FACTS["how_to_arrive"]


async def test_a_save_without_the_field_leaves_the_facts_untouched(client: AsyncClient) -> None:
    await client.put(CONFIG, json={"clinic_facts": FACTS})
    await client.put(CONFIG, json={"persona_notes": "Seja cordial."})
    assert (await client.get(CONFIG)).json()["clinic_facts"]["parking"] == FACTS["parking"]


async def test_null_clears_the_facts(client: AsyncClient) -> None:
    await client.put(CONFIG, json={"clinic_facts": FACTS})
    await client.put(CONFIG, json={"clinic_facts": None})
    assert (await client.get(CONFIG)).json()["clinic_facts"] is None


async def test_the_put_replaces_the_whole_object_it_does_not_merge(client: AsyncClient) -> None:
    await client.put(CONFIG, json={"clinic_facts": FACTS})
    await client.put(CONFIG, json={"clinic_facts": {"parking": "Só isto."}})
    got = (await client.get(CONFIG)).json()["clinic_facts"]
    assert got["parking"] == "Só isto." and got.get("payment_methods") in (None, [])


@pytest.mark.parametrize(
    "bad",
    [
        {"parking": "p" * 301},
        {"payment_methods": ["Pix"] * 11},
        {"faq": [{"question": "", "answer": "a"}]},
    ],
)
async def test_oversized_or_malformed_facts_are_rejected(client: AsyncClient, bad) -> None:
    assert (await client.put(CONFIG, json={"clinic_facts": bad})).status_code == 422


async def test_completeness_of_an_empty_clinic(client: AsyncClient) -> None:
    body = (await client.get(COMPLETENESS)).json()
    assert body["score"] == 0
    keys = {item["key"]: item for item in body["items"]}
    assert keys["address"]["status"] == "missing" and keys["address"]["hint"]
    assert keys["address"]["section"] == "address"
    assert keys["payment_methods"]["section"] == "facts"


async def test_completeness_follows_what_was_saved(client: AsyncClient) -> None:
    await client.put(
        CONFIG, json={"clinic_facts": FACTS, "address": {"line": "Rua A", "city": "Recife"}}
    )
    body = (await client.get(COMPLETENESS)).json()
    status = {i["key"]: i["status"] for i in body["items"]}
    assert status["address"] == "done" and status["parking_or_arrival"] == "done"
    assert status["payment_methods"] == "done" and status["cancellation_policy"] == "done"
    assert status["hours"] == "missing" and 0 < body["score"] < 100
