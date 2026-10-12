"""Patient e-mail is masked in real DB history before the model boundary."""

import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.ai import graph  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    ConversationPiiTokenMap,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.schemas.conversation import ConversationRead, MessageRead  # noqa: E402
from secretaria.schemas.internal import InternalPatient  # noqa: E402
from secretaria.services import pii_pseudonymization as pii_store  # noqa: E402
from secretaria.services.llm_context import build_conversation_state  # noqa: E402

EMAIL = "maria.silva@exemplo.com"


@pytest_asyncio.fixture
async def db():
    engine = create_async_engine(
        "sqlite+aiosqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


async def _seed_portal_conversation(db, *, patient_email, typed):
    async with db() as session:
        tenant = Tenant(id=uuid4(), clinic_name="Clinica Teste", phone_number_id="12345")
        session.add(tenant)
        await session.flush()
        patient = Patient(
            tenant_id=tenant.id,
            channel="brain_message",
            external_id="portal-test",
            wa_id=None,
            email=patient_email,
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()
        for index, body in enumerate(typed):
            session.add(
                Message(
                    conversation_id=conversation.id,
                    direction=MessageDirection.INBOUND,
                    sender=MessageSender.PATIENT,
                    body=body,
                    created_at=datetime.now(UTC) + timedelta(seconds=index),
                )
            )
        await session.commit()
        return conversation


async def _history_seen_by_the_model(db, monkeypatch, conversation):
    seen = []

    async def _spy(messages):
        seen.append(messages)
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _spy)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(pii_store, "async_session_factory", db)
    await graph.run_agent("oi", context={"conversation_id": str(conversation.id)})
    return "\n".join(str(message.content) for message in seen[0])


async def test_email_reaches_the_llm_only_as_a_token(db, monkeypatch):
    conversation = await _seed_portal_conversation(
        db,
        patient_email=EMAIL,
        typed=[f"meu e-mail é {EMAIL.upper()}", f"confirma para {EMAIL}"],
    )
    history = await _history_seen_by_the_model(db, monkeypatch, conversation)
    assert EMAIL.casefold() not in history.casefold()
    assert "exemplo.com" not in history.casefold()
    assert "[EMAIL_" in history
    # The stored canonical value must seed the token before history discovery.
    # Without registration the regex learns the uppercase spelling first instead.
    async with db() as session:
        row = await session.get(ConversationPiiTokenMap, conversation.id)
        email_tokens = {
            token: value for token, value in row.tokens.items() if token.startswith("[EMAIL_")
        }
    assert list(email_tokens.values()) == [EMAIL]
    assert history.count(next(iter(email_tokens))) == 2


async def test_a_different_spelling_of_the_stored_email_is_masked_too(db, monkeypatch):
    conversation = await _seed_portal_conversation(
        db, patient_email=EMAIL, typed=[f"confirma pra {EMAIL.upper()}, por favor"]
    )
    history = await _history_seen_by_the_model(db, monkeypatch, conversation)
    assert "exemplo.com" not in history.casefold()


async def test_an_email_the_patient_typed_is_masked_even_before_it_is_stored(db, monkeypatch):
    conversation = await _seed_portal_conversation(
        db, patient_email=None, typed=[f"pode usar {EMAIL}"]
    )
    history = await _history_seen_by_the_model(db, monkeypatch, conversation)
    assert "exemplo.com" not in history.casefold()
    assert "[EMAIL_" in history


def test_no_read_schema_or_prompt_block_exposes_the_email():
    for schema in (InternalPatient, ConversationRead, MessageRead):
        assert "email" not in schema.model_fields
    conv = SimpleNamespace(
        flow_state=None,
        flow_step=None,
        flow_selected_type=None,
        flow_selected_day=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
        flow_attendee_name=None,
        email=EMAIL,
        patient_email=EMAIL,
    )
    assert "@" not in (build_conversation_state(conv, SimpleNamespace(), []) or "")
