"""A visit merged into an existing account is thrown away (LACUNAS L2)."""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest_asyncio  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

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
from secretaria.services.visit_merge import discard_visit  # noqa: E402

VISIT = "visit-aaaa"
ACCOUNT = "account-bbbb"
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


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


async def _tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4().int)[:20],
            is_active=True,
            clinic_description="x",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        return tenant


async def _portal_patient(db, tenant, external_id, *, messages=2, channel="brain_message"):
    async with db() as session:
        patient = Patient(
            tenant_id=tenant.id,
            channel=channel,
            external_id=external_id,
            wa_id=None if channel == "brain_message" else "5511900000000",
            name="Maria",
            lgpd_accepted_at=NOW,
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        for i in range(messages):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.OUTBOUND,
                    sender=MessageSender.BOT,
                    body=f"m{i}",
                    created_at=NOW + timedelta(minutes=i),
                    updated_at=NOW + timedelta(minutes=i),
                )
            )
        session.add(
            ConsentEvent(
                tenant_id=tenant.id,
                wa_id=external_id,
                kind="first_contact_service",
                legal_basis="test",
            )
        )
        await session.commit()
        return patient, conversation


async def _count(db, model) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


async def test_discard_removes_the_visit_and_keeps_the_audit_trail(db) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, messages=3)
    await _portal_patient(db, tenant, ACCOUNT, messages=5)
    async with db() as session:
        result = await discard_visit(session, tenant.id, VISIT)
        await session.commit()
    assert result.status == "discarded"
    assert result.messages == 3
    assert await _count(db, Patient) == 1  # only the account's patient is left
    assert await _count(db, Conversation) == 1
    assert await _count(db, Message) == 5  # the account's history is untouched
    assert await _count(db, ConsentEvent) == 2  # audit trail is never deleted


async def test_discard_of_an_unknown_visit_is_a_quiet_no_op(db) -> None:
    tenant = await _tenant(db)
    async with db() as session:
        result = await discard_visit(session, tenant.id, "nobody")
    assert result.status == "absent"


async def test_discard_never_reaches_a_whatsapp_patient_with_the_same_string(db) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, channel="whatsapp")
    async with db() as session:
        result = await discard_visit(session, tenant.id, VISIT)
    assert result.status == "absent"
    assert await _count(db, Patient) == 1


async def test_discard_is_scoped_to_the_tenant(db) -> None:
    mine = await _tenant(db)
    other = await _tenant(db)
    await _portal_patient(db, other, VISIT)
    async with db() as session:
        result = await discard_visit(session, mine.id, VISIT)
    assert result.status == "absent"
    assert await _count(db, Patient) == 1


from types import SimpleNamespace  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers import tasks  # noqa: E402
from secretaria.workers.shared.jobs import _claim_event as _real_claim_event  # noqa: E402
from tests._patching import workers_ns  # noqa: E402

KEY = {"X-Internal-Api-Key": "test-internal-key"}
MERGE_URL = "/internal/brain-message/visits/merge"


class _Queue:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.jobs.append((name, args, kwargs))


@pytest_asyncio.fixture
async def api(db):
    from secretaria.config import get_settings
    from secretaria.core.database import get_session

    get_settings.cache_clear()
    from secretaria.main import app

    async def _override_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _override_session
    pool = _Queue()
    app.state.arq_pool = pool
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield SimpleNamespace(client=client, pool=pool)
    app.dependency_overrides.clear()


async def test_merge_route_requires_the_internal_key(api) -> None:
    body = {"tenant_id": str(uuid4()), "visit_external_id": VISIT, "into_external_id": ACCOUNT}
    assert (await api.client.post(MERGE_URL, json=body)).status_code in (401, 403)


async def test_merge_route_enqueues_one_job(api) -> None:
    tenant_id = str(uuid4())
    body = {"tenant_id": tenant_id, "visit_external_id": VISIT, "into_external_id": ACCOUNT}
    resp = await api.client.post(MERGE_URL, json=body, headers=KEY)
    assert resp.status_code == 202
    assert resp.json() == {"status": "queued"}
    assert api.pool.jobs == [("merge_brain_message_visit", (tenant_id, VISIT, ACCOUNT), {})]


