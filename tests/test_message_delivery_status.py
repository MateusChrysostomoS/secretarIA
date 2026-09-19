"""Delivery state (enviado / entregue / lido / falhou), part 1 of 3 (secretarIA).

Pins §5 of z_prompts/PROMPT_BRAIN_MESSAGE_STATUS_ENTREGA_1_SECRETARIA.md:

  * a Meta `statuses[]` receipt reaches the worker (the fast-ACK used to drop it) and
    sets `delivered_at` / `read_at` / `failed_at`+`failure_reason` on the right row,
    by `wam_id`, inside the tenant that owns the receiving number; a replay changes
    nothing and a late receipt never moves a message backwards;
  * `status_of` derives the four values from the timestamps;
  * a Brain-Message row is born delivered (`delivered_at == created_at`);
  * both read routes mark in bulk, only their own conversation and direction, and a
    WhatsApp conversation is never marked read by hand;
  * a poll with an OLD `since` returns a message whose status changed after it was
    first fetched (the cursor is `updated_at`);
  * the patient's phone number (`recipient_id`) never reaches the worker or a log.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

import json  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.models.message import status_of  # noqa: E402
from secretaria.schemas.webhook import minimal_event_payload  # noqa: E402
from secretaria.services.channel_sender import BrainMessageSender  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.message_status import meta_timestamp  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

KEY_HEADER = {"X-Internal-Api-Key": "test-internal-key"}
PNID = "1234567890"
PHONE = "5511988887777"  # the recipient_id Meta puts on every receipt
EXTERNAL_ID = "bm-status-001"
T0 = 1_726_740_000  # an epoch Meta could send
READ_URL = "/internal/brain-message/messages/read"


# --------------------------------------------------------------------------- fixtures


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


class _Queue:
    """Stands in for both the arq pool (API) and ctx["redis"] (worker)."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


class _ExplodingWhatsAppClient:
    @classmethod
    def for_tenant(cls, tenant, access_token):
        raise AssertionError("a Brain-Message path built a WhatsApp client")


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    monkeypatch.setattr(tasks, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))

    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

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
    monkeypatch.setattr(tasks, "get_entitlements", _fake_entitlements)
    monkeypatch.setattr(tasks, "WhatsAppClient", _ExplodingWhatsAppClient)


@pytest_asyncio.fixture
async def api(db, monkeypatch: pytest.MonkeyPatch):
    """The real app on the in-memory DB, a recording queue, and a hub token whose
    tenant the test picks (`api.acting["tenant"]`)."""
    from secretaria.api.hub.deps import get_current_tenant
    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    acting: dict = {}

    async def _override_session():
        async with db() as session:
            yield session

    async def _acting_tenant():
        return acting["tenant"]

    app.dependency_overrides[get_session] = _override_session
    app.dependency_overrides[get_current_tenant] = _acting_tenant
    pool = _Queue()
    app.state.arq_pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield SimpleNamespace(client=client, pool=pool, acting=acting)
    app.dependency_overrides.clear()


async def _seed_tenant(db, phone_number_id: str = PNID) -> Tenant:
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


