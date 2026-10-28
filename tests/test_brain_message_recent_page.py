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
from secretaria.services.payments.payer_data import REDACTED_BODY  # noqa: E402

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
    assert body == {
        "data": [],
        "has_more": False,
        "typing": False,
        "typing_by": None,
        "accepts_typing": False,
    }


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
    assert body == {
        "data": [],
        "has_more": False,
        "typing": False,
        "typing_by": None,
        "accepts_typing": False,
    }


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


# ---- the redacted-document placeholder never reaches the PATIENT ---------------------------------


async def _add_rows(db, tenant: Tenant, rows: list[tuple[str, str, datetime]]) -> None:
    """Append `(kind, body, at)` rows: kind `bot` is outbound/bot, `marker` is the inbound/patient
    row the worker stores in place of a CPF answer (the answer's own body is `body`)."""
    async with db() as session:
        conversation_id = await session.scalar(
            select(Conversation.id).where(Conversation.tenant_id == tenant.id)
        )
        for kind, body, at in rows:
            patient = kind != "bot"
            session.add(
                Message(
                    conversation_id=conversation_id,
                    direction=MessageDirection.INBOUND if patient else MessageDirection.OUTBOUND,
                    sender=MessageSender.PATIENT if patient else MessageSender.BOT,
                    body=body,
                    created_at=at,
                    updated_at=at,
                )
            )
        await session.commit()


def _minute(n: int) -> datetime:
    return BASE + timedelta(minutes=n)


async def test_patient_transcript_hides_redacted_document_answer(client, db) -> None:
    tenant = await _seed(db, 0)
    await _add_rows(
        db,
        tenant,
        [
            ("bot", "Envie seu CPF", _minute(0)),
            ("patient", REDACTED_BODY, _minute(1)),
            ("bot", "Recebi, obrigada", _minute(2)),
            ("patient", "obrigada tambem", _minute(3)),
        ],
    )
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert [m["body"] for m in body["data"]] == [
        "Envie seu CPF",
        "Recebi, obrigada",
        "obrigada tambem",
    ]
    assert body["has_more"] is False

    # The clinic console is a different reader: it keeps the row (the staff sees that an
    # answer was given), and so does the database.
    from secretaria.api.hub.deps import get_current_tenant
    from secretaria.main import app

    async def _staff_tenant():
        return tenant

    app.dependency_overrides[get_current_tenant] = _staff_tenant
    try:
        async with db() as session:
            conversation_id = await session.scalar(
                select(Conversation.id).where(Conversation.tenant_id == tenant.id)
            )
        console = (
            await client.get(f"/tenants/me/conversations/{conversation_id}/messages")
        ).json()
    finally:
        app.dependency_overrides.pop(get_current_tenant, None)
    assert [m["body"] for m in console] == [
        "Envie seu CPF",
        REDACTED_BODY,
        "Recebi, obrigada",
        "obrigada tambem",
    ]


async def test_only_the_patients_own_marker_row_is_hidden(client, db) -> None:
    # The same text from the BOT (outbound) is not the redacted answer: nothing but the
    # inbound/patient row is dropped, so a clinic that really writes it is still read.
    tenant = await _seed(db, 0)
    await _add_rows(
        db,
        tenant,
        [("bot", REDACTED_BODY, _minute(0)), ("patient", REDACTED_BODY, _minute(1))],
    )
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert [(m["sender"], m["body"]) for m in body["data"]] == [("bot", REDACTED_BODY)]


async def test_a_hidden_row_at_the_page_boundary_loses_no_visible_message(client, db) -> None:
    # a(t0) b(t1) MARKER(t2) c(t3), limit=2: the newest page is [MARKER, c]. has_more is
    # decided from the rows read (not from what is shown) and the next `before` is the oldest
    # created_at the caller SAW (c's): the next page re-reads MARKER (hidden again) and b.
    tenant = await _seed(db, 0)
    await _add_rows(
        db,
        tenant,
        [
            ("bot", "a", _minute(0)),
            ("bot", "b", _minute(1)),
            ("patient", REDACTED_BODY, _minute(2)),
            ("bot", "c", _minute(3)),
        ],
    )
    params = {"tenant_id": str(tenant.id), "limit": 2}
    first = (await client.get(_url(), params=params, headers=KEY)).json()
    assert [m["body"] for m in first["data"]] == ["c"]
    assert first["has_more"] is True  # a, b are older: the paging must go on

    second = (
        await client.get(
            _url(), params={**params, "before": first["data"][0]["created_at"]}, headers=KEY
        )
    ).json()
    assert [m["body"] for m in second["data"]] == ["b"]
    assert second["has_more"] is True

    third = (
        await client.get(
            _url(), params={**params, "before": second["data"][0]["created_at"]}, headers=KEY
        )
    ).json()
    assert [m["body"] for m in third["data"]] == ["a"]
    assert third["has_more"] is False

    seen = [m["body"] for page in (third, second, first) for m in page["data"]]
    assert seen == ["a", "b", "c"]  # nothing lost, nothing repeated, in order


async def test_a_hidden_row_inside_a_tie_group_keeps_the_group_whole(client, db) -> None:
    # One transaction stamps the answer and the reply with the same created_at. The group
    # completion must still see the hidden row (it is hidden only when the page is built).
    tenant = await _seed(db, 0)
    await _add_rows(
        db,
        tenant,
        [
            ("bot", "ask", _minute(0)),
            ("patient", REDACTED_BODY, _minute(1)),
            ("bot", "thanks", _minute(1)),
            ("bot", "last", _minute(2)),
        ],
    )
    body = (
        await client.get(_url(), params={"tenant_id": str(tenant.id), "limit": 2}, headers=KEY)
    ).json()
    assert REDACTED_BODY not in [m["body"] for m in body["data"]]
    assert [m["body"] for m in body["data"]] == ["thanks", "last"]
    assert body["has_more"] is True  # "ask" is older


async def test_the_since_branch_hides_the_marker_and_keeps_its_neighbours(client, db) -> None:
    tenant = await _seed(db, 0)
    await _add_rows(
        db,
        tenant,
        [
            ("bot", "old", _minute(0)),
            ("bot", "before", _minute(5)),
            ("patient", REDACTED_BODY, _minute(6)),
            ("bot", "after", _minute(7)),
        ],
    )
    cursor = _minute(1).isoformat()
    body = (
        await client.get(_url(), params={"tenant_id": str(tenant.id), "since": cursor}, headers=KEY)
    ).json()
    assert [m["body"] for m in body["data"]] == ["before", "after"]
    assert body["has_more"] is False
