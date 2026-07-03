"""Tests for api/hub/conversations.py — per-conversation manual handover.

Uses the `client` fixture from conftest.py (ASGITransport, no real DB) with
`get_session` overridden to a real in-memory sqlite DB and `get_current_tenant`
overridden to a canned Tenant — the same override-the-FastAPI-dependency
pattern as test_hub_professionals.py, with a REAL session since these
endpoints actually read/write rows.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import Conversation, HandoverState, Message, Patient, Tenant  # noqa: E402
from secretaria.models.message import MessageDirection, MessageSender  # noqa: E402

ENDPOINT = "/tenants/me/conversations"


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
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
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

    async def _fake_get_current_tenant():
        return tenant

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


async def _seed_patient(db, tenant: Tenant, **overrides) -> Patient:
    fields = dict(tenant_id=tenant.id, wa_id="5511999999999", name="Ana Paciente")
    fields.update(overrides)
    async with db() as session:
        patient = Patient(**fields)
        session.add(patient)
        await session.commit()
        await session.refresh(patient)
        return patient


async def _seed_conversation(db, tenant: Tenant, patient: Patient, **overrides) -> Conversation:
    fields = dict(
        tenant_id=tenant.id, patient_id=patient.id, handover_state=HandoverState.BOT_ACTIVE
    )
    fields.update(overrides)
    async with db() as session:
        conv = Conversation(**fields)
        session.add(conv)
        await session.commit()
        await session.refresh(conv)
        return conv


async def _seed_message(
    db, conversation: Conversation, created_at: datetime, **overrides
) -> Message:
    fields = dict(
        conversation_id=conversation.id,
        direction=MessageDirection.INBOUND,
        sender=MessageSender.PATIENT,
        body="oi",
        created_at=created_at,
    )
    fields.update(overrides)
    async with db() as session:
        msg = Message(**fields)
        session.add(msg)
        await session.commit()
        await session.refresh(msg)
        return msg


async def _get_conversation_row(db, conversation_id) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, conversation_id)


# --------------------------------------------------------------------------
# GET — list
# --------------------------------------------------------------------------


async def test_list_returns_only_own_tenant_conversations_with_correct_shape(
    client: AsyncClient, db, tenant
) -> None:
    now = datetime.now(UTC)

    # Conversation with 2 messages -> last_message_at is the newest one.
    patient_a = await _seed_patient(db, tenant, wa_id="5511111111111", name="Paciente A")
    conv_a = await _seed_conversation(
        db, tenant, patient_a, handover_state=HandoverState.BOT_ACTIVE
    )
    await _seed_message(db, conv_a, created_at=now - timedelta(minutes=10))
    await _seed_message(db, conv_a, created_at=now - timedelta(minutes=1))

    # Conversation with no messages -> last_message_at is None.
    patient_b = await _seed_patient(db, tenant, wa_id="5511222222222", name=None)
    conv_b = await _seed_conversation(
        db, tenant, patient_b, handover_state=HandoverState.HUMAN_ACTIVE
    )

    # A conversation belonging to a DIFFERENT tenant must never show up.
    other_tenant = Tenant(id=uuid4(), clinic_name="Other Clinic", phone_number_id=str(uuid4())[:12])
    async with db() as session:
        session.add(other_tenant)
        await session.commit()
    patient_c = await _seed_patient(db, other_tenant, wa_id="5511333333333", name="Paciente C")
    await _seed_conversation(db, other_tenant, patient_c)

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2

    # Newest-first ordering, None (no messages) last.
    assert body[0]["id"] == str(conv_a.id)
    assert body[1]["id"] == str(conv_b.id)

    assert body[0]["patient_wa_id"] == "5511111111111"
    assert body[0]["patient_name"] == "Paciente A"
    assert body[0]["handover_state"] == "BOT_ACTIVE"
    assert body[0]["last_message_at"] is not None

    assert body[1]["patient_wa_id"] == "5511222222222"
    assert body[1]["patient_name"] is None
    assert body[1]["handover_state"] == "HUMAN_ACTIVE"
    assert body[1]["last_message_at"] is None


async def test_list_is_empty_for_a_tenant_with_no_conversations(client: AsyncClient) -> None:
    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    assert response.json() == []


# --------------------------------------------------------------------------
# POST /handover — success both directions
# --------------------------------------------------------------------------


async def test_handover_bot_to_human_stamps_last_human_message_at(
    client: AsyncClient, db, tenant
) -> None:
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient, handover_state=HandoverState.BOT_ACTIVE)
    assert conv.last_human_message_at is None

    response = await client.post(f"{ENDPOINT}/{conv.id}/handover", json={"state": "HUMAN_ACTIVE"})
    assert response.status_code == 200
    body = response.json()
    assert body["handover_state"] == "HUMAN_ACTIVE"

    persisted = await _get_conversation_row(db, conv.id)
    assert persisted.handover_state == HandoverState.HUMAN_ACTIVE
    assert persisted.last_human_message_at is not None


async def test_handover_human_to_bot(client: AsyncClient, db, tenant) -> None:
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient, handover_state=HandoverState.HUMAN_ACTIVE)

    response = await client.post(f"{ENDPOINT}/{conv.id}/handover", json={"state": "BOT_ACTIVE"})
    assert response.status_code == 200
    body = response.json()
    assert body["handover_state"] == "BOT_ACTIVE"

    persisted = await _get_conversation_row(db, conv.id)
    assert persisted.handover_state == HandoverState.BOT_ACTIVE


async def test_handover_is_idempotent(client: AsyncClient, db, tenant) -> None:
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient, handover_state=HandoverState.BOT_ACTIVE)

    response = await client.post(f"{ENDPOINT}/{conv.id}/handover", json={"state": "BOT_ACTIVE"})
    assert response.status_code == 200
    assert response.json()["handover_state"] == "BOT_ACTIVE"


# --------------------------------------------------------------------------
# POST /handover — 404s, same message regardless of reason
# --------------------------------------------------------------------------


async def test_handover_for_other_tenant_conversation_is_404(
    client: AsyncClient, db, tenant
) -> None:
    other_tenant = Tenant(id=uuid4(), clinic_name="Other Clinic", phone_number_id=str(uuid4())[:12])
    async with db() as session:
        session.add(other_tenant)
        await session.commit()
    patient = await _seed_patient(db, other_tenant)
    conv = await _seed_conversation(db, other_tenant, patient)

    response = await client.post(f"{ENDPOINT}/{conv.id}/handover", json={"state": "HUMAN_ACTIVE"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation not found"


async def test_handover_for_random_uuid_is_404(client: AsyncClient) -> None:
    response = await client.post(f"{ENDPOINT}/{uuid4()}/handover", json={"state": "HUMAN_ACTIVE"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation not found"


async def test_handover_for_malformed_id_is_404(client: AsyncClient) -> None:
    response = await client.post(f"{ENDPOINT}/not-a-uuid/handover", json={"state": "HUMAN_ACTIVE"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation not found"


# --------------------------------------------------------------------------
# 401 — invalid/missing token (get_current_tenant is NOT overridden here;
# instead verify_subscription_token is monkeypatched to fail, same idiom as
# test_hub_deps.py).
# --------------------------------------------------------------------------


async def test_list_without_token_is_401(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.api.hub import deps as hub_deps
    from secretaria.main import app

    app.dependency_overrides.pop(get_current_tenant, None)

    async def _fake_verify(token: str):
        return None

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake_verify)

    response = await client.get(ENDPOINT)
    assert response.status_code == 401


async def test_post_handover_without_token_is_401(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.api.hub import deps as hub_deps
    from secretaria.main import app

    app.dependency_overrides.pop(get_current_tenant, None)

    async def _fake_verify(token: str):
        return None

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake_verify)

    response = await client.post(f"{ENDPOINT}/{uuid4()}/handover", json={"state": "HUMAN_ACTIVE"})
    assert response.status_code == 401


# --------------------------------------------------------------------------
# 422 — invalid state value
# --------------------------------------------------------------------------


async def test_handover_invalid_state_value_is_422(client: AsyncClient, db, tenant) -> None:
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient)

    response = await client.post(f"{ENDPOINT}/{conv.id}/handover", json={"state": "NOT_A_STATE"})
    assert response.status_code == 422


async def test_handover_missing_state_is_422(client: AsyncClient, db, tenant) -> None:
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient)

    response = await client.post(f"{ENDPOINT}/{conv.id}/handover", json={})
    assert response.status_code == 422