async def _seed_conversation(
    db, tenant: Tenant, *, channel: str, external_id: str = EXTERNAL_ID
) -> Conversation:
    whatsapp = channel == "whatsapp"
    async with db() as session:
        patient = Patient(
            tenant_id=tenant.id,
            channel=channel,
            external_id=PHONE if whatsapp else external_id,
            wa_id=PHONE if whatsapp else None,
            name="Maria",
            lgpd_accepted_at=datetime.now(UTC),
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return conversation


async def _seed_message(
    db,
    conversation: Conversation,
    *,
    direction: MessageDirection,
    wam_id: str | None = None,
    minutes_ago: int = 5,
    delivered: bool = False,
) -> Message:
    """A row written in the PAST, so a later write gets a strictly later `updated_at`
    even on SQLite's one-second CURRENT_TIMESTAMP."""
    at = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    outbound = direction == MessageDirection.OUTBOUND
    async with db() as session:
        message = Message(
            conversation_id=conversation.id,
            direction=direction,
            sender=MessageSender.BOT if outbound else MessageSender.PATIENT,
            wam_id=wam_id,
            body="olá",
            created_at=at,
            updated_at=at,
            delivered_at=at if delivered else None,
        )
        session.add(message)
        await session.commit()
        await session.refresh(message)
        return message


async def _reload(db, message_id) -> Message:
    async with db() as session:
        return await session.get(Message, message_id)


def _meta_receipt(wam_id: str, status: str, ts: int, *, pnid: str = PNID, errors=None) -> dict:
    """A receipt as Meta sends it - recipient phone, billing and all."""
    entry: dict = {
        "id": wam_id,
        "status": status,
        "timestamp": str(ts),
        "recipient_id": PHONE,
        "conversation": {"id": "conv-1", "origin": {"type": "service"}},
        "pricing": {"billable": True, "category": "service", "pricing_model": "CBP"},
    }
    if errors is not None:
        entry["errors"] = errors
    value = {
        "messaging_product": "whatsapp",
        "metadata": {"display_phone_number": "5511900000000", "phone_number_id": pnid},
        "statuses": [entry],
    }
    return {
        "object": "whatsapp_business_account",
        "entry": [{"id": "waba-1", "changes": [{"field": "messages", "value": value}]}],
    }


async def _deliver_receipt(raw: dict, queue: _Queue | None = None) -> None:
    """Exactly the production path: fast-ACK reduction, then the arq job."""
    await tasks.process_webhook_event({"redis": queue}, minimal_event_payload(raw))


def _utc(value: datetime) -> datetime:
    """SQLite hands timestamps back naive; they were written as UTC."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


# ------------------------------------------------------------------ 1. status_of (pure)


@pytest.mark.parametrize(
    ("delivered", "read", "failed", "expected"),
    [
        (False, False, False, "enviado"),
        (True, False, False, "entregue"),
        (True, True, False, "lido"),
        (False, True, False, "lido"),  # read without a recorded delivery is still read
        (False, False, True, "falhou"),
        (True, False, True, "falhou"),
        (True, True, True, "falhou"),  # a failure never "progresses" afterwards
    ],
)
def test_status_of_derives_the_four_states(delivered, read, failed, expected) -> None:
    now = datetime.now(UTC)
    row = SimpleNamespace(
        delivered_at=now if delivered else None,
        read_at=now if read else None,
        failed_at=now if failed else None,
    )
    assert status_of(row) == expected


def test_meta_timestamp_is_aware_utc_and_tolerant() -> None:
    assert meta_timestamp(str(T0)) == datetime.fromtimestamp(T0, tz=UTC)
    assert meta_timestamp(T0).tzinfo is UTC
    assert meta_timestamp("not-a-number") is None
    assert meta_timestamp(None) is None


# ------------------------------------------------------- 2. WhatsApp receipts (worker)


def test_the_fast_ack_now_carries_receipts_without_the_phone() -> None:
    """The third drop point: the webhook reduced every event to messages only, so
    `statuses` never even reached the worker. It must now - minus `recipient_id`."""
    error = {
        "code": 131026,
        "title": "Message undeliverable",
        "message": f"to {PHONE}",
        "error_data": {"details": f"recipient {PHONE}"},
    }
    reduced = minimal_event_payload(_meta_receipt("wamid.A", "failed", T0, errors=[error]))
    statuses = reduced["entry"][0]["changes"][0]["value"]["statuses"]
    assert statuses == [
        {
            "id": "wamid.A",
            "status": "failed",
            "timestamp": str(T0),
            "errors": [{"code": 131026, "title": "Message undeliverable"}],
        }
    ]
    assert PHONE not in json.dumps(reduced)


async def test_delivered_then_read_then_replays_never_regress(db) -> None:
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="whatsapp")
    message = await _seed_message(
        db, conversation, direction=MessageDirection.OUTBOUND, wam_id="wamid.OUT1"
    )
    assert status_of(message) == "enviado"

    await _deliver_receipt(_meta_receipt("wamid.OUT1", "sent", T0))
    assert status_of(await _reload(db, message.id)) == "enviado"

    await _deliver_receipt(_meta_receipt("wamid.OUT1", "delivered", T0 + 5))
    row = await _reload(db, message.id)
    assert status_of(row) == "entregue"
    assert _utc(row.delivered_at) == datetime.fromtimestamp(T0 + 5, tz=UTC)

    await _deliver_receipt(_meta_receipt("wamid.OUT1", "read", T0 + 60))
    row = await _reload(db, message.id)
    assert status_of(row) == "lido"
    assert _utc(row.read_at) == datetime.fromtimestamp(T0 + 60, tz=UTC)
    snapshot = (row.delivered_at, row.read_at, row.updated_at)

    # Meta re-delivers; a late "delivered" arrives after "read": nothing moves.
    await _deliver_receipt(_meta_receipt("wamid.OUT1", "read", T0 + 60))
    await _deliver_receipt(_meta_receipt("wamid.OUT1", "delivered", T0 + 90))
    row = await _reload(db, message.id)
    assert (row.delivered_at, row.read_at, row.updated_at) == snapshot
    assert status_of(row) == "lido"
    async with db() as session:
        assert len((await session.scalars(select(Message))).all()) == 1  # nothing duplicated


async def test_read_before_delivered_fills_delivery_and_the_late_one_is_a_noop(db) -> None:
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="whatsapp")
    message = await _seed_message(
        db, conversation, direction=MessageDirection.OUTBOUND, wam_id="wamid.OUT2"
    )
    await _deliver_receipt(_meta_receipt("wamid.OUT2", "read", T0 + 60))
    await _deliver_receipt(_meta_receipt("wamid.OUT2", "delivered", T0 + 5))
    row = await _reload(db, message.id)
    assert status_of(row) == "lido"
    # Filled by "read", not overwritten by the late "delivered".
    assert _utc(row.delivered_at) == datetime.fromtimestamp(T0 + 60, tz=UTC)


async def test_failed_sets_failed_at_and_a_reason_without_pii(db) -> None:
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="whatsapp")
    message = await _seed_message(
        db, conversation, direction=MessageDirection.OUTBOUND, wam_id="wamid.OUT3"
    )
    errors = [{"code": 131026, "title": "Message undeliverable", "message": f"to {PHONE}"}]
    await _deliver_receipt(_meta_receipt("wamid.OUT3", "failed", T0, errors=errors))
    await _deliver_receipt(_meta_receipt("wamid.OUT3", "failed", T0 + 30, errors=errors))
    row = await _reload(db, message.id)
    assert status_of(row) == "falhou"
    assert row.failure_reason == "131026: Message undeliverable"
    assert _utc(row.failed_at) == datetime.fromtimestamp(T0, tz=UTC)


async def test_a_receipt_only_touches_its_own_message_in_its_own_tenant(db) -> None:
    tenant = await _seed_tenant(db)
    other = await _seed_tenant(db, phone_number_id="9999999999")
    mine = await _seed_message(
        db,
        await _seed_conversation(db, tenant, channel="whatsapp"),
        direction=MessageDirection.OUTBOUND,
        wam_id="wamid.SHARED",
    )
    neighbour = await _seed_message(
        db,
        await _seed_conversation(db, tenant, channel="brain_message"),
        direction=MessageDirection.OUTBOUND,
        wam_id="wamid.OTHER",
    )
    # The same wamid on an INBOUND row must not be touched either.
    inbound = await _seed_message(
        db,
        await _seed_conversation(db, tenant, channel="brain_message", external_id="bm-x"),
        direction=MessageDirection.INBOUND,
        wam_id="wamid.SHARED",
    )

    # A receipt arriving on ANOTHER clinic's number changes nothing here.
    await _deliver_receipt(_meta_receipt("wamid.SHARED", "read", T0, pnid=other.phone_number_id))
    assert (await _reload(db, mine.id)).read_at is None

    await _deliver_receipt(_meta_receipt("wamid.SHARED", "read", T0))
    assert status_of(await _reload(db, mine.id)) == "lido"
    assert (await _reload(db, neighbour.id)).read_at is None
    assert (await _reload(db, inbound.id)).read_at is None


async def test_a_receipt_that_overtakes_its_row_gets_one_deferred_second_look(db) -> None:
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="whatsapp")
    queue = _Queue()
    await _deliver_receipt(_meta_receipt("wamid.LATE", "delivered", T0), queue)

    assert len(queue.jobs) == 1
    name, args, kwargs = queue.jobs[0]
    assert name == "process_message_statuses"
    assert kwargs["_defer_by"] > timedelta(0)
    assert PHONE not in json.dumps(args)

    # The send's row commits meanwhile; the deferred job then lands the receipt.
    message = await _seed_message(
        db, conversation, direction=MessageDirection.OUTBOUND, wam_id="wamid.LATE"
    )
    await tasks.process_message_statuses({"redis": queue}, *args)
    assert status_of(await _reload(db, message.id)) == "entregue"
    assert len(queue.jobs) == 1  # never re-enqueues itself


def test_the_deferred_job_is_registered_on_the_worker() -> None:
    from secretaria.workers.arq_worker import registered_function_names

    assert "process_message_statuses" in registered_function_names()


async def test_no_log_line_carries_the_recipient_phone(db, caplog, capsys) -> None:
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="whatsapp")
    await _seed_message(db, conversation, direction=MessageDirection.OUTBOUND, wam_id="wamid.L")
    caplog.set_level("DEBUG")
    await _deliver_receipt(_meta_receipt("wamid.L", "delivered", T0))
    await _deliver_receipt(_meta_receipt("wamid.UNKNOWN", "read", T0), _Queue())
    captured = capsys.readouterr()
    assert PHONE not in caplog.text + captured.out + captured.err


# ------------------------------------------------ 3. Brain-Message: born delivered


async def test_brain_message_rows_are_born_delivered_both_directions(api, db) -> None:
    tenant = await _seed_tenant(db)
    response = await api.client.post(
        "/internal/brain-message/inbound",
        headers=KEY_HEADER,
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "text": "oi"},
    )
    assert response.status_code == 202, response.text
    await tasks.process_brain_message_inbound({}, *api.pool.jobs[0][1], **api.pool.jobs[0][2])

    async with db() as session:
        rows = (await session.scalars(select(Message))).all()
    # The patient's message AND the bot's replies (BrainMessageSender).
    assert {row.direction for row in rows} == {MessageDirection.INBOUND, MessageDirection.OUTBOUND}
    for row in rows:
        assert row.delivered_at is not None
        assert row.delivered_at == row.created_at
        assert status_of(row) == "entregue"


async def test_brain_message_sender_stamps_delivery_in_the_same_insert(db) -> None:
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="brain_message")
    sender = BrainMessageSender(conversation_id=conversation.id, session_factory=db)
    await sender.send_text_message(to=EXTERNAL_ID, body="Olá!")
    async with db() as session:
        row = await session.scalar(select(Message))
    assert row.delivered_at == row.created_at


async def test_whatsapp_outbound_is_not_born_delivered(db) -> None:
    """Only Meta says a WhatsApp message arrived."""
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="whatsapp")
    await tasks._record_outbound(conversation.id, "Olá", {"messages": [{"id": "wamid.N"}]})
    async with db() as session:
        row = await session.scalar(select(Message).where(Message.wam_id == "wamid.N"))
    assert row.delivered_at is None
    assert status_of(row) == "enviado"


# ------------------------------------------------------------ 4. the two read routes


async def test_patient_read_marks_only_the_clinics_messages_up_to_the_cursor(api, db) -> None:
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="brain_message")

    async def seed(direction, minutes_ago, conv=conversation):
        return await _seed_message(
            db, conv, direction=direction, minutes_ago=minutes_ago, delivered=True
        )

    older = await seed(MessageDirection.OUTBOUND, 10)
    cursor = await seed(MessageDirection.OUTBOUND, 8)
    newer = await seed(MessageDirection.OUTBOUND, 2)
    patients_own = await seed(MessageDirection.INBOUND, 9)
    other_patient = await _seed_conversation(
        db, tenant, channel="brain_message", external_id="bm-other"
    )
    elsewhere = await seed(MessageDirection.OUTBOUND, 10, conv=other_patient)

    body = {
        "tenant_id": str(tenant.id),
        "external_id": EXTERNAL_ID,
        "up_to_message_id": str(cursor.id),
    }
    response = await api.client.post(READ_URL, headers=KEY_HEADER, json=body)
    assert response.status_code == 200, response.text
    assert response.json() == {"marked": 2, "applied": True}

    assert status_of(await _reload(db, older.id)) == "lido"
    assert status_of(await _reload(db, cursor.id)) == "lido"
    assert status_of(await _reload(db, newer.id)) == "entregue"
    assert status_of(await _reload(db, patients_own.id)) == "entregue"
    assert status_of(await _reload(db, elsewhere.id)) == "entregue"

    # Idempotent: the same mark again marks nothing new.
    again = await api.client.post(READ_URL, headers=KEY_HEADER, json=body)
    assert again.json() == {"marked": 0, "applied": True}


async def test_patient_read_cannot_reach_a_whatsapp_patient_or_another_tenant(api, db) -> None:
    tenant = await _seed_tenant(db)
    other = await _seed_tenant(db, phone_number_id="9999999999")
    whatsapp = await _seed_conversation(db, tenant, channel="whatsapp")
    wa_message = await _seed_message(db, whatsapp, direction=MessageDirection.OUTBOUND)
    bm = await _seed_conversation(db, tenant, channel="brain_message")
    bm_message = await _seed_message(db, bm, direction=MessageDirection.OUTBOUND, delivered=True)

    for tenant_id, external_id in ((tenant.id, PHONE), (other.id, EXTERNAL_ID)):
        response = await api.client.post(
            READ_URL,
            headers=KEY_HEADER,
            json={
                "tenant_id": str(tenant_id),
                "external_id": external_id,
                "up_to": datetime.now(UTC).isoformat(),
            },
        )
        assert response.json() == {"marked": 0, "applied": False}
    assert (await _reload(db, wa_message.id)).read_at is None
    assert (await _reload(db, bm_message.id)).read_at is None


@pytest.mark.parametrize(
    "cursor",
    [
        {},  # none
        {"up_to_message_id": str(uuid4()), "up_to": "2026-09-19T12:00:00+00:00"},  # both
        {"up_to": "2026-09-19T12:00:00"},  # naive - refused, never guessed
    ],
)
async def test_read_mark_needs_exactly_one_aware_cursor(api, db, cursor) -> None:
    tenant = await _seed_tenant(db)
    response = await api.client.post(
        READ_URL,
        headers=KEY_HEADER,
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, **cursor},
    )
    assert response.status_code == 422


async def test_patient_read_requires_the_internal_key(api, db) -> None:
    tenant = await _seed_tenant(db)
    response = await api.client.post(
        READ_URL,
        json={
            "tenant_id": str(tenant.id),
            "external_id": EXTERNAL_ID,
            "up_to": "2026-09-19T12:00:00+00:00",
        },
    )
    assert response.status_code == 401


async def test_staff_read_marks_the_patients_messages(api, db) -> None:
    tenant = await _seed_tenant(db)
    api.acting["tenant"] = tenant
    conversation = await _seed_conversation(db, tenant, channel="brain_message")
    from_patient = await _seed_message(
        db, conversation, direction=MessageDirection.INBOUND, delivered=True
    )
    from_clinic = await _seed_message(
        db, conversation, direction=MessageDirection.OUTBOUND, delivered=True
    )

    response = await api.client.post(
        f"/tenants/me/conversations/{conversation.id}/messages/read",
        json={"up_to": datetime.now(UTC).isoformat()},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"marked": 1, "applied": True}
    assert status_of(await _reload(db, from_patient.id)) == "lido"
    assert status_of(await _reload(db, from_clinic.id)) == "entregue"

    thread = await api.client.get(f"/tenants/me/conversations/{conversation.id}/messages")
    by_id = {row["id"]: row for row in thread.json()}
    assert by_id[str(from_patient.id)]["status"] == "lido"
    assert by_id[str(from_patient.id)]["read_at"] is not None
    assert by_id[str(from_clinic.id)]["status"] == "entregue"


async def test_staff_can_never_mark_a_whatsapp_conversation_read(api, db) -> None:
    tenant = await _seed_tenant(db)
    api.acting["tenant"] = tenant
    conversation = await _seed_conversation(db, tenant, channel="whatsapp")
    message = await _seed_message(db, conversation, direction=MessageDirection.INBOUND)
    before = await _reload(db, message.id)

    response = await api.client.post(
        f"/tenants/me/conversations/{conversation.id}/messages/read",
        json={"up_to": datetime.now(UTC).isoformat()},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"marked": 0, "applied": False}
    after = await _reload(db, message.id)
    assert (after.read_at, after.delivered_at, after.updated_at) == (
        before.read_at,
        before.delivered_at,
        before.updated_at,
    )


async def test_staff_read_is_tenant_scoped(api, db) -> None:
    tenant = await _seed_tenant(db)
    api.acting["tenant"] = await _seed_tenant(db, phone_number_id="9999999999")
    conversation = await _seed_conversation(db, tenant, channel="brain_message")
    message = await _seed_message(db, conversation, direction=MessageDirection.INBOUND)
    response = await api.client.post(
        f"/tenants/me/conversations/{conversation.id}/messages/read",
        json={"up_to": datetime.now(UTC).isoformat()},
    )
    assert response.status_code == 404
    assert (await _reload(db, message.id)).read_at is None


# ------------------------------------------ 5. the poll cursor is updated_at (decision 6)


async def test_an_old_since_returns_a_message_whose_status_changed_later(api, db) -> None:
    """Fetch; the status changes; fetch again with the SAME cursor -> the row is back,
    with its new status. With the old `created_at` cursor it never came back."""
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="brain_message")
    message = await _seed_message(
        db, conversation, direction=MessageDirection.OUTBOUND, minutes_ago=5, delivered=True
    )
    url = f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages"
    params = {"tenant_id": str(tenant.id)}

    first = (await api.client.get(url, params=params, headers=KEY_HEADER)).json()["data"]
    assert [(row["id"], row["status"]) for row in first] == [(str(message.id), "entregue")]
    since = first[-1]["updated_at"]

    idle = await api.client.get(url, params={**params, "since": since}, headers=KEY_HEADER)
    assert idle.json()["data"] == []

    marked = await api.client.post(
        READ_URL,
        headers=KEY_HEADER,
        json={
            "tenant_id": str(tenant.id),
            "external_id": EXTERNAL_ID,
            "up_to_message_id": str(message.id),
        },
    )
    assert marked.json() == {"marked": 1, "applied": True}

    again = (
        await api.client.get(url, params={**params, "since": since}, headers=KEY_HEADER)
    ).json()["data"]
    assert [(row["id"], row["status"]) for row in again] == [(str(message.id), "lido")]
    assert again[0]["read_at"] is not None
    assert again[0]["updated_at"] > since


async def test_a_page_never_cuts_a_bulk_read_group(api, db) -> None:
    """One read mark gives N rows the SAME updated_at. With limit < N, a page cut inside
    that group plus a strict `>` cursor would lose the rest forever (reviewer finding)."""
    tenant = await _seed_tenant(db)
    conversation = await _seed_conversation(db, tenant, channel="brain_message")
    ids = [
        (
            await _seed_message(
                db, conversation, direction=MessageDirection.OUTBOUND,
                minutes_ago=30 - i, delivered=True,
            )
        ).id
        for i in range(5)
    ]
    url = f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages"
    since = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    mark = {"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "up_to": since}
    assert (await api.client.post(READ_URL, headers=KEY_HEADER, json=mark)).json()["marked"] == 5

    page = (
        await api.client.get(
            url, params={"tenant_id": str(tenant.id), "since": since, "limit": 2},
            headers=KEY_HEADER,
        )
    ).json()["data"]
    assert sorted(row["id"] for row in page) == sorted(str(i) for i in ids)
    assert {row["status"] for row in page} == {"lido"}
