"""The "digitando" flag: three typers, two viewers, never a source of errors."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")


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
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fakeredis import FakeRedis, FakeServer  # noqa: E402
from fakeredis.aioredis import FakeRedis as AsyncFakeRedis  # noqa: E402
from sqlalchemy import select, update  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services import typing_indicator as ti  # noqa: E402
from secretaria.services.turn_safety_net import note_send  # noqa: E402
from secretaria.workers import orchestrator, tasks  # noqa: E402
from tests._patching import workers_ns  # noqa: E402


class _Redis(AsyncFakeRedis):
    """Redis emulator with Lua: exercise the actual atomic scripts, not a script stub."""

    def __init__(self) -> None:
        server = FakeServer()
        super().__init__(server=server, decode_responses=True)
        self._sync = FakeRedis(server=server, decode_responses=True)

    @property
    def data(self):
        return {
            key: (
                self._sync.get(key)
                if self._sync.type(key) == "string"
                else self._sync.zrange(key, 0, -1),
                self._sync.ttl(key),
            )
            for key in self._sync.scan_iter()
        }


class _BrokenRedis:
    async def eval(self, *a, **k):
        raise ConnectionError("redis down")

    async def zrem(self, *a, **k):
        raise ConnectionError("redis down")

    async def setex(self, *a, **k):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")

    async def delete(self, *a, **k):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")

    async def exists(self, *a, **k):  # noqa: ANN002, ANN003
        raise ConnectionError("redis down")


async def test_the_patient_sees_the_automation_and_the_staff_but_never_themselves() -> None:
    redis, cid = _Redis(), uuid4()
    assert await ti.typing_by(redis, cid, viewer="patient") is None
    await ti.mark_typing(redis, cid, "patient")
    assert await ti.typing_by(redis, cid, viewer="patient") is None  # not their own typing
    await ti.mark_typing(redis, cid, "staff")
    assert await ti.typing_by(redis, cid, viewer="patient") == "staff"
    await ti.mark_typing(redis, cid, "automation")
    assert await ti.typing_by(redis, cid, viewer="patient") == "automation"  # priority


async def test_the_clinic_sees_the_automation_and_the_patient_never_staff() -> None:
    redis, cid = _Redis(), uuid4()
    await ti.mark_typing(redis, cid, "staff")
    assert await ti.typing_by(redis, cid, viewer="staff") is None
    await ti.mark_typing(redis, cid, "patient")
    assert await ti.typing_by(redis, cid, viewer="staff") == "patient"
    await ti.mark_typing(redis, cid, "automation")
    assert await ti.typing_by(redis, cid, viewer="staff") == "automation"


async def test_each_typer_has_its_own_ttl_and_clear(monkeypatch) -> None:
    # The emulator reports remaining TTL, so freeze its wall clock for exact checks.
    monkeypatch.setattr("fakeredis._helpers.time.time", lambda: 1_800_000_000.0)
    redis, cid = _Redis(), uuid4()
    await ti.mark_typing(redis, cid, "automation")
    await ti.mark_typing(redis, cid, "staff")
    await ti.mark_typing(redis, cid, "patient")
    assert redis.data[ti.typing_key(cid, "patient")][1] == 6
    assert redis.data[ti.typing_key(cid, "automation")][1] == 90
    assert redis.data[ti.typing_key(cid, "staff")][1] == 6
    await ti.clear_typing(redis, cid, "automation")
    assert await ti.typing_by(redis, cid, viewer="patient") == "staff"


async def test_a_missing_redis_is_just_not_typing() -> None:
    cid = uuid4()
    await ti.mark_typing(None, cid, "automation")
    await ti.clear_typing(None, cid, "automation")
    assert await ti.typing_by(None, cid, viewer="patient") is None


async def test_a_broken_redis_never_raises() -> None:
    redis, cid = _BrokenRedis(), uuid4()
    await ti.mark_typing(redis, cid, "patient")
    await ti.clear_typing(redis, cid, "patient")
    assert await ti.typing_by(redis, cid, viewer="staff") is None


async def test_a_pool_without_exists_is_tolerated() -> None:
    """The test double used by older suites only has `enqueue_job`."""

    class _OnlyEnqueue:
        async def enqueue_job(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    assert await ti.typing_by(_OnlyEnqueue(), uuid4(), viewer="patient") is None


async def test_slow_redis_has_a_bounded_budget() -> None:
    import asyncio

    class SlowRedis:
        async def eval(self, *args):
            await asyncio.sleep(10)

        async def zrem(self, *args):
            await asyncio.sleep(10)

        async def setex(self, *args):
            await asyncio.sleep(10)

        async def delete(self, *args):
            await asyncio.sleep(10)

        async def exists(self, *args):
            await asyncio.sleep(10)

    redis, cid = SlowRedis(), uuid4()
    async with asyncio.timeout(1):
        await ti.mark_typing(redis, cid, "automation")
        await ti.clear_typing(redis, cid, "automation")
        assert await ti.typing_by(redis, cid, viewer="patient") is None


def _reply(channel: str):
    return tasks._ReplyContext(
        channel=channel,
        conversation_id=uuid4(),
        patient_ref="ref",
        inbound_body="oi",
        tenant_id=uuid4(),
    )


async def test_automation_flag_is_on_during_a_portal_turn_and_off_after(monkeypatch) -> None:
    redis = _Redis()
    reply = _reply("brain_message")
    seen: list[str | None] = []

    async def _inner(r, redis=None):  # noqa: ANN001
        seen.append(await ti.typing_by(redis, r.conversation_id, viewer="patient"))
        note_send()

    monkeypatch.setattr(workers_ns, "_send_bot_reply_inner", _inner)
    await orchestrator._send_bot_reply(reply, redis=redis)
    assert seen == ["automation"]
    assert await ti.typing_by(redis, reply.conversation_id, viewer="patient") is None


async def test_the_flag_is_cleared_even_when_the_turn_raises(monkeypatch) -> None:
    redis = _Redis()
    reply = _reply("brain_message")

    async def _boom(r, redis=None):  # noqa: ANN001
        assert await ti.typing_by(redis, r.conversation_id, viewer="patient") == "automation"
        raise RuntimeError("downstream exploded")

    monkeypatch.setattr(workers_ns, "_send_bot_reply_inner", _boom)
    await orchestrator._send_bot_reply(reply, redis=redis)  # the net absorbs it
    assert await ti.typing_by(redis, reply.conversation_id, viewer="patient") is None


async def test_a_whatsapp_turn_never_sets_the_flag(monkeypatch) -> None:
    redis = _Redis()
    reply = _reply("whatsapp")
    seen: list[str | None] = []

    async def _inner(r, redis=None):  # noqa: ANN001
        seen.append(await ti.typing_by(redis, r.conversation_id, viewer="patient"))
        note_send()

    monkeypatch.setattr(workers_ns, "_send_bot_reply_inner", _inner)
    await orchestrator._send_bot_reply(reply, redis=redis)
    assert seen == [None]


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

    old_pool = getattr(app.state, "arq_pool", None)
    app.state.arq_pool = None
    app.dependency_overrides[get_session] = _override_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c
    app.dependency_overrides.clear()
    app.state.arq_pool = old_pool


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


async def _conversation_id(db):
    async with db() as session:
        return await session.scalar(select(Conversation.id))


async def test_the_listing_reports_who_is_typing(client, db) -> None:
    from secretaria.main import app

    tenant = await _seed(db, 2)
    pool = _Redis()
    app.state.arq_pool = pool
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert (body["typing"], body["typing_by"]) == (False, None)

    cid = await _conversation_id(db)
    await ti.mark_typing(pool, cid, "staff")
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert (body["typing"], body["typing_by"]) == (True, "staff")

    await ti.mark_typing(pool, cid, "automation")
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body["typing_by"] == "automation"


async def test_the_patient_never_sees_their_own_typing(client, db) -> None:
    from secretaria.main import app

    tenant = await _seed(db, 1)
    pool = _Redis()
    app.state.arq_pool = pool
    await ti.mark_typing(pool, await _conversation_id(db), "patient")
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert (body["typing"], body["typing_by"]) == (False, None)


async def test_accepts_typing_only_while_a_human_conducts(client, db) -> None:
    tenant = await _seed(db, 1)
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body["accepts_typing"] is False  # automation conducting

    async with db() as session:
        await session.execute(
            update(Conversation).values(handover_state=HandoverState.HUMAN_ACTIVE)
        )
        await session.commit()
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert body["accepts_typing"] is True


async def test_the_listing_survives_a_pool_without_redis_methods(client, db) -> None:
    from secretaria.main import app

    class _OnlyEnqueue:
        async def enqueue_job(self, *a, **k):  # noqa: ANN002, ANN003
            return None

    tenant = await _seed(db, 1)
    app.state.arq_pool = _OnlyEnqueue()
    body = (await client.get(_url(), params={"tenant_id": str(tenant.id)}, headers=KEY)).json()
    assert (body["typing"], body["typing_by"]) == (False, None)


TYPING_URL = "/internal/brain-message/typing"


async def test_patient_typing_is_not_processed_while_the_automation_conducts(client, db) -> None:
    from secretaria.main import app

    tenant = await _seed(db, 1)
    pool = _Redis()
    app.state.arq_pool = pool
    body = {"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID}
    resp = await client.post(TYPING_URL, json=body, headers=KEY)
    assert resp.status_code == 200
    assert resp.json() == {"applied": False}
    assert pool.data == {}  # nothing written: nobody would read it


async def test_patient_typing_is_recorded_when_a_human_conducts(client, db) -> None:
    from secretaria.main import app

    tenant = await _seed(db, 1)
    pool = _Redis()
    app.state.arq_pool = pool
    async with db() as session:
        await session.execute(
            update(Conversation).values(handover_state=HandoverState.HUMAN_ACTIVE)
        )
        await session.commit()
    resp = await client.post(
        TYPING_URL, json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID}, headers=KEY
    )
    assert resp.json() == {"applied": True}
    assert await ti.typing_by(pool, await _conversation_id(db), viewer="staff") == "patient"


async def test_patient_typing_for_an_unknown_patient_is_a_quiet_no(client, db) -> None:
    tenant = await _seed(db, 1)
    resp = await client.post(
        TYPING_URL, json={"tenant_id": str(tenant.id), "external_id": "nobody"}, headers=KEY
    )
    assert resp.status_code == 200
    assert resp.json() == {"applied": False}


async def test_patient_typing_requires_the_internal_key_and_a_closed_body(client, db) -> None:
    tenant = await _seed(db, 1)
    body = {"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID}
    assert (await client.post(TYPING_URL, json=body)).status_code in (401, 403)
    assert (
        await client.post(TYPING_URL, json={**body, "by": "staff"}, headers=KEY)
    ).status_code == 422


@pytest.mark.parametrize("channel", ["whatsapp", "brain_message"])
async def test_patient_beat_requires_own_tenant_and_portal_channel(client, db, channel):
    from secretaria.main import app

    tenant = await _seed(db, 1, channel=channel)
    pool = _Redis()
    app.state.arq_pool = pool
    async with db() as session:
        await session.execute(
            update(Conversation).values(handover_state=HandoverState.HUMAN_ACTIVE)
        )
        await session.commit()
    scopes = [uuid4()] if channel == "brain_message" else [tenant.id]
    for scope in scopes:
        response = await client.post(
            TYPING_URL, json={"tenant_id": str(scope), "external_id": EXTERNAL_ID}, headers=KEY
        )
        assert response.json() == {"applied": False}
        assert pool.data == {}


@pytest.mark.parametrize("pool", [None, _BrokenRedis()])
async def test_patient_beat_redis_failure_remains_normal_response(client, db, pool):
    from secretaria.main import app

    tenant = await _seed(db, 1)
    app.state.arq_pool = pool
    async with db() as session:
        await session.execute(
            update(Conversation).values(handover_state=HandoverState.HUMAN_ACTIVE)
        )
        await session.commit()
    response = await client.post(
        TYPING_URL, json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID}, headers=KEY
    )
    assert response.status_code == 200
    assert response.json() == {"applied": True}


async def test_cancelling_a_running_turn_clears_automation(monkeypatch):
    import asyncio

    started = asyncio.Event()
    pool, reply = _Redis(), _reply("brain_message")

    async def inner(r, redis=None):
        started.set()
        await asyncio.sleep(10)

    monkeypatch.setattr(workers_ns, "_send_bot_reply_inner", inner)
    task = asyncio.create_task(orchestrator._send_bot_reply(reply, redis=pool))
    await started.wait()
    assert await ti.typing_by(pool, reply.conversation_id, viewer="patient") == "automation"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await ti.typing_by(pool, reply.conversation_id, viewer="patient") is None


async def test_cancel_during_initial_redis_write_cleans_the_mark(monkeypatch):
    import asyncio

    class InterruptedRedis(_Redis):
        def __init__(self):
            super().__init__()
            self.written = asyncio.Event()

        async def eval(self, script, *args):
            result = await super().eval(script, *args)
            if "ZADD" in script:
                self.written.set()
                await asyncio.Future()
            return result

    pool, reply = InterruptedRedis(), _reply("brain_message")
    task = asyncio.create_task(orchestrator._send_bot_reply(reply, redis=pool))
    await pool.written.wait()
    assert await pool.exists(ti.typing_key(reply.conversation_id, "automation"))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not await pool.exists(ti.typing_key(reply.conversation_id, "automation"))


@pytest.mark.parametrize("first_to_finish", [0, 1])
@pytest.mark.parametrize("cancel_first", [False, True])
async def test_overlapping_turns_remain_visible_until_both_finish(
    monkeypatch, first_to_finish, cancel_first
):
    import asyncio

    pool = _Redis()
    replies = [_reply("brain_message"), _reply("brain_message")]
    from dataclasses import replace

    replies[1] = replace(replies[1], conversation_id=replies[0].conversation_id)
    started = [asyncio.Event(), asyncio.Event()]
    finish = [asyncio.Event(), asyncio.Event()]

    async def inner(reply, redis=None):
        index = 0 if reply is replies[0] else 1
        started[index].set()
        await finish[index].wait()
        note_send()

    monkeypatch.setattr(workers_ns, "_send_bot_reply_inner", inner)
    running = [
        asyncio.create_task(orchestrator._send_bot_reply(reply, redis=pool)) for reply in replies
    ]
    try:
        await asyncio.gather(*(event.wait() for event in started))
        assert (
            await ti.typing_by(pool, replies[0].conversation_id, viewer="patient") == "automation"
        )
        if cancel_first:
            running[first_to_finish].cancel()
            with pytest.raises(asyncio.CancelledError):
                await running[first_to_finish]
        else:
            finish[first_to_finish].set()
            await running[first_to_finish]
        assert not running[1 - first_to_finish].done()
        assert (
            await ti.typing_by(pool, replies[0].conversation_id, viewer="patient") == "automation"
        )
    finally:
        for event in finish:
            event.set()
        await asyncio.gather(*running, return_exceptions=True)
    assert await ti.typing_by(pool, replies[0].conversation_id, viewer="patient") is None


async def test_dead_automation_turn_expires_despite_a_newer_turn():
    pool, cid = _Redis(), uuid4()
    await ti.mark_typing(pool, cid, "automation", turn_id="dead")
    await ti.mark_typing(pool, cid, "automation", turn_id="live")
    # A dead owner's deadline elapsed, but the newer owner renewed the key's TTL.
    await pool.zadd(ti.typing_key(cid, "automation"), {"dead": 0})
    assert await ti.typing_by(pool, cid, viewer="patient") == "automation"
    await ti.clear_typing(pool, cid, "automation", turn_id="live")
    assert await ti.typing_by(pool, cid, viewer="patient") is None
    assert not await pool.exists(ti.typing_key(cid, "automation"))