async def test_merge_route_refuses_merging_a_handle_into_itself(api) -> None:
    body = {"tenant_id": str(uuid4()), "visit_external_id": VISIT, "into_external_id": VISIT}
    resp = await api.client.post(MERGE_URL, json=body, headers=KEY)
    assert resp.status_code == 422
    assert api.pool.jobs == []


@pytest.fixture
def _worker(monkeypatch, db):
    monkeypatch.setattr(workers_ns, "async_session_factory", db)

    async def _entitled(tenant_id, redis):
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

    async def _no_ledger(key):
        return True

    monkeypatch.setattr(workers_ns, "get_entitlements", _entitled)
    monkeypatch.setattr(workers_ns, "_claim_event", _no_ledger)


async def test_job_discards_the_visit_and_shows_the_menu_in_the_account(db, _worker) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, messages=3)
    _, account_conv = await _portal_patient(db, tenant, ACCOUNT, messages=5)

    await tasks.merge_brain_message_visit({"redis": None}, str(tenant.id), VISIT, ACCOUNT)

    async with db() as session:
        visit = await session.scalar(select(Patient).where(Patient.external_id == VISIT))
        assert visit is None
        rows = list(
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == account_conv.id)
                .order_by(Message.created_at)
            )
        )
    # The old history is intact and exactly ONE new bot row (the menu) was added - no greeting.
    assert len(rows) == 6
    assert rows[-1].sender == MessageSender.BOT
    assert rows[-1].interactive is not None


async def test_job_opens_the_account_when_it_never_had_a_conversation(
    db, _worker, monkeypatch
) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, messages=3)
    opened: list[tuple] = []

    async def _open(ctx, tenant_id, external_id, patient_name=None):
        opened.append((tenant_id, external_id))
        await _portal_patient(db, tenant, external_id, messages=1)

    monkeypatch.setattr(workers_ns, "process_brain_message_open", _open)
    await tasks.merge_brain_message_visit({"redis": None}, str(tenant.id), VISIT, ACCOUNT)
    assert opened == [(str(tenant.id), ACCOUNT)]
    async with db() as session:
        assert await session.scalar(select(Patient).where(Patient.external_id == VISIT)) is None


async def test_job_refuses_self_merge_even_when_called_directly(db, _worker):
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, ACCOUNT, messages=5)
    await tasks.merge_brain_message_visit({}, str(tenant.id), ACCOUNT, ACCOUNT)
    assert await _count(db, Patient) == 1
    assert await _count(db, Message) == 5


async def test_job_is_idempotent_with_the_real_ledger(db, _worker, monkeypatch):
    from secretaria.models import ProcessedEvent

    monkeypatch.setattr(workers_ns, "_claim_event", _real_claim_event)
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT)
    await _portal_patient(db, tenant, ACCOUNT, messages=5)
    for _ in range(2):
        await tasks.merge_brain_message_visit({}, str(tenant.id), VISIT, ACCOUNT)
    assert await _count(db, Message) == 6
    assert await _count(db, ProcessedEvent) == 1


async def test_job_discards_without_menu_when_entitlement_unknown(db, _worker, monkeypatch):
    async def unavailable(tenant_id, redis):
        return None

    monkeypatch.setattr(workers_ns, "get_entitlements", unavailable)
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT)
    await _portal_patient(db, tenant, ACCOUNT, messages=5)
    await tasks.merge_brain_message_visit({}, str(tenant.id), VISIT, ACCOUNT)
    assert await _count(db, Patient) == 1
    assert await _count(db, Message) == 5


