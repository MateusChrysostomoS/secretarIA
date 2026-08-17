"""Tests for the Coexistence test-window allowlist (`BOT_ALLOWLIST_WA_IDS`).

Covers:
  - `Settings.bot_allowlist_wa_ids` (pure, config.py): CSV parsing, digits-only
    normalization, quote-stripping, empty-entry tolerance.
  - The `_persist_inbound_message` guard (workers/tasks.py): empty allowlist =
    unchanged production behavior; a wa_id off the allowlist is dropped with
    no reply and no Patient/Conversation/ConsentEvent, but the dedup
    `ProcessedEvent` row is still written; a wa_id on the allowlist flows
    through normally.
  - The `_persist_human_echo` guard: an off-allowlist echo creates no
    Patient/Conversation, but `mode_resolved_at` is still set (Coexistence
    mode resolution must survive even when the echoed conversation itself is
    discarded — see the inline comment at the guard).

DB tests mirror the in-memory-sqlite pattern from test_action_buttons.py /
test_handover_echoes.py: a real aiosqlite engine on StaticPool, monkeypatched
in place of `workers.tasks.async_session_factory`.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

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
    Patient,
    ProcessedEvent,
    Tenant,
)
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"


# --------------------------------------------------------------------------
# Settings.bot_allowlist_wa_ids - pure unit tests
# --------------------------------------------------------------------------


def test_bot_allowlist_empty_is_no_restriction():
    settings = Settings(BOT_ALLOWLIST_WA_IDS="")
    assert settings.bot_allowlist_wa_ids == frozenset()


def test_bot_allowlist_normalizes_formatted_number():
    settings = Settings(BOT_ALLOWLIST_WA_IDS="+55 (11) 99999-8888")
    assert settings.bot_allowlist_wa_ids == frozenset({"5511999998888"})


def test_bot_allowlist_parses_csv_multiple_entries():
    settings = Settings(BOT_ALLOWLIST_WA_IDS="5511999998888,5521988887777")
    assert settings.bot_allowlist_wa_ids == frozenset({"5511999998888", "5521988887777"})


def test_bot_allowlist_strips_surrounding_quotes():
    settings = Settings(BOT_ALLOWLIST_WA_IDS="'5511999998888',\"5521988887777\"")
    assert settings.bot_allowlist_wa_ids == frozenset({"5511999998888", "5521988887777"})


def test_bot_allowlist_drops_blank_entries():
    settings = Settings(BOT_ALLOWLIST_WA_IDS="5511999998888,,  ,")
    assert settings.bot_allowlist_wa_ids == frozenset({"5511999998888"})


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
    # The greeting-adaptation path does its own indexed reads; stub it out
    # exactly like test_patient_context.py's opening-router test so these
    # tests exercise only the allowlist guard, not opening-state resolution.
    async def _fake_resolve(session, tenant_id, patient_id, **kwargs):
        return None

    monkeypatch.setattr(tasks, "resolve_patient_opening_state", _fake_resolve)
    yield


def _set_allowlist(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    fake_settings = Settings(BOT_ALLOWLIST_WA_IDS=raw)
    monkeypatch.setattr(tasks, "get_settings", lambda: fake_settings)


async def _seed_tenant(db, **kwargs) -> Tenant:
    async with db() as session:
        kwargs.setdefault("is_active", True)
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=PHONE_NUMBER_ID,
            greeting_message="Olá! Bem-vindo à Clínica.",
            **kwargs,
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _count(db, model) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


# --------------------------------------------------------------------------
# _persist_inbound_message guard
# --------------------------------------------------------------------------


async def test_empty_allowlist_behaves_like_before(db, monkeypatch: pytest.MonkeyPatch):
    _set_allowlist(monkeypatch, "")
    tenant = await _seed_tenant(db)

    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id="5511988887777",
        patient_name="Maria",
        wam_id="wamid.allow.1",
        body="oi",
    )

    assert reply is not None
    assert reply.greeting_override == tenant.greeting_message
    assert await _count(db, Patient) == 1
    assert await _count(db, Conversation) == 1
    assert await _count(db, ConsentEvent) == 1
    assert await _count(db, ProcessedEvent) == 1


async def test_greeting_uses_tenant_whatsapp_credentials(
    db, monkeypatch: pytest.MonkeyPatch
):
    _set_allowlist(monkeypatch, "")
    tenant = await _seed_tenant(db)
    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id="5511988887777",
        patient_name="Maria",
        wam_id="wamid.greeting.tenant-client",
        body="oi",
    )
    assert reply is not None

    captured = {}

    class _Client:
        async def send_buttons(self, *, to, body, buttons):
            captured.update(to=to, body=body, buttons=buttons)
            return {"messages": [{"id": "wamid.sent.greeting"}]}

    def _for_tenant(cls, selected_tenant, token):
        captured.update(tenant=selected_tenant, token=token)
        return _Client()

    monkeypatch.setattr(tasks.WhatsAppClient, "for_tenant", classmethod(_for_tenant))

    await tasks._send_greeting(reply, tenant=tenant, waba_token="tenant-token")

    assert captured["tenant"].id == tenant.id
    assert captured["token"] == "tenant-token"
    assert captured["to"] == "5511988887777"
    assert captured["body"] == tenant.greeting_message


async def test_wa_id_off_allowlist_is_dropped_silently(db, monkeypatch: pytest.MonkeyPatch):
    _set_allowlist(monkeypatch, "5521900000000")  # a different number
    tenant = await _seed_tenant(db)

    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id="5511988887777",
        patient_name="Maria",
        wam_id="wamid.allow.2",
        body="oi",
    )

    assert reply is None
    assert await _count(db, Patient) == 0
    assert await _count(db, Conversation) == 0
    assert await _count(db, ConsentEvent) == 0
    # Idempotency ledger row is still written - the event WAS seen.
    assert await _count(db, ProcessedEvent) == 1


async def test_wa_id_on_allowlist_flows_normally(db, monkeypatch: pytest.MonkeyPatch):
    wa_id = "5511988887777"
    _set_allowlist(monkeypatch, "+55 (11) 98888-7777")  # formatted, matches digits
    tenant = await _seed_tenant(db)

    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id=wa_id,
        patient_name="Maria",
        wam_id="wamid.allow.3",
        body="oi",
    )

    assert reply is not None
    assert reply.greeting_override == tenant.greeting_message
    assert await _count(db, Patient) == 1
    assert await _count(db, Conversation) == 1
    assert await _count(db, ConsentEvent) == 1


async def test_allowlist_blocks_even_the_inactive_tenant_fallback(
    db, monkeypatch: pytest.MonkeyPatch
):
    """Ordering fix (PROMPT_FIX_21): the allowlist runs BEFORE the
    tenant-active gate.

    The inactive-tenant branch returns a `service_unavailable` context, which
    IS an outbound message. Evaluating it first meant an off-allowlist number
    could still make the platform send during the restricted Coexistence test
    window, just by talking to a tenant that happened to be inactive.
    """
    _set_allowlist(monkeypatch, "5521900000000")  # a different number
    tenant = await _seed_tenant(db, is_active=False)

    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id="5511988887777",
        patient_name="Maria",
        wam_id="wamid.allow.inactive",
        body="oi",
    )

    assert reply is None  # NOT a service_unavailable context
    assert await _count(db, Patient) == 0
    assert await _count(db, ProcessedEvent) == 1  # the event WAS seen


async def test_inactive_tenant_on_allowlist_still_gets_the_fallback(
    db, monkeypatch: pytest.MonkeyPatch
):
    """The reorder must not silence the legitimate case."""
    _set_allowlist(monkeypatch, "5511988887777")
    tenant = await _seed_tenant(db, is_active=False)

    reply = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id="5511988887777",
        patient_name="Maria",
        wam_id="wamid.allow.inactive.ok",
        body="oi",
    )

    assert reply is not None
    assert reply.service_unavailable is True
    # The tenant rides along so the fallback goes out on ITS credentials.
    assert reply.tenant_id == tenant.id


async def test_duplicate_event_off_allowlist_is_still_deduped(
    db, monkeypatch: pytest.MonkeyPatch
):
    """A redelivery of an already-dropped event hits the pre-existing
    idempotency fast path (`_event_already_processed`), same as any other
    event - the allowlist guard does not interfere with dedup."""
    _set_allowlist(monkeypatch, "5521900000000")
    tenant = await _seed_tenant(db)

    first = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id="5511988887777",
        patient_name="Maria",
        wam_id="wamid.allow.dup",
        body="oi",
    )
    second = await tasks._persist_inbound_message(
        phone_number_id=tenant.phone_number_id,
        wa_id="5511988887777",
        patient_name="Maria",
        wam_id="wamid.allow.dup",
        body="oi",
    )

    assert first is None
    assert second is None
    assert await _count(db, ProcessedEvent) == 1


# --------------------------------------------------------------------------
# _persist_human_echo guard
# --------------------------------------------------------------------------


def _echo_value(*, wa_id: str, wam_id: str, body: str = "oi, pode vir"):
    from secretaria.schemas.webhook import WebhookValue

    return WebhookValue.model_validate(
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"wa_id": wa_id}],
            "message_echoes": [{"id": wam_id, "to": wa_id, "type": "text", "text": {"body": body}}],
        }
    )


async def test_echo_off_allowlist_creates_no_patient_but_marks_mode_resolved(
    db, monkeypatch: pytest.MonkeyPatch
):
    _set_allowlist(monkeypatch, "5521900000000")
    tenant = await _seed_tenant(db)
    wa_id = "5511988887777"

    await tasks._handle_human_echoes(_echo_value(wa_id=wa_id, wam_id="wamid.echo.1"))

    assert await _count(db, Patient) == 0
    assert await _count(db, Conversation) == 0

    async with db() as session:
        refreshed = await session.get(Tenant, tenant.id)
    assert refreshed.mode_resolved_at is not None


async def test_echo_on_allowlist_flows_normally(db, monkeypatch: pytest.MonkeyPatch):
    wa_id = "5511988887777"
    _set_allowlist(monkeypatch, wa_id)
    await _seed_tenant(db)

    await tasks._handle_human_echoes(_echo_value(wa_id=wa_id, wam_id="wamid.echo.2"))

    assert await _count(db, Patient) == 1
    assert await _count(db, Conversation) == 1


async def test_echo_empty_allowlist_behaves_like_before(db, monkeypatch: pytest.MonkeyPatch):
    _set_allowlist(monkeypatch, "")
    wa_id = "5511988887777"
    await _seed_tenant(db)

    await tasks._handle_human_echoes(_echo_value(wa_id=wa_id, wam_id="wamid.echo.3"))

    assert await _count(db, Patient) == 1
    assert await _count(db, Conversation) == 1
