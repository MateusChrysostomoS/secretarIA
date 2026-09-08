"""The second channel: one pipeline, two ways in.

What these tests are actually defending is a REFACTOR of the code path that
answers real patients today. So the load-bearing assertion is not that
Brain-Message works — it is that WhatsApp still behaves exactly as it did, and
that both channels now reach the same decision core.

Layout, in the order the prompt's completion criteria ask for it:
  1. pure (no DB): the sender abstraction itself.
  2. parity: the same inbound text, routed through the WhatsApp wrapper and
     through the Brain-Message wrapper, produces the same decision.
  3. unchanged: a `wa_id` inbound writes exactly the rows it wrote before.
  4. end to end: POST /internal/brain-message/inbound -> worker job -> the
     reply is readable on GET .../messages, with WhatsAppClient rigged to fail
     the test if anything so much as builds it.
  5. fail-closed auth on both new routes.

DB tests use the in-memory-sqlite-on-StaticPool pattern from
test_lgpd_consent_gate.py / test_menu_command.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from dataclasses import fields  # noqa: E402
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
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.services.channel_sender import (  # noqa: E402
    CHANNEL_BRAIN_MESSAGE,
    CHANNEL_WHATSAPP,
    BrainMessageSender,
    ChannelSender,
    interactive_history_body,
    sender_persists_outbound,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.whatsapp import WhatsAppClient  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"
WA_ID = "5511988887777"
EXTERNAL_ID = "bm-session-abc123"
GOOD_KEY = "test-internal-key"


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
    """Any use at all fails the test.

    Installed for every Brain-Message test. It is the strongest available
    statement of the property that matters: a console patient's turn must not
    touch the Graph API, and `for_tenant` is the only door into it from the
    worker (`_tenant_client`).
    """

    @classmethod
    def for_tenant(cls, tenant, access_token):
        raise AssertionError("the Brain-Message path built a WhatsApp client")

    @classmethod
    def for_dev_scaffold(cls, settings=None):
        raise AssertionError("the Brain-Message path built a WhatsApp client")


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

    async def _fake_token(session, tenant_id):
        return "decrypted-waba-token"

    async def _fake_entitlements(tenant_id, redis):
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

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _fake_resolve)
    monkeypatch.setattr(tasks, "get_waba_token", _fake_token)
    monkeypatch.setattr(tasks, "get_entitlements", _fake_entitlements)
    yield


async def _seed_tenant(db, phone_number_id: str = PHONE_NUMBER_ID) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            # Unique per tenant: `tenants.phone_number_id` is a real WhatsApp
            # number and the column says so. A second clinic in one test needs
            # its own, even when the test is about a channel that has none.
            phone_number_id=phone_number_id,
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


# --------------------------------------------------------------------------
# 1. Pure layer — the sender abstraction, no DB, no session
# --------------------------------------------------------------------------


def test_whatsapp_client_satisfies_the_sender_protocol() -> None:
    """The refactor's core claim: the existing client IS the new interface.

    If this ever fails, ~60 send call sites in workers/tasks.py stopped being
    channel-blind and the abstraction has quietly become a second thing to keep
    in sync.
    """
    assert isinstance(WhatsAppClient(phone_number_id="1", access_token="t"), ChannelSender)


def test_brain_message_sender_satisfies_the_sender_protocol() -> None:
    sender = BrainMessageSender(conversation_id=uuid4(), session_factory=None)
    assert isinstance(sender, ChannelSender)


def test_whatsapp_does_not_persist_its_own_sends_but_brain_message_does() -> None:
    """The one asymmetry the two channels genuinely have."""
    assert WhatsAppClient(phone_number_id="1", access_token="t").persists_outbound is False
    brain = BrainMessageSender(conversation_id=uuid4(), session_factory=None)
    assert brain.persists_outbound is True


def test_a_sender_that_never_heard_of_the_flag_defaults_to_caller_persists() -> None:
    """The default is the pre-refactor behaviour, so old stand-ins keep working."""

    class _OldStyleClient:
        pass

    assert sender_persists_outbound(_OldStyleClient()) is False


def test_interactive_cards_keep_their_options_in_history() -> None:
    """A card that collapsed to its bare body would blind the next agent turn."""
    assert interactive_history_body("Escolha:", ["Agendar", "Outro"]) == (
        "Escolha:\n(opções: Agendar, Outro)"
    )
    assert interactive_history_body("Olá", []) == "Olá"


# --------------------------------------------------------------------------
# 2. Parity — one core, two ways in
# --------------------------------------------------------------------------


async def _wa_turn(tenant: Tenant, body: str, wam_id: str):
    return await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id=wam_id,
        body=body,
    )


async def _bm_turn(tenant: Tenant, body: str, external_id: str = EXTERNAL_ID):
    return await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id,
        external_id=external_id,
        text=body,
        patient_name="Maria",
    )


async def test_both_wrappers_reach_the_same_decision(db) -> None:
    """The same first contact decides the same thing on either channel.

    Every field of the returned `_ReplyContext` must match except the two that
    ARE the channel: `channel` itself and `patient_ref` (the address to reply
    to). `conversation_id` differs because they are different patients, which is
    the point — same decision, different people.
    """
    tenant = await _seed_tenant(db)

    wa = await _wa_turn(tenant, "oi", "wamid.parity")
    bm = await _bm_turn(tenant, "oi")

    assert wa is not None and bm is not None
    differing = {"channel", "patient_ref", "conversation_id"}
    compared = 0
    for f in fields(tasks._ReplyContext):
        if f.name in differing:
            continue
        assert getattr(wa, f.name) == getattr(bm, f.name), f.name
        compared += 1
    assert compared >= 8, "parity check got vacuous — too few fields compared"

    assert wa.channel == CHANNEL_WHATSAPP
    assert wa.patient_ref == WA_ID
    assert bm.channel == CHANNEL_BRAIN_MESSAGE
    assert bm.patient_ref == EXTERNAL_ID


async def test_parity_holds_through_the_menu_branch(db) -> None:
    """Not just the greeting: a mid-conversation `/menu` decides identically."""
    tenant = await _seed_tenant(db)
    from secretaria.services.greeting_template import CONSENT_BUTTON_LABEL

    for turn in (("oi", "w1"), (CONSENT_BUTTON_LABEL, "w2"), ("/menu", "w3")):
        wa = await _wa_turn(tenant, turn[0], turn[1])
    for body in ("oi", CONSENT_BUTTON_LABEL, "/menu"):
        bm = await _bm_turn(tenant, body)

    assert wa is not None and bm is not None
    assert wa.menu_requested is True
    assert bm.menu_requested is True
    assert wa.greeting_override == bm.greeting_override
    assert wa.greeting_buttons == bm.greeting_buttons


# --------------------------------------------------------------------------
# 3. Unchanged — the WhatsApp path writes exactly what it wrote before
# --------------------------------------------------------------------------


async def test_wa_id_inbound_writes_exactly_as_before(db) -> None:
    """Row-for-row, column-for-column, after the extraction.

    The patient keyed on wa_id, `channel="whatsapp"`, `external_id` mirroring
    wa_id, one conversation, one inbound message carrying the Meta id, one
    first-contact consent event.
    """
    tenant = await _seed_tenant(db)

    await _wa_turn(tenant, "oi", "wamid.unchanged")

    async with db() as session:
        patient = await session.scalar(select(Patient).where(Patient.wa_id == WA_ID))
        assert patient is not None
        assert patient.tenant_id == tenant.id
        assert patient.channel == CHANNEL_WHATSAPP
        assert patient.external_id == WA_ID
        assert patient.name == "Maria"

        assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
        rows = (await session.execute(select(Message))).scalars().all()
        assert len(rows) == 1
        assert rows[0].direction == MessageDirection.INBOUND
        assert rows[0].sender == MessageSender.PATIENT
        assert rows[0].wam_id == "wamid.unchanged"
        assert rows[0].body == "oi"

        events = (await session.execute(select(ConsentEvent))).scalars().all()
        assert [(e.wa_id, e.kind) for e in events] == [(WA_ID, "first_contact_service")]


async def test_brain_message_inbound_creates_a_phoneless_patient(db) -> None:
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")

    async with db() as session:
        patient = await session.scalar(
            select(Patient).where(Patient.channel == CHANNEL_BRAIN_MESSAGE)
        )
        assert patient is not None
        assert patient.external_id == EXTERNAL_ID
        # The whole reason `wa_id` was made nullable.
        assert patient.wa_id is None
        row = await session.scalar(select(Message).where(Message.body == "oi"))
        # No Meta id exists for a message Meta never carried.
        assert row is not None and row.wam_id is None


async def test_a_second_brain_message_turn_reuses_the_same_patient(db) -> None:
    tenant = await _seed_tenant(db)

    await _bm_turn(tenant, "oi")
    await _bm_turn(tenant, "de novo")

    async with db() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(Patient)
            .where(Patient.channel == CHANNEL_BRAIN_MESSAGE)
        )
        assert count == 1
        # And exactly one consent event, never one per message.
        assert await session.scalar(select(func.count()).select_from(ConsentEvent)) == 1


async def test_the_two_channels_do_not_collide_on_a_shared_identifier(db) -> None:
    """A Brain-Message external_id that happens to equal someone's wa_id.

    Two different people. If the channel predicate were ever dropped from the
    patient lookup this test is what notices.
    """
    tenant = await _seed_tenant(db)

    await _wa_turn(tenant, "oi", "wamid.collide")
    await _bm_turn(tenant, "oi", external_id=WA_ID)

    async with db() as session:
        rows = (await session.execute(select(Patient))).scalars().all()
        assert len(rows) == 2
        assert {r.channel for r in rows} == {CHANNEL_WHATSAPP, CHANNEL_BRAIN_MESSAGE}


# --------------------------------------------------------------------------
# 4. End to end through the new path, with the Graph API rigged to explode
# --------------------------------------------------------------------------


class _FakeArqPool:
    """Records what the endpoint enqueued, so the test can run it as the worker would."""

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
    # Nothing on this path may reach Meta.
    monkeypatch.setattr(tasks, "WhatsAppClient", _ExplodingWhatsAppClient)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac, pool
    app.dependency_overrides.clear()


async def test_inbound_to_readable_reply_without_touching_the_graph_api(api, db) -> None:
    """The whole new channel, end to end.

    POST -> 202 + a queued job -> run the job as the worker would -> the bot's
    reply is readable on the messages endpoint. `WhatsAppClient` is rigged to
    raise on any use, so a single Graph API attempt fails this test.
    """
    client, pool = api
    tenant = await _seed_tenant(db)

    response = await client.post(
        "/internal/brain-message/inbound",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "text": "oi"},
    )
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "queued"}

    assert len(pool.jobs) == 1
    name, args, kwargs = pool.jobs[0]
    assert name == "process_brain_message_inbound"
    # Run it exactly as arq would.
    await tasks.process_brain_message_inbound({}, *args, **kwargs)

    read = await client.get(
        f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages",
        params={"tenant_id": str(tenant.id)},
        headers={"X-Internal-Api-Key": GOOD_KEY},
    )
    assert read.status_code == 200, read.text
    rows = read.json()["data"]
    from secretaria.services.greeting_template import (
        CONSENT_BUTTON_LABEL,
        LGPD_CONSENT_MESSAGE,
        render_greeting,
    )

    # Asserted as a SET, not a sequence, on purpose: `Message.created_at` is a
    # server default with one-second granularity on SQLite, so three rows
    # written inside the same tick tie, and the `id` tiebreak is a random
    # UUID. Ordering between same-tick rows is genuinely undefined today — a
    # real limitation of the read endpoint, recorded in
    # docs/CHECKPOINT_brain_message_pipeline.md, not something to hide behind
    # a flaky index assertion.
    assert sorted(r["direction"] for r in rows) == ["inbound", "outbound", "outbound"]
    assert {(r["sender"], r["body"]) for r in rows} == {
        ("patient", "oi"),
        ("bot", render_greeting(tenant.clinic_name, tenant.clinic_description)),
        ("bot", interactive_history_body(LGPD_CONSENT_MESSAGE, [CONSENT_BUTTON_LABEL])),
    }


async def test_the_reply_is_recorded_exactly_once(api, db) -> None:
    """The sender writes the row, so the caller must not write a second one."""
    client, pool = api
    tenant = await _seed_tenant(db)

    await client.post(
        "/internal/brain-message/inbound",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "text": "oi"},
    )
    await tasks.process_brain_message_inbound({}, *pool.jobs[0][1], **pool.jobs[0][2])

    async with db() as session:
        bodies = (
            await session.execute(
                select(Message.body).where(Message.direction == MessageDirection.OUTBOUND)
            )
        ).scalars().all()
        assert len(bodies) == len(set(bodies)), bodies


async def test_read_is_scoped_to_the_tenant(api, db) -> None:
    """Right external_id, wrong tenant => nothing. Never another clinic's rows."""
    client, pool = api
    tenant = await _seed_tenant(db)
    other = await _seed_tenant(db, phone_number_id="9999999999")

    await client.post(
        "/internal/brain-message/inbound",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "text": "segredo"},
    )
    await tasks.process_brain_message_inbound({}, *pool.jobs[0][1], **pool.jobs[0][2])

    leak = await client.get(
        f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages",
        params={"tenant_id": str(other.id)},
        headers={"X-Internal-Api-Key": GOOD_KEY},
    )
    assert leak.status_code == 200
    assert leak.json()["data"] == []


