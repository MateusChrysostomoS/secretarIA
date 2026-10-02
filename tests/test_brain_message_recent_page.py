"""The Portal transcript shows the NEWEST messages, not the oldest 50 (LACUNAS L1)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)

KEY = {"X-Internal-Api-Key": "test-internal-key"}
EXTERNAL_ID = "bm-recent-001"
BASE = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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
async def client(db):
    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()


async def _seed(db, count: int, *, channel: str = "brain_message") -> Tenant:
    """`count` outbound bot rows, body `m0..m{count-1}`, one minute apart, oldest first."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id="1234567890",
            is_active=True,
            clinic_description="x",
            initial_flows={},
        )
        session.add(tenant)
        patient = Patient(
            tenant_id=tenant.id,
            channel=channel,
            external_id=EXTERNAL_ID,
            wa_id=None if channel == "brain_message" else "5511900000000",
            name="Maria",
            lgpd_accepted_at=BASE,
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        for i in range(count):
            at = BASE + timedelta(minutes=i)
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    body=f"m{i}",
                    created_at=at,
                    updated_at=at,
                )
            )
        await session.commit()
        return tenant


async def _seed_stamped(db, stamps: list[datetime]) -> Tenant:
    """One outbound bot row per stamp, body `m1..mN`, id `UUID(int=N)`, in the given order.

    Equal stamps model rows one transaction wrote with the same now(); the explicit ids make
    the `id` tie-break deterministic (`m2` sorts before `m3` among equal `created_at`).
    """
    tenant = await _seed(db, 0)
    async with db() as session:
        conversation_id = await session.scalar(
            select(Conversation.id).where(Conversation.tenant_id == tenant.id)
        )
        for n, at in enumerate(stamps, start=1):
            session.add(
                Message(
                    id=UUID(int=n),
                    conversation_id=conversation_id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    body=f"m{n}",
                    created_at=at,
                    updated_at=at,
                )
            )
        await session.commit()
    return tenant


def _url() -> str:
    return f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages"


async def test_first_load_returns_the_newest_fifty_oldest_first(client, db) -> None:
    tenant = await _seed(db, 60)
    resp = await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)
    assert resp.status_code == 200
    body = resp.json()
    bodies = [m["body"] for m in body["data"]]
    assert bodies == [f"m{i}" for i in range(10, 60)]
    assert body["has_more"] is True


async def test_exactly_the_page_size_has_no_more(client, db) -> None:
    tenant = await _seed(db, 50)
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert [m["body"] for m in body["data"]] == [f"m{i}" for i in range(50)]
    assert body["has_more"] is False


async def test_one_over_the_page_size_has_more(client, db) -> None:
    tenant = await _seed(db, 51)
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body["data"][0]["body"] == "m1"
    assert body["data"][-1]["body"] == "m50"
    assert body["has_more"] is True


async def test_before_pages_backwards_without_overlap(client, db) -> None:
    tenant = await _seed(db, 60)
    first = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    oldest_seen = first["data"][0]["created_at"]
    older = (
        await client.get(
            _url(), params={"tenant_id": str(tenant.id), "before": oldest_seen}, headers=KEY
        )
    ).json()
    assert [m["body"] for m in older["data"]] == [f"m{i}" for i in range(10)]
    assert older["has_more"] is False


async def test_empty_conversation_is_empty_not_an_error(client, db) -> None:
    tenant = await _seed(db, 0)
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body == {"data": [], "has_more": False}


async def test_since_and_before_together_are_refused(client, db) -> None:
    tenant = await _seed(db, 3)
    resp = await client.get(
        _url(),
        params={
            "tenant_id": str(tenant.id),
            "since": BASE.isoformat(),
            "before": BASE.isoformat(),
        },
        headers=KEY,
    )
    assert resp.status_code == 422


async def test_since_branch_keeps_its_old_behaviour(client, db) -> None:
    tenant = await _seed(db, 5)
    cursor = (BASE + timedelta(minutes=2)).isoformat()
    body = (
        await client.get(_url(), params={"tenant_id": str(tenant.id), "since": cursor}, headers=KEY)
    ).json()
    assert [m["body"] for m in body["data"]] == ["m3", "m4"]
    assert body["has_more"] is False


async def test_a_whatsapp_patient_with_the_same_string_is_unreachable(client, db) -> None:
    tenant = await _seed(db, 3, channel="whatsapp")
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body == {"data": [], "has_more": False}


async def test_a_tie_group_straddling_the_page_boundary_is_neither_lost_nor_split(
    client, db
) -> None:
    # t0 / t1 t1 t1 / t2 with limit=2: the cut falls INSIDE the t1 group. A strict
    # `created_at < before` cursor would skip the t1 rows the first page left out.
    t0, t1, t2 = BASE, BASE + timedelta(minutes=1), BASE + timedelta(minutes=2)
    tenant = await _seed_stamped(db, [t0, t1, t1, t1, t2])
    params = {"tenant_id": str(tenant.id), "limit": 2}

    first = (await client.get(_url(), params=params, headers=KEY)).json()
    # The page is completed with the whole t1 group, so it exceeds `limit` by the tie.
    assert [m["body"] for m in first["data"]] == ["m2", "m3", "m4", "m5"]
    assert first["has_more"] is True

    older = (
        await client.get(
            _url(), params={**params, "before": first["data"][0]["created_at"]}, headers=KEY
        )
    ).json()
    assert [m["body"] for m in older["data"]] == ["m1"]
    assert older["has_more"] is False

    seen = [m["id"] for m in older["data"] + first["data"]]
    assert len(seen) == len(set(seen)) == 5


async def test_a_tie_group_at_the_very_start_of_the_conversation_ends_the_paging(
    client, db
) -> None:
    # t0 t0 t0 / t1 with limit=2: the group the cut lands in is also the oldest one, so
    # completing it leaves nothing older - `has_more` must come back false, not true.
    t0, t1 = BASE, BASE + timedelta(minutes=1)
    tenant = await _seed_stamped(db, [t0, t0, t0, t1])

    body = (
        await client.get(_url(), params={"tenant_id": str(tenant.id), "limit": 2}, headers=KEY)
    ).json()
    assert [m["body"] for m in body["data"]] == ["m1", "m2", "m3", "m4"]
    assert body["has_more"] is False
