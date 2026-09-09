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

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import AsyncClient  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub import conversations as hub_conversations  # noqa: E402
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


async def _get_message_rows(db, conversation_id) -> list[Message]:
    async with db() as session:
        rows = await session.scalars(
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.asc())
        )
        return list(rows.all())


class _FakeWhatsAppClient:
    """Records constructed instances + sends; installed in place of the real
    client — same idiom as test_deposit_lifecycle.py's fake."""
    # Mirrors WhatsAppClient: the CALLER records the outbound row.
    persists_outbound = False

    created: list["_FakeWhatsAppClient"] = []

    def __init__(self, access_token=None, phone_number_id=None):
        self._access_token = access_token
        self._phone_number_id = phone_number_id
        self.sent: list[tuple] = []
        _FakeWhatsAppClient.created.append(self)

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls(access_token=waba_token, phone_number_id=tenant.phone_number_id)

    async def send_text_message(self, to, body):
        self.sent.append((to, body))
        return {"messages": [{"id": "wamid.console.1"}]}


class _FailingWhatsAppClient:
    """Every send raises — proves a failed delivery never gets persisted."""

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        raise httpx.ConnectError("whatsapp down")


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


async def test_list_includes_brain_message_patient_with_no_wa_id(
    client: AsyncClient, db, tenant
) -> None:
    """A brain_message-channel patient has wa_id=None by construction (no
    WhatsApp number) — the list must still serialize, not 500. Regression
    test for the ConversationRead.patient_wa_id non-optional-str bug."""
    patient = await _seed_patient(
        db,
        tenant,
        wa_id=None,
        channel="brain_message",
        external_id=str(uuid4()),
        name="Paciente Brain-Message",
    )
    conv = await _seed_conversation(db, tenant, patient, handover_state=HandoverState.BOT_ACTIVE)

    response = await client.get(ENDPOINT)
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == str(conv.id)
    assert body[0]["patient_wa_id"] is None
    assert body[0]["patient_name"] == "Paciente Brain-Message"


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


# --------------------------------------------------------------------------
# GET /messages — full thread
# --------------------------------------------------------------------------


async def test_list_messages_returns_full_thread_oldest_first(
    client: AsyncClient, db, tenant
) -> None:
    now = datetime.now(UTC)
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient)

    await _seed_message(
        db,
        conv,
        created_at=now - timedelta(minutes=5),
        sender=MessageSender.PATIENT,
        direction=MessageDirection.INBOUND,
        body="oi, quero marcar",
    )
    await _seed_message(
        db,
        conv,
        created_at=now - timedelta(minutes=4),
        sender=MessageSender.BOT,
        direction=MessageDirection.OUTBOUND,
        body="Claro! Qual serviço?",
    )
    await _seed_message(
        db,
        conv,
        created_at=now - timedelta(minutes=1),
        sender=MessageSender.HUMAN,
        direction=MessageDirection.OUTBOUND,
        body="Oi, aqui é a recepção",
    )

    response = await client.get(f"{ENDPOINT}/{conv.id}/messages")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 3
    assert [m["sender"] for m in body] == ["patient", "bot", "human"]
    assert [m["direction"] for m in body] == ["inbound", "outbound", "outbound"]
    assert body[0]["body"] == "oi, quero marcar"
    assert body[2]["body"] == "Oi, aqui é a recepção"
    assert all(m["id"] and m["created_at"] for m in body)


async def test_list_messages_is_empty_for_conversation_with_no_messages(
    client: AsyncClient, db, tenant
) -> None:
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient)

    response = await client.get(f"{ENDPOINT}/{conv.id}/messages")
    assert response.status_code == 200
    assert response.json() == []


async def test_list_messages_for_other_tenant_conversation_is_404(
    client: AsyncClient, db, tenant
) -> None:
    other_tenant = Tenant(id=uuid4(), clinic_name="Other Clinic", phone_number_id=str(uuid4())[:12])
    async with db() as session:
        session.add(other_tenant)
        await session.commit()
    patient = await _seed_patient(db, other_tenant)
    conv = await _seed_conversation(db, other_tenant, patient)
    await _seed_message(db, conv, created_at=datetime.now(UTC))

    response = await client.get(f"{ENDPOINT}/{conv.id}/messages")
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation not found"


async def test_list_messages_for_random_uuid_is_404(client: AsyncClient) -> None:
    response = await client.get(f"{ENDPOINT}/{uuid4()}/messages")
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation not found"


async def test_list_messages_without_token_is_401(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.api.hub import deps as hub_deps
    from secretaria.main import app

    app.dependency_overrides.pop(get_current_tenant, None)

    async def _fake_verify(token: str):
        return None

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake_verify)

    response = await client.get(f"{ENDPOINT}/{uuid4()}/messages")
    assert response.status_code == 401


# --------------------------------------------------------------------------
# POST /messages — staff sends a message
# --------------------------------------------------------------------------