async def test_read_is_scoped_to_the_patient(api, db) -> None:
    """Right tenant, another patient's external_id => nothing."""
    client, pool = api
    tenant = await _seed_tenant(db)

    await client.post(
        "/internal/brain-message/inbound",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "text": "segredo"},
    )
    await tasks.process_brain_message_inbound({}, *pool.jobs[0][1], **pool.jobs[0][2])

    other = await client.get(
        "/internal/brain-message/conversations/bm-someone-else/messages",
        params={"tenant_id": str(tenant.id)},
        headers={"X-Internal-Api-Key": GOOD_KEY},
    )
    assert other.json()["data"] == []


async def test_read_requires_a_tenant(api, db) -> None:
    """`tenant_id` is required, not an optional filter: it is the outer scope."""
    client, _ = api
    response = await client.get(
        f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages",
        headers={"X-Internal-Api-Key": GOOD_KEY},
    )
    assert response.status_code == 422


async def test_since_advances_the_poll_cursor(api, db) -> None:
    client, pool = api
    tenant = await _seed_tenant(db)
    await client.post(
        "/internal/brain-message/inbound",
        headers={"X-Internal-Api-Key": GOOD_KEY},
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "text": "oi"},
    )
    await tasks.process_brain_message_inbound({}, *pool.jobs[0][1], **pool.jobs[0][2])

    params = {"tenant_id": str(tenant.id)}
    everything = (
        await client.get(
            f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages",
            params=params,
            headers={"X-Internal-Api-Key": GOOD_KEY},
        )
    ).json()["data"]
    after = (
        await client.get(
            f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages",
            params={**params, "since": everything[-1]["created_at"]},
            headers={"X-Internal-Api-Key": GOOD_KEY},
        )
    ).json()["data"]
    # Everything shares the same second on SQLite, so `since` the last row's
    # timestamp excludes them all - which is exactly what "strictly after"
    # promises. On Postgres the three transactions get distinct timestamps.
    assert after == []
    assert len(everything) == 3


