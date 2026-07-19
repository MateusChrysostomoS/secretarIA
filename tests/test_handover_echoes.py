"""Tests for Coexistence human-echo handling (`smb_message_echoes`).

Covers `extract_echo_body` (pure, schemas/webhook.py) plus the DB-touching
worker path `_handle_human_echoes` / `_persist_human_echo` (workers/tasks.py)
and the `check_handover_timeouts` cron (services/handover.py wiring).

DB tests mirror the in-memory-sqlite pattern from test_bot_reply_gating.py:
a real aiosqlite engine on StaticPool (so every `async_session_factory()`
call inside the worker sees the same in-memory DB), monkeypatched in place
of the Postgres-backed `secretaria.workers.tasks.async_session_factory`. The
env-var setup block below matches test_hub_professionals.py.
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
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import get_settings  # noqa: E402
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
from secretaria.schemas.webhook import (  # noqa: E402
    WebhookMessage,
    WebhookValue,
    extract_echo_body,
)
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"  # matches META_PHONE_NUMBER_ID above


# --------------------------------------------------------------------------
# extract_echo_body - pure unit tests
# --------------------------------------------------------------------------


def _msg(**kw) -> WebhookMessage:
    return WebhookMessage.model_validate(kw)


def test_extract_echo_body_edit_with_text():
    msg = _msg(id="wamid.1", type="edit", text={"body": "novo texto"})
    assert extract_echo_body(msg) == "[mensagem editada: novo texto]"


def test_extract_echo_body_edit_without_text():
    msg = _msg(id="wamid.1", type="edit")
    assert extract_echo_body(msg) == "[mensagem editada]"


def test_extract_echo_body_edit_with_empty_text_body():
    msg = _msg(id="wamid.1", type="edit", text={"body": ""})
    assert extract_echo_body(msg) == "[mensagem editada]"


def test_extract_echo_body_revoke():
    msg = _msg(id="wamid.1", type="revoke", text={"body": "should be ignored"})
    assert extract_echo_body(msg) == "[mensagem apagada]"


def test_extract_echo_body_plain_text_delegates():
    msg = _msg(id="wamid.1", type="text", text={"body": "oi"})
    assert extract_echo_body(msg) == "oi"


def test_extract_echo_body_unactionable_type_delegates_to_none():
    msg = _msg(id="wamid.1", type="image")
    assert extract_echo_body(msg) is None


# --------------------------------------------------------------------------
# DB fixture (shared in-memory sqlite, StaticPool)
# --------------------------------------------------------------------------


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


@pytest.fixture(autouse=True)
def _wire_db(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    yield


async def _seed_tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


def _echo_value(*, wa_id: str, echoes: list[dict]) -> WebhookValue:
    return WebhookValue.model_validate(
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"wa_id": wa_id}],
            "message_echoes": echoes,
        }
    )


async def _load_conversation(db, patient_wa_id: str) -> Conversation:
    async with db() as session:
        patient = await session.scalar(select(Patient).where(Patient.wa_id == patient_wa_id))
        assert patient is not None
        conversation = await session.scalar(
            select(Conversation).where(Conversation.patient_id == patient.id)
        )
        assert conversation is not None
        return conversation


async def _load_message(db, wam_id: str) -> Message:
    async with db() as session:
        message = await session.scalar(select(Message).where(Message.wam_id == wam_id))
        assert message is not None
        return message


# --------------------------------------------------------------------------
# _handle_human_echoes - text / edit / revoke
# --------------------------------------------------------------------------


async def test_text_echo_persists_message_and_flips_handover(db):
    await _seed_tenant(db)
    wa_id = "5521900000001"
    value = _echo_value(
        wa_id=wa_id,
        echoes=[
            {"id": "wamid.text.1", "to": wa_id, "type": "text", "text": {"body": "oi, pode vir"}}
        ],
    )

    await tasks._handle_human_echoes(value)

    message = await _load_message(db, "wamid.text.1")
    assert message.sender == MessageSender.HUMAN
    assert message.direction == MessageDirection.OUTBOUND
    assert message.body == "oi, pode vir"

    conversation = await _load_conversation(db, wa_id)
    assert conversation.handover_state == HandoverState.HUMAN_ACTIVE
    assert conversation.last_human_message_at is not None


async def test_text_echo_marks_mode_resolved_when_null(db):
    """smb_message_echoes is one of the three Coexistence mode-resolution
    signals (contract v1 §10) - a NULL mode_resolved_at flips to non-NULL."""
    tenant = await _seed_tenant(db)
    wa_id = "5521900000099"
    value = _echo_value(
        wa_id=wa_id,
        echoes=[{"id": "wamid.text.mode", "to": wa_id, "type": "text", "text": {"body": "oi"}}],
    )

    await tasks._handle_human_echoes(value)

    async with db() as session:
        refreshed = await session.get(Tenant, tenant.id)
    assert refreshed.mode_resolved_at is not None


async def test_text_echo_does_not_overwrite_existing_mode_resolved_at(db):
    tenant = await _seed_tenant(db)
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.mode_resolved_at = fixed
        await session.commit()

    wa_id = "5521900000098"
    value = _echo_value(
        wa_id=wa_id,
        echoes=[{"id": "wamid.text.mode2", "to": wa_id, "type": "text", "text": {"body": "oi"}}],
    )
    await tasks._handle_human_echoes(value)

    async with db() as session:
        refreshed = await session.get(Tenant, tenant.id)
    # SQLite round-trips DateTime as naive - compare the naive components
    # (the point under test is "unchanged", not tz representation).
    assert refreshed.mode_resolved_at.replace(tzinfo=UTC) == fixed


async def test_edit_echo_persists_placeholder_body_and_flips_handover(db):
    await _seed_tenant(db)
    wa_id = "5521900000002"
    value = _echo_value(
        wa_id=wa_id,
        echoes=[
            {
                "id": "wamid.edit.1",
                "to": wa_id,
                "type": "edit",
                "text": {"body": "chegue as 15h"},
            }
        ],
    )

    await tasks._handle_human_echoes(value)

    message = await _load_message(db, "wamid.edit.1")
    assert message.body == "[mensagem editada: chegue as 15h]"
    assert message.sender == MessageSender.HUMAN

    conversation = await _load_conversation(db, wa_id)
    assert conversation.handover_state == HandoverState.HUMAN_ACTIVE


async def test_revoke_echo_persists_placeholder_body_and_flips_handover(db):
    await _seed_tenant(db)
    wa_id = "5521900000003"
    value = _echo_value(
        wa_id=wa_id,
        echoes=[{"id": "wamid.revoke.1", "to": wa_id, "type": "revoke"}],
    )

    await tasks._handle_human_echoes(value)

    message = await _load_message(db, "wamid.revoke.1")
    assert message.body == "[mensagem apagada]"

    conversation = await _load_conversation(db, wa_id)
    assert conversation.handover_state == HandoverState.HUMAN_ACTIVE


# --------------------------------------------------------------------------
# check_handover_timeouts
# --------------------------------------------------------------------------


async def _seed_conversation(db, *, last_human_message_at) -> tuple[Tenant, Conversation]:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=True,
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id=str(uuid4())[:12])
        session.add_all([tenant, patient])
        await session.flush()
        conversation = Conversation(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient.id,
            handover_state=HandoverState.HUMAN_ACTIVE,
            last_human_message_at=last_human_message_at,
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(conversation)
        return tenant, conversation


async def test_check_handover_timeouts_flips_stale_conversation(db):
    timeout_minutes = get_settings().HANDOVER_TIMEOUT_MINUTES
    stale_at = datetime.now(UTC) - timedelta(minutes=timeout_minutes + 5)
    _, stale_conversation = await _seed_conversation(db, last_human_message_at=stale_at)

    await tasks.check_handover_timeouts({})

    async with db() as session:
        refreshed = await session.get(Conversation, stale_conversation.id)
        assert refreshed.handover_state == HandoverState.BOT_ACTIVE


async def test_check_handover_timeouts_leaves_recent_conversation_untouched(db):
    timeout_minutes = get_settings().HANDOVER_TIMEOUT_MINUTES
    recent_at = datetime.now(UTC) - timedelta(minutes=max(timeout_minutes - 5, 1))
    _, recent_conversation = await _seed_conversation(db, last_human_message_at=recent_at)

    await tasks.check_handover_timeouts({})

    async with db() as session:
        refreshed = await session.get(Conversation, recent_conversation.id)
        assert refreshed.handover_state == HandoverState.HUMAN_ACTIVE
