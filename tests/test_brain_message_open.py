"""`POST /internal/brain-message/open` — the automation speaks first.

Origin: TASK-003 §2. Before this route existed, `Conversation` had exactly one
constructor (`workers/tasks.py::_get_or_create_conversation`) and exactly one
road to it, `_route_inbound_turn`, which every path reaches through an inbound
message. A patient who opened a clinic's Portal link and had not typed yet
therefore sat in an empty chat.

What this file defends, in order:

  1. the route's two answers and the status codes that carry them;
  2. the thing that must NEVER happen — an inbound `Message` the patient did
     not write, anywhere on this path;
  3. idempotency in both layers, because one greeting per visitor is the whole
     contract: the route's read (cheap, and enough for a returning visitor) and
     the worker's `ProcessedEvent` claim (durable, and the only one that
     survives two concurrent calls);
  4. that the greeting is the SAME first-contact turn an inbound would have
     produced — same frame, same e-mail question — rather than a second
     greeting machine;
  5. WhatsApp: unreachable from here, by construction and by test.

Fixtures follow tests/test_brain_message_pipeline.py (in-memory SQLite on a
StaticPool, the real ASGI app, a recording queue).
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    ConsentEvent,
    Conversation,
    Message,
    MessageDirection,
    Patient,
    ProcessedEvent,
    Tenant,
)
from secretaria.services.channel_sender import (  # noqa: E402
    CHANNEL_BRAIN_MESSAGE,
    CHANNEL_WHATSAPP,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.greeting_template import render_greeting  # noqa: E402
from secretaria.services.pending_identity import (  # noqa: E402
    EMAIL_REQUEST_MESSAGE,
    IdentityState,
)
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
EXTERNAL_ID = "bm-session-open-1"
GOOD_KEY = "test-internal-key"
OPEN_JOB = "process_brain_message_open"


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


class _ExplodingWhatsAppClient:
    """Any use at all fails the test. Nothing on this path may reach Meta."""

    @classmethod
    def for_tenant(cls, tenant, access_token):  # pragma: no cover - must not run
        raise AssertionError("the open path reached the Graph API")


class _State:
    """What the identity leg was asked for, and what it should answer."""

    def __init__(self) -> None:
        self.probed: list[str] = []
        self.answer = IdentityState.PENDING_UNCLAIMED
        self.entitled = True


@pytest.fixture
def state() -> _State:
    return _State()


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db, state):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    monkeypatch.setattr(tasks, "WhatsAppClient", _ExplodingWhatsAppClient)

    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

    async def _fake_token(session, tenant_id):
        return "decrypted-waba-token"

    async def _fake_entitlements(tenant_id, redis):
        if not state.entitled:
            return None
        return EntitlementSummary(
            tenant_id=str(tenant_id),
            status="active",
            active=True,
            secretaria_enabled=True,
            plan="bronze",
            secretaria_tier="basico",
            addons={},
            limits={},
        )

    async def _probe(tenant_id, external_id):
        state.probed.append(external_id)
        return state.answer

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(tasks, "get_waba_token", _fake_token)
    monkeypatch.setattr(tasks, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(tasks, "probe_identity", _probe)
    yield


class _FakeArqPool:
    """Records what the endpoint enqueued, so a test can run it as the worker would."""

    def __init__(self) -> None:
        self.jobs: list[tuple] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


@pytest_asyncio.fixture
async def api(db, monkeypatch: pytest.MonkeyPatch):
    """The real ASGI app, on the in-memory DB, with a recording queue."""
    from httpx import ASGITransport, AsyncClient

    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    pool = _FakeArqPool()
    app.state.arq_pool = pool
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, pool
    app.dependency_overrides.clear()
    app.state.arq_pool = None


async def _seed_tenant(db, phone_number_id: str = PHONE_NUMBER_ID) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=phone_number_id,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _open(tenant: Tenant, external_id: str = EXTERNAL_ID, name: str | None = "Maria"):
    """One open, decided AND delivered — what the arq job does end to end."""
    await tasks.process_brain_message_open(
        {"redis": None}, str(tenant.id), external_id, patient_name=name
    )


async def _bodies(db, tenant: Tenant, direction: MessageDirection) -> list[str]:
    async with db() as session:
        rows = (
            await session.scalars(
                select(Message.body)
                .join(Conversation, Conversation.id == Message.conversation_id)
                .where(Conversation.tenant_id == tenant.id, Message.direction == direction)
            )
        ).all()
    return list(rows)


async def _claim_held(db, tenant: Tenant, external_id: str = EXTERNAL_ID) -> bool:
    async with db() as session:
        row = await session.scalar(
            select(ProcessedEvent.id).where(
                ProcessedEvent.event_id == f"brain_message_open:{tenant.id}:{external_id}"
            )
        )
    return row is not None


# --------------------------------------------------------------------------
# 1. The route: two answers, two status codes
# --------------------------------------------------------------------------


async def test_a_visitor_who_has_not_written_gets_a_queued_greeting(api, db) -> None:
    """202 `queued`, and the job carries exactly what the worker needs."""
    client, pool = api
    tenant = await _seed_tenant(db)

    response = await client.post(
        "/internal/brain-message/open",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "patient_name": "Maria"},
    )

    assert response.status_code == 202, response.text
    assert response.json() == {"status": "queued"}
    assert pool.jobs == [
        (OPEN_JOB, (str(tenant.id), EXTERNAL_ID), {"patient_name": "Maria"}),
    ]


async def test_a_conversation_that_already_started_is_never_greeted_again(api, db) -> None:
    """200 `exists`, and NOTHING is queued — the contract's second answer.

    The patient wrote first; an unsolicited greeting on top of a live thread
    would read as the clinic forgetting the conversation it is in.
    """
    client, pool = api
    tenant = await _seed_tenant(db)
    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id, external_id=EXTERNAL_ID, text="oi"
    )
    assert reply is not None

    response = await client.post(
        "/internal/brain-message/open",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "patient_name": None},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {"status": "exists"}
    assert pool.jobs == []


async def test_the_route_is_behind_the_same_key_as_every_other_internal_route(api, db) -> None:
    client, pool = api
    tenant = await _seed_tenant(db)

    response = await client.post(
        "/internal/brain-message/open",
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID},
    )

    assert response.status_code == 401
    assert pool.jobs == []


@pytest.mark.parametrize(
    "body",
    [
        {"external_id": EXTERNAL_ID},
        {"tenant_id": str(uuid4())},
        {"tenant_id": "not-a-uuid", "external_id": EXTERNAL_ID},
        {"tenant_id": str(uuid4()), "external_id": ""},
        {"tenant_id": str(uuid4()), "external_id": "x" * 65},
        # Strict schema: a field brain-api has not agreed on is a clear 422,
        # never a silently dropped value (`frozen-contract-migration`).
        {"tenant_id": str(uuid4()), "external_id": EXTERNAL_ID, "text": "oi"},
    ],
)
async def test_a_malformed_body_is_422_and_queues_nothing(api, body) -> None:
    client, pool = api

    response = await client.post(
        "/internal/brain-message/open",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json=body,
    )

    assert response.status_code == 422, response.text
    assert pool.jobs == []


async def test_no_queue_is_503_not_a_promise_of_a_greeting(api, db) -> None:
    """Answering 202 with nothing enqueued would promise a greeting forever."""
    from secretaria.main import app

    client, _pool = api
    tenant = await _seed_tenant(db)
    app.state.arq_pool = None

    response = await client.post(
        "/internal/brain-message/open",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID},
    )

    assert response.status_code == 503


async def test_a_queue_that_exists_but_refuses_is_also_503(api, db) -> None:
    """A pool OBJECT is not a working queue.

    The existence check cannot see a Redis that accepts the connection and then
    fails the enqueue; without the guard that case leaves as a generic 500,
    contradicting the route's own fail-closed promise.
    """
    from secretaria.main import app

    client, _pool = api
    tenant = await _seed_tenant(db)

    class _BrokenPool:
        async def enqueue_job(self, *args, **kwargs):
            raise RuntimeError("redis is gone")

    app.state.arq_pool = _BrokenPool()

    response = await client.post(
        "/internal/brain-message/open",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID},
    )

    assert response.status_code == 503


async def test_both_answers_are_typed_in_the_published_openapi(api) -> None:
    """The caller branches on the status code, so BOTH must carry a schema.

    FastAPI attaches `response_model` only to a route's declared status code,
    which would leave the 200 `exists` answer documented as an untyped empty
    body for anything generated from this spec.
    """
    from secretaria.main import app

    responses = app.openapi()["paths"]["/internal/brain-message/open"]["post"]["responses"]

    for code in ("200", "202"):
        schema = responses[code]["content"]["application/json"]["schema"]
        assert "BrainMessageAck" in str(schema), (code, schema)


# --------------------------------------------------------------------------
# 2. The job: a greeting, and no fabricated patient bubble
# --------------------------------------------------------------------------


async def test_the_open_greets_and_asks_for_the_email_with_no_inbound_at_all(db, state) -> None:
    """The point of the whole route, and the line it must not cross.

    The patient receives exactly what a first inbound would have produced —
    the frame, then the e-mail question — and the transcript contains NO
    message from them, because they did not write one. A fabricated inbound
    would lie to the patient's own console, to the staff console and to the
    model's history all at once.
    """
    tenant = await _seed_tenant(db)

    await _open(tenant)

    assert await _bodies(db, tenant, MessageDirection.INBOUND) == []
    outbound = await _bodies(db, tenant, MessageDirection.OUTBOUND)
    assert outbound == [
        render_greeting(tenant.clinic_name, tenant.clinic_description),
        EMAIL_REQUEST_MESSAGE,
    ]
    assert state.probed == [EXTERNAL_ID]


async def test_the_open_creates_the_phoneless_patient_and_one_consent_event(db) -> None:
    """The same rows an inbound creates, minus the message."""
    tenant = await _seed_tenant(db)

    await _open(tenant)

    async with db() as session:
        patient = await session.scalar(select(Patient))
        assert patient is not None
        assert patient.channel == CHANNEL_BRAIN_MESSAGE
        assert patient.external_id == EXTERNAL_ID
        assert patient.wa_id is None
        assert patient.name == "Maria"
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
        events = (await session.execute(select(ConsentEvent))).scalars().all()
        assert [(e.wa_id, e.kind) for e in events] == [(EXTERNAL_ID, "first_contact_service")]


async def test_an_already_verified_visitor_is_taken_straight_to_the_menu(db, state) -> None:
    """The opening turn is the SHARED first-contact turn, not a copy of it.

    Proved by a behaviour that lives entirely in `_send_bot_reply`: a visitor
    brain-api already knows skips the e-mail question. If this route had grown
    its own greeting path, that branch would simply not exist here.
    """
    tenant = await _seed_tenant(db)
    state.answer = IdentityState.VERIFIED

    await _open(tenant)

    outbound = await _bodies(db, tenant, MessageDirection.OUTBOUND)
    assert EMAIL_REQUEST_MESSAGE not in outbound
    assert len(outbound) > 1, outbound
    async with db() as session:
        patient = await session.scalar(select(Patient))
        assert patient is not None and patient.lgpd_accepted_at is not None


# --------------------------------------------------------------------------
# 3. Idempotency — one greeting per visitor, both layers
# --------------------------------------------------------------------------


async def test_two_opens_for_the_same_visitor_produce_one_greeting(db, state) -> None:
    """The durable half. Both calls pass the route's read; only one may speak.

    This is the F5 case the owner named: a refresh that resumes the SAME
    pending visit arrives with the same `external_id`, and the second call must
    add nothing to the transcript.
    """
    tenant = await _seed_tenant(db)

    await _open(tenant)
    before = await _bodies(db, tenant, MessageDirection.OUTBOUND)
    await _open(tenant)

    assert await _bodies(db, tenant, MessageDirection.OUTBOUND) == before
    assert state.probed == [EXTERNAL_ID], "the second open still called brain-api"
    assert await _claim_held(db, tenant)


async def test_an_open_after_the_patient_wrote_says_nothing(db) -> None:
    """The job re-checks what the route checked, inside its own transaction."""
    tenant = await _seed_tenant(db)
    reply = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id, external_id=EXTERNAL_ID, text="oi"
    )
    assert reply is not None
    await tasks._send_bot_reply(reply, redis=None)
    before = await _bodies(db, tenant, MessageDirection.OUTBOUND)

    await _open(tenant)

    assert await _bodies(db, tenant, MessageDirection.OUTBOUND) == before
    assert await _bodies(db, tenant, MessageDirection.INBOUND) == ["oi"]


async def test_a_greeting_that_never_landed_gives_the_claim_back(db, state) -> None:
    """A held claim over an unsent greeting would make the empty chat permanent.

    Driven through the entitlement gate, the one failure that reaches
    `_send_bot_reply` and returns before a single row is written.
    """
    tenant = await _seed_tenant(db)
    state.entitled = False

    await _open(tenant)

    assert await _bodies(db, tenant, MessageDirection.OUTBOUND) == []
    assert not await _claim_held(db, tenant), "the claim was kept although nothing was sent"

    # ... and the next open really does get a second chance.
    state.entitled = True
    await _open(tenant)
    assert await _bodies(db, tenant, MessageDirection.OUTBOUND) != []


async def test_an_unknown_tenant_is_a_noop_that_burns_no_key(db) -> None:
    tenant = await _seed_tenant(db)
    stranger = uuid4()

    await tasks.process_brain_message_open({"redis": None}, str(stranger), EXTERNAL_ID)

    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Patient)) == 0
        assert await session.scalar(select(func.count()).select_from(ProcessedEvent)) == 0
    assert await _bodies(db, tenant, MessageDirection.OUTBOUND) == []


async def test_each_visitor_of_a_clinic_gets_their_own_greeting(db) -> None:
    """The claim is per (tenant, visitor), never per clinic."""
    tenant = await _seed_tenant(db)

    await _open(tenant, external_id="bm-one")
    await _open(tenant, external_id="bm-two")

    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 2
    assert len(await _bodies(db, tenant, MessageDirection.OUTBOUND)) == 4


# --------------------------------------------------------------------------
# 4. WhatsApp cannot be reached from here
# --------------------------------------------------------------------------


async def test_a_whatsapp_patient_sharing_the_string_is_a_different_person(db) -> None:
    """The scope predicate is `channel`, not just the identifier.

    A clinic whose WhatsApp patient's `wa_id` happens to equal a Portal
    visitor's `external_id` must end up with two rows, and the open must
    greet the Portal one on the Portal — never the phone.
    """
    tenant = await _seed_tenant(db)
    wa_reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id="wamid.collide",
        body="oi",
    )
    assert wa_reply is not None

    await _open(tenant, external_id=WA_ID)

    async with db() as session:
        rows = (await session.execute(select(Patient))).scalars().all()
        assert {r.channel for r in rows} == {CHANNEL_WHATSAPP, CHANNEL_BRAIN_MESSAGE}
        portal = next(r for r in rows if r.channel == CHANNEL_BRAIN_MESSAGE)
        assert portal.wa_id is None
        # The WhatsApp patient's own thread was not touched by the open.
        wa_patient = next(r for r in rows if r.channel == CHANNEL_WHATSAPP)
        conversation_id = await session.scalar(
            select(Conversation.id).where(Conversation.patient_id == wa_patient.id)
        )
        wa_inbound = (
            await session.scalars(
                select(Message.body).where(
                    Message.conversation_id == conversation_id,
                    Message.direction == MessageDirection.INBOUND,
                )
            )
        ).all()
    assert list(wa_inbound) == ["oi"]


async def test_the_open_job_is_registered_on_the_worker(db) -> None:
    """A route that enqueues a job name no worker knows is a silent 202.

    The name is a STRING at the call site (`api/internal.py`), so nothing but
    this pins the two ends together.
    """
    from secretaria.workers.arq_worker import registered_function_names

    assert OPEN_JOB in registered_function_names()