# --------------------------------------------------------------------------
# 5. Fail-closed auth on both new routes
# --------------------------------------------------------------------------

async def _call(client, method, path, **kwargs):
    """Hit either route. Only POST takes a body; `get(json=...)` is a TypeError.

    The BODY is deliberately absent/empty: auth must reject before any payload
    or query-parameter validation runs, so a 422 here would itself be a finding
    (the guard would be sitting behind the parser).
    """
    if method == "post":
        return await client.post(path, json={}, **kwargs)
    return await client.get(path, **kwargs)


_ROUTES = [
    ("post", "/internal/brain-message/inbound"),
    ("get", f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages"),
]


@pytest.mark.parametrize("method,path", _ROUTES)
async def test_no_key_is_unauthorized(api, method, path) -> None:
    client, _ = api
    response = await _call(client, method, path)
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("method,path", _ROUTES)
async def test_wrong_key_is_unauthorized(api, method, path) -> None:
    client, _ = api
    response = await _call(client, method, path, headers={"X-Internal-Api-Key": "wrong-key"})
    assert response.status_code == 401, response.text


@pytest.mark.parametrize("method,path", _ROUTES)
async def test_unconfigured_server_key_is_forbidden(
    api, monkeypatch: pytest.MonkeyPatch, method, path
) -> None:
    """No server-side key => locked, not open. Never accept-all."""
    from secretaria.config import get_settings

    client, _ = api
    monkeypatch.setenv("INTERNAL_API_KEY", "")
    get_settings.cache_clear()
    try:
        response = await _call(client, method, path, headers={"X-Internal-Api-Key": "anything"})
        assert response.status_code == 403, response.text
    finally:
        monkeypatch.setenv("INTERNAL_API_KEY", GOOD_KEY)
        get_settings.cache_clear()


async def test_auth_runs_before_the_queue(api, db) -> None:
    """An unauthenticated POST enqueues nothing at all."""
    client, pool = api
    tenant = await _seed_tenant(db)
    await client.post(
        "/internal/brain-message/inbound",
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "text": "oi"},
    )
    assert pool.jobs == []