async def test_send_message_persists_as_human_and_delivers_via_whatsapp(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeWhatsAppClient.created.clear()
    monkeypatch.setattr(hub_conversations, "WhatsAppClient", _FakeWhatsAppClient)

    patient = await _seed_patient(db, tenant, wa_id="5511987654321")
    conv = await _seed_conversation(db, tenant, patient, handover_state=HandoverState.BOT_ACTIVE)

    response = await client.post(f"{ENDPOINT}/{conv.id}/messages", json={"body": "Pode vir às 15h"})
    assert response.status_code == 200
    body = response.json()
    assert body["sender"] == "human"
    assert body["direction"] == "outbound"
    assert body["body"] == "Pode vir às 15h"

    # Delivery actually happened - the whole point of this endpoint.
    assert len(_FakeWhatsAppClient.created) == 1
    fake_client = _FakeWhatsAppClient.created[-1]
    assert fake_client.sent == [("5511987654321", "Pode vir às 15h")]

    # Persisted with the same shape smb_message_echoes already uses.
    rows = await _get_message_rows(db, conv.id)
    assert len(rows) == 1
    assert rows[0].sender == MessageSender.HUMAN
    assert rows[0].direction == MessageDirection.OUTBOUND
    assert rows[0].body == "Pode vir às 15h"
    assert rows[0].wam_id == "wamid.console.1"

    # Never diverges from the echo path: a human send takes the conversation over.
    persisted_conv = await _get_conversation_row(db, conv.id)
    assert persisted_conv.handover_state == HandoverState.HUMAN_ACTIVE
    assert persisted_conv.last_human_message_at is not None


async def test_send_message_for_other_tenant_conversation_is_404_and_never_sends(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    _FakeWhatsAppClient.created.clear()
    monkeypatch.setattr(hub_conversations, "WhatsAppClient", _FakeWhatsAppClient)

    other_tenant = Tenant(id=uuid4(), clinic_name="Other Clinic", phone_number_id=str(uuid4())[:12])
    async with db() as session:
        session.add(other_tenant)
        await session.commit()
    patient = await _seed_patient(db, other_tenant)
    conv = await _seed_conversation(db, other_tenant, patient)

    response = await client.post(f"{ENDPOINT}/{conv.id}/messages", json={"body": "oi"})
    assert response.status_code == 404
    assert response.json()["detail"] == "Conversation not found"
    assert _FakeWhatsAppClient.created == []  # isolation is checked before any send


async def test_send_message_without_token_is_401(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.api.hub import deps as hub_deps
    from secretaria.main import app

    app.dependency_overrides.pop(get_current_tenant, None)

    async def _fake_verify(token: str):
        return None

    monkeypatch.setattr(hub_deps, "verify_subscription_token", _fake_verify)

    response = await client.post(f"{ENDPOINT}/{uuid4()}/messages", json={"body": "oi"})
    assert response.status_code == 401


async def test_send_message_empty_body_is_422(client: AsyncClient, db, tenant) -> None:
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient)

    response = await client.post(f"{ENDPOINT}/{conv.id}/messages", json={"body": ""})
    assert response.status_code == 422


async def test_send_message_delivery_failure_is_502_and_persists_nothing(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_conversations, "WhatsAppClient", _FailingWhatsAppClient)

    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient, handover_state=HandoverState.BOT_ACTIVE)

    response = await client.post(f"{ENDPOINT}/{conv.id}/messages", json={"body": "oi"})
    assert response.status_code == 502

    rows = await _get_message_rows(db, conv.id)
    assert rows == []
    persisted_conv = await _get_conversation_row(db, conv.id)
    assert persisted_conv.handover_state == HandoverState.BOT_ACTIVE  # untouched on failure


async def test_send_message_missing_whatsapp_credentials_is_502(
    client: AsyncClient, db, tenant
) -> None:
    # No monkeypatch: the REAL WhatsAppClient.for_tenant, against a tenant with
    # no waba token provisioned in tenant_credentials -> fails closed
    # (PROMPT_FIX_21), same as every worker send path.
    patient = await _seed_patient(db, tenant)
    conv = await _seed_conversation(db, tenant, patient)

    response = await client.post(f"{ENDPOINT}/{conv.id}/messages", json={"body": "oi"})
    assert response.status_code == 502

    rows = await _get_message_rows(db, conv.id)
    assert rows == []


async def test_send_message_to_brain_message_patient_persists_without_touching_whatsapp(
    client: AsyncClient, db, tenant, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Staff replying on the Brain-Message channel must not call the Graph API.

    Regression for the bug reproduced live on 2026-09-09: `_send_via_whatsapp`
    called `send_text_message(to=patient.wa_id)` unconditionally, and a
    brain_message patient has `wa_id=None` by design (migration `c7e1a4b9d0f3`).
    Meta answered 400 to the null `to`, which this router maps to 502 — so staff
    could not answer a single patient on the new channel.

    The row IS the delivery here (the patient's console polls
    `/internal/brain-message/conversations/{external_id}/messages`, which filters
    by conversation and not by sender), so the assertions that matter are that a
    HUMAN row exists and that no WhatsApp client was ever built.
    """
    _FakeWhatsAppClient.created.clear()
    monkeypatch.setattr(hub_conversations, "WhatsAppClient", _FakeWhatsAppClient)

    patient = await _seed_patient(
        db,
        tenant,
        wa_id=None,
        channel="brain_message",
        external_id=str(uuid4()),
        name="Paciente Brain-Message",
    )
    conv = await _seed_conversation(db, tenant, patient, handover_state=HandoverState.BOT_ACTIVE)

    response = await client.post(
        f"{ENDPOINT}/{conv.id}/messages", json={"body": "Bom dia, pode vir às 15h"}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["sender"] == "human"
    assert body["direction"] == "outbound"
    assert body["body"] == "Bom dia, pode vir às 15h"

    # The whole point: no Graph API call was even attempted.
    assert _FakeWhatsAppClient.created == []

    rows = await _get_message_rows(db, conv.id)
    assert len(rows) == 1
    assert rows[0].sender == MessageSender.HUMAN
    assert rows[0].direction == MessageDirection.OUTBOUND
    # No Meta id exists for a message Meta never carried.
    assert rows[0].wam_id is None

    # Handover still flips, exactly as on the WhatsApp path.
    persisted_conv = await _get_conversation_row(db, conv.id)
    assert persisted_conv.handover_state == HandoverState.HUMAN_ACTIVE
    assert persisted_conv.last_human_message_at is not None
