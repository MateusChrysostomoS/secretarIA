"""Tests for the entitlement gate + per-tenant WhatsApp client wiring inside
workers/tasks.py:_send_bot_reply.

`_send_bot_reply` is a DB-touching function (needs a real session). This
mirrors the in-memory-sqlite pattern established in test_waba_encryption.py
(a real DB, monkeypatched in place of the Postgres-backed
`async_session_factory`) rather than test_reactivation.py / test_menu_command.py,
which only cover pure-logic helpers and never construct this seam.

run_agent, get_entitlements and WhatsAppClient are faked so no LLM/network
call is ever made; only the gating + wiring logic in _send_bot_reply itself
is under test.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

from uuid import uuid4

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.core.database import Base  # noqa: E402
from secretaria.models import Conversation, Patient, Tenant  # noqa: E402
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

_ALL_ADDONS_OFF = {
    "reactivation_pack": False,
    "verified_identity": False,
    "multi_professional": False,
    "multi_unit": False,
    "ehr": False,
    "pix_whatsapp": False,
    "analytics_bi": False,
    "human_backup_24_7": False,
}


def _summary(**overrides) -> EntitlementSummary:
    base = dict(
        tenant_id=str(uuid4()),
        status="active",
        active=True,
        secretaria_enabled=True,
        plan="bronze",
        secretaria_tier="bronze_1",
        addons=dict(_ALL_ADDONS_OFF),
        limits={},
    )
    base.update(overrides)
    return EntitlementSummary(**base)


class _FakeWhatsAppClient:
    """Records constructed instances + sends; installed in place of the real client."""

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
        self.sent.append(("text", to, body))
        return {"messages": [{"id": "wamid.test"}]}

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", to, body, buttons))
        return {"messages": [{"id": "wamid.test"}]}

    async def send_list(self, to, body, button_label, rows, section_title="Opções"):
        self.sent.append(("list", to, body, rows))
        return {"messages": [{"id": "wamid.test"}]}


@pytest_asyncio.fixture
async def db():
    """A sessionmaker bound to a shared in-memory sqlite DB (StaticPool keeps
    the single connection alive across every session opened from it, so rows
    committed via the `conversation` fixture are visible to _send_bot_reply's
    own `async_session_factory()` calls)."""
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
def _fakes(monkeypatch: pytest.MonkeyPatch, db):
    monkeypatch.setattr(tasks, "async_session_factory", db)
    _FakeWhatsAppClient.created = []
    monkeypatch.setattr(tasks, "WhatsAppClient", _FakeWhatsAppClient)
    # get_waba_token is exercised end-to-end elsewhere (test_waba_encryption.py);
    # here we only care that _send_bot_reply threads whatever it returns into
    # the WhatsApp client, so a fixed decrypted-looking value is enough.
    monkeypatch.setattr(tasks, "get_waba_token", _fake_get_waba_token)
    yield


async def _fake_get_waba_token(session, tenant_id):
    return "decrypted-waba-token"


async def _make_conversation(db, *, is_active: bool = True) -> tuple[Tenant, Patient, Conversation]:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=str(uuid4())[:12],
            is_active=is_active,
            initial_flows={},  # deterministic flow engine OFF -> always hits run_agent
        )
        patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999", name="Maria")
        session.add_all([tenant, patient])
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id)
        session.add(conversation)
        await session.commit()
        await session.refresh(tenant)
        await session.refresh(conversation)
        return tenant, patient, conversation


def _reply_context(conversation: Conversation, patient: Patient) -> tasks._ReplyContext:
    return tasks._ReplyContext(
        conversation_id=conversation.id,
        patient_wa_id=patient.wa_id,
        inbound_body="oi, quero marcar",
    )


# --------------------------------------------------------------------------
# Entitlement gating
# --------------------------------------------------------------------------


async def test_entitled_reply_flows_as_before(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        assert tenant_id == tenant.id
        return _summary()

    run_agent_calls: list[dict] = []

    async def _fake_run_agent(
        message, context, tenant_config=None, extra_tools=(), redis=None, **kwargs
    ):
        run_agent_calls.append(
            {"message": message, "tenant_config": tenant_config, "extra_tools": extra_tools}
        )
        return "Olá! Tudo bem?"

    monkeypatch.setattr(tasks, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(tasks, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert len(run_agent_calls) == 1
    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client.sent  # a message was actually sent
    assert client.sent[0][2] == "Olá! Tudo bem?"


async def test_unentitled_summary_suppresses_reply(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary(active=False, status="canceled")

    run_agent_calls: list[dict] = []

    async def _fake_run_agent(*args, **kwargs):
        run_agent_calls.append(kwargs)
        return "should never be sent"

    monkeypatch.setattr(tasks, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(tasks, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert run_agent_calls == []
    assert _FakeWhatsAppClient.created == []


async def test_secretaria_disabled_suppresses_reply(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary(active=True, secretaria_enabled=False)

    async def _fake_run_agent(*args, **kwargs):
        raise AssertionError("run_agent must not be called when suppressed")

    monkeypatch.setattr(tasks, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(tasks, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert _FakeWhatsAppClient.created == []


async def test_none_summary_suppresses_reply(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return None

    async def _fake_run_agent(*args, **kwargs):
        raise AssertionError("run_agent must not be called when suppressed")

    monkeypatch.setattr(tasks, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(tasks, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert _FakeWhatsAppClient.created == []


async def test_suppressed_reply_logs_tenant_and_status(monkeypatch: pytest.MonkeyPatch, db) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary(active=False, status="past_due")

    async def _fake_run_agent(*args, **kwargs):
        raise AssertionError("run_agent must not be called when suppressed")

    monkeypatch.setattr(tasks, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(tasks, "run_agent", _fake_run_agent)

    captured: dict = {}
    original_warning = tasks.logger.warning

    def _capture(event, **kwargs):
        if event == "bot_reply_suppressed_unentitled":
            captured["event"] = event
            captured.update(kwargs)
        return original_warning(event, **kwargs)

    monkeypatch.setattr(tasks.logger, "warning", _capture)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert captured.get("event") == "bot_reply_suppressed_unentitled"
    assert captured.get("tenant_id") == str(tenant.id)
    assert captured.get("status") == "past_due"


# --------------------------------------------------------------------------
# Per-tenant WhatsApp client + plugin tools wiring
# --------------------------------------------------------------------------


async def test_reply_uses_whatsapp_client_for_tenant_with_decrypted_token(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    tenant, patient, conversation = await _make_conversation(db)

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary()

    async def _fake_run_agent(
        message, context, tenant_config=None, extra_tools=(), redis=None, **kwargs
    ):
        return "Oi!"

    monkeypatch.setattr(tasks, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(tasks, "run_agent", _fake_run_agent)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert len(_FakeWhatsAppClient.created) == 1
    client = _FakeWhatsAppClient.created[0]
    assert client._access_token == "decrypted-waba-token"
    assert client._phone_number_id == tenant.phone_number_id


async def test_run_agent_receives_plugin_tools_for_entitled_addons(
    monkeypatch: pytest.MonkeyPatch, db
) -> None:
    tenant, patient, conversation = await _make_conversation(db)
    sentinel_tools = ["sentinel-tool"]

    async def _fake_get_entitlements(tenant_id, redis):
        return _summary(addons={**_ALL_ADDONS_OFF, "pix_whatsapp": True})

    run_agent_calls: list[dict] = []

    async def _fake_run_agent(
        message, context, tenant_config=None, extra_tools=(), redis=None, **kwargs
    ):
        run_agent_calls.append({"extra_tools": extra_tools})
        return "Oi!"

    monkeypatch.setattr(tasks, "get_entitlements", _fake_get_entitlements)
    monkeypatch.setattr(tasks, "run_agent", _fake_run_agent)
    monkeypatch.setattr(tasks, "agent_tools_for", lambda summary: sentinel_tools)

    await tasks._send_bot_reply(_reply_context(conversation, patient))

    assert run_agent_calls == [{"extra_tools": sentinel_tools}]