async def test_discard_deletes_children_and_preserves_processed_events(db):
    from secretaria.models import ProcessedEvent
    from secretaria.models.booking_hold import BookingHold
    from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap

    tenant = await _tenant(db)
    visit, conv = await _portal_patient(db, tenant, VISIT)
    account, kept = await _portal_patient(db, tenant, ACCOUNT)
    async with db() as session:
        for patient, conversation in ((visit, conv), (account, kept)):
            session.add(ConversationPiiTokenMap(conversation_id=conversation.id, tokens={}))
            session.add(
                BookingHold(
                    tenant_id=tenant.id,
                    patient_id=patient.id,
                    conversation_id=conversation.id,
                    start_at=NOW,
                    end_at=NOW + timedelta(hours=1),
                    expires_at=NOW + timedelta(minutes=10),
                )
            )
        session.add(ProcessedEvent(event_id="audit-visit"))
        await session.commit()
    async with db() as session:
        await discard_visit(session, tenant.id, VISIT)
        await session.commit()
    assert await _count(db, ConversationPiiTokenMap) == 1
    assert await _count(db, BookingHold) == 1
    assert await _count(db, ProcessedEvent) == 1


async def test_discard_transaction_belongs_to_the_caller(db):
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT)
    async with db() as session:
        await discard_visit(session, tenant.id, VISIT)
        await session.rollback()
    assert await _count(db, Patient) == 1
    assert await _count(db, Message) == 2


@pytest.mark.parametrize("stage", ["claim", "discard", "menu", "open"])
async def test_failed_merge_releases_claim_and_retries(db, _worker, monkeypatch, stage):
    from arq import Retry

    from secretaria.models import ProcessedEvent
    from secretaria.workers.portal import merge as merge_job

    monkeypatch.setattr(workers_ns, "_claim_event", _real_claim_event)
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT)
    if stage != "open":
        await _portal_patient(db, tenant, ACCOUNT, messages=5)
    target = {
        "claim": "_claim_event",
        "discard": "discard_visit",
        "menu": "_handle_show_main_menu",
        "open": "process_brain_message_open",
    }[stage]
    real = getattr(merge_job, target)
    calls = 0

    async def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic transient failure")
        if stage == "open":
            await _portal_patient(db, tenant, ACCOUNT, messages=1)
            return
        return await real(*args, **kwargs)

    monkeypatch.setattr(merge_job, target, fail_once)
    with pytest.raises(Retry):
        await tasks.merge_brain_message_visit({}, str(tenant.id), VISIT, ACCOUNT)
    assert await _count(db, ProcessedEvent) == 0
    assert await _count(db, Patient) == (
        2 if stage in ("claim", "discard") else (1 if stage == "menu" else 0)
    )
    await tasks.merge_brain_message_visit({}, str(tenant.id), VISIT, ACCOUNT)
    assert await _count(db, Patient) == 1
    assert await _count(db, Message) == (1 if stage == "open" else 6)
    assert await _count(db, ProcessedEvent) == 1


async def test_unsent_menu_is_retryable(db, _worker, monkeypatch):
    from arq import Retry

    from secretaria.models import ProcessedEvent
    from secretaria.workers.portal import merge as merge_job

    monkeypatch.setattr(workers_ns, "_claim_event", _real_claim_event)
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT)
    await _portal_patient(db, tenant, ACCOUNT, messages=5)

    async def unsent(*args, **kwargs):
        return False

    monkeypatch.setattr(merge_job, "_handle_show_main_menu", unsent)
    with pytest.raises(Retry):
        await tasks.merge_brain_message_visit({}, str(tenant.id), VISIT, ACCOUNT)
    assert await _count(db, Patient) == 1
    assert await _count(db, Message) == 5
    assert await _count(db, ProcessedEvent) == 0


async def test_unsent_open_is_retryable(db, _worker, monkeypatch):
    from arq import Retry

    from secretaria.models import ProcessedEvent
    from secretaria.workers.portal import merge as merge_job

    monkeypatch.setattr(workers_ns, "_claim_event", _real_claim_event)
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT)

    async def unsent(*args, **kwargs):
        return None

    monkeypatch.setattr(merge_job, "process_brain_message_open", unsent)
    with pytest.raises(Retry):
        await tasks.merge_brain_message_visit({}, str(tenant.id), VISIT, ACCOUNT)
    assert await _count(db, Patient) == 0
    assert await _count(db, ProcessedEvent) == 0
