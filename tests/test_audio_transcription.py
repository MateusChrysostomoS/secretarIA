"""Tests for inbound WhatsApp AUDIO transcription.

Covers `iter_audio_messages` (pure, schemas/webhook.py), the webhook POST
handler's audio-enqueue side effect (api/webhook.py), the
`_handle_patient_messages` audio skip (workers/tasks.py), the
`transcribe_audio_message` arq job end-to-end against a real sqlite DB, and
the OPENAI_MODEL -> OPENAI_SECRETARIA_MODEL config split (config.py).

DB tests mirror the in-memory-sqlite pattern from test_handover_echoes.py /
test_bot_reply_gating.py: a real aiosqlite engine on StaticPool (so every
`async_session_factory()` call inside the worker - and, for the webhook
enqueue test, inside the API layer's idempotency check - sees the same
in-memory DB), monkeypatched in place of the Postgres-backed
`async_session_factory` on both `workers.tasks` and `api.webhook`. The
env-var setup block below matches test_hub_professionals.py /
test_handover_echoes.py.
"""

import hashlib
import hmac
import json
import os
from uuid import uuid4

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
# transcribe_audio_message always builds a TranscriptionConfig (even when the
# STT call itself is faked), which requires a non-empty openai_api_key.
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402
from transcription_core import MediaFetchError, MediaTooLarge, TranscriptionResult  # noqa: E402

from secretaria.api import webhook as webhook_api  # noqa: E402
from secretaria.config import get_settings  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Message,
    MessageDirection,
    MessageSender,
    ProcessedEvent,
    Tenant,
)
from secretaria.schemas.webhook import (  # noqa: E402
    WebhookValue,
    iter_audio_messages,
    minimal_event_payload,
)
from secretaria.services.tenant_config import set_waba_token  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

PHONE_NUMBER_ID = "1234567890"  # matches META_PHONE_NUMBER_ID above
WA_ID = "5511999990000"
# Deliberately DIFFERENT from META_ACCESS_TOKEN: the audio path must use the
# tenant's own decrypted token, never the global env scaffold (PROMPT_FIX_21).
TENANT_WABA_TOKEN = "tenant-waba-token"


# --------------------------------------------------------------------------
# iter_audio_messages - pure unit tests
# --------------------------------------------------------------------------


def _voice_payload(
    *,
    message_id: str = "wamid.AUDIO1",
    media_id: str = "MEDIA1",
    wa_id: str = WA_ID,
    phone_number_id: str = PHONE_NUMBER_ID,
    name: str | None = "Ana",
) -> dict:
    """A realistic Meta Cloud API webhook payload for one inbound voice note."""
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "15550001111",
                                "phone_number_id": phone_number_id,
                            },
                            "contacts": [{"wa_id": wa_id, "profile": {"name": name}}],
                            "messages": [
                                {
                                    "id": message_id,
                                    "from": wa_id,
                                    "timestamp": "1700000000",
                                    "type": "audio",
                                    "audio": {
                                        "id": media_id,
                                        "mime_type": "audio/ogg; codecs=opus",
                                        "voice": True,
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }


def test_iter_audio_messages_yields_expected_dict():
    payload = _voice_payload()
    assert list(iter_audio_messages(payload)) == [
        {
            "media_id": "MEDIA1",
            "phone_number_id": PHONE_NUMBER_ID,
            "wa_id": WA_ID,
            "message_id": "wamid.AUDIO1",
            "patient_name": "Ana",
        }
    ]


def test_iter_audio_messages_ignores_smb_message_echoes():
    """A coexistence echo of the business's OWN outbound audio is never transcribed."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "smb_message_echoes",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": WA_ID, "profile": {"name": "Ana"}}],
                            "message_echoes": [
                                {
                                    "id": "wamid.ECHO1",
                                    "from": PHONE_NUMBER_ID,
                                    "to": WA_ID,
                                    "type": "audio",
                                    "audio": {
                                        "id": "MEDIA_ECHO",
                                        "mime_type": "audio/ogg; codecs=opus",
                                        "voice": True,
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    assert list(iter_audio_messages(payload)) == []


def test_iter_audio_messages_text_only_yields_nothing():
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": WA_ID, "profile": {"name": "Ana"}}],
                            "messages": [
                                {
                                    "id": "wamid.TEXT1",
                                    "from": WA_ID,
                                    "type": "text",
                                    "text": {"body": "oi, queria marcar uma consulta"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    assert list(iter_audio_messages(payload)) == []


def test_iter_audio_messages_without_media_id_yields_nothing():
    payload = _voice_payload()
    del payload["entry"][0]["changes"][0]["value"]["messages"][0]["audio"]["id"]
    assert list(iter_audio_messages(payload)) == []


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"entry": "x"},
        {"entry": [1, 2, 3]},
        {"entry": [{"changes": "nope"}]},
        {"entry": [{"changes": [1, 2]}]},
        {"entry": [{"changes": [{"field": "messages", "value": "nope"}]}]},
        {"entry": [{"changes": [{"field": "messages", "value": {"messages": "nope"}}]}]},
        {"entry": [{"changes": [{"field": "messages", "value": {"messages": [1, "x"]}}]}]},
        {
            "entry": [
                {
                    "changes": [
                        {
                            "field": "messages",
                            "value": {"messages": [{"type": "audio", "audio": "nope"}]},
                        }
                    ]
                }
            ]
        },
        None,
        "just a string",
        123,
    ],
)
def test_iter_audio_messages_garbage_payloads_never_raise(payload):
    assert list(iter_audio_messages(payload)) == []


# --------------------------------------------------------------------------
# DB fixture (shared in-memory sqlite, StaticPool) - wired into BOTH the
# worker module and the webhook API module, since the webhook enqueue test
# exercises the HTTP layer's own idempotency DB check too.
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
    monkeypatch.setattr(webhook_api, "async_session_factory", db)
    yield


@pytest.fixture(autouse=True)
def _reset_settings_cache_after():
    """Guarantee a fresh Settings rebuild for whichever test runs next.

    Belt-and-suspenders alongside the explicit cache_clear() calls in the
    config-split tests below: even if a test forgets, the cache never stays
    populated with a mutated-env Settings instance past this file's tests.
    """
    yield
    get_settings.cache_clear()


# --------------------------------------------------------------------------
# Webhook enqueue: POST /webhook enqueues BOTH jobs with the minimal payload
# --------------------------------------------------------------------------


class _FakeArqPool:
    """Records every enqueue_job call; installed on app.state.arq_pool."""

    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))


def _sign(body: bytes) -> str:
    digest = hmac.new(b"test-app-secret", body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def test_webhook_enqueues_audio_job_with_minimal_payload(client, monkeypatch) -> None:
    from secretaria.main import app as fastapi_app

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    payload = _voice_payload()
    body = json.dumps(payload).encode()

    response = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)},
    )
    assert response.status_code == 200

    process_calls = [c for c in fake_pool.calls if c[0] == "process_webhook_event"]
    audio_calls = [c for c in fake_pool.calls if c[0] == "transcribe_audio_message"]

    assert len(process_calls) == 1
    # The MINIMAL envelope, positional - never the raw Meta body
    # (PROMPT_FIX_21). Same key names, so the worker parses it unchanged.
    assert process_calls[0][1] == (minimal_event_payload(payload),)
    assert process_calls[0][2] == {}

    assert len(audio_calls) == 1
    name, args, kwargs = audio_calls[0]
    assert args == ()  # minimal payload only, passed as kwargs
    assert kwargs == {
        "media_id": "MEDIA1",
        "phone_number_id": PHONE_NUMBER_ID,
        "wa_id": WA_ID,
        "message_id": "wamid.AUDIO1",
        "patient_name": "Ana",
    }


async def test_webhook_text_only_does_not_enqueue_audio_job(client, monkeypatch) -> None:
    from secretaria.main import app as fastapi_app

    fake_pool = _FakeArqPool()
    monkeypatch.setattr(fastapi_app.state, "arq_pool", fake_pool, raising=False)

    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
                            "contacts": [{"wa_id": WA_ID, "profile": {"name": "Ana"}}],
                            "messages": [
                                {
                                    "id": "wamid.TEXT1",
                                    "from": WA_ID,
                                    "type": "text",
                                    "text": {"body": "oi"},
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    body = json.dumps(payload).encode()

    response = await client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": _sign(body)},
    )
    assert response.status_code == 200
    assert [c[0] for c in fake_pool.calls] == ["process_webhook_event"]


# --------------------------------------------------------------------------
# _handle_patient_messages: audio is skipped, text in the same value flows
# --------------------------------------------------------------------------


async def test_handle_patient_messages_skips_audio_but_text_still_flows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rate_limit_calls: list[tuple] = []
    persist_calls: list[dict] = []

    async def _fake_is_rate_limited(redis, phone_number_id, wa_id):
        rate_limit_calls.append((phone_number_id, wa_id))
        return False

    async def _fake_persist_inbound_message(**kwargs):
        persist_calls.append(kwargs)
        return None

    monkeypatch.setattr(tasks, "_is_rate_limited", _fake_is_rate_limited)
    monkeypatch.setattr(tasks, "_persist_inbound_message", _fake_persist_inbound_message)

    value = WebhookValue.model_validate(
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"wa_id": WA_ID, "profile": {"name": "Ana"}}],
            "messages": [
                {
                    "id": "wamid.AUDIO1",
                    "from": WA_ID,
                    "type": "audio",
                    "audio": {"id": "MEDIA1", "mime_type": "audio/ogg", "voice": True},
                },
                {
                    "id": "wamid.TEXT1",
                    "from": WA_ID,
                    "type": "text",
                    "text": {"body": "oi"},
                },
            ],
        }
    )

    await tasks._handle_patient_messages(value, redis=None)

    # Exactly one call, for the TEXT message - the audio one never reached
    # either the rate limiter or the persist path.
    assert rate_limit_calls == [(PHONE_NUMBER_ID, WA_ID)]
    assert len(persist_calls) == 1
    assert persist_calls[0]["wam_id"] == "wamid.TEXT1"
    assert persist_calls[0]["body"] == "oi"


async def test_handle_patient_messages_audio_without_media_id_falls_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio with no `audio.id` is NOT skipped - it falls through to the
    quiet body=None path (extract_inbound_body returns None for type=audio),
    exactly like today's behavior for any other unactionable message type.
    """
    rate_limit_calls: list[tuple] = []
    persist_calls: list[dict] = []

    async def _fake_is_rate_limited(redis, phone_number_id, wa_id):
        rate_limit_calls.append((phone_number_id, wa_id))
        return False

    async def _fake_persist_inbound_message(**kwargs):
        persist_calls.append(kwargs)
        return None

    monkeypatch.setattr(tasks, "_is_rate_limited", _fake_is_rate_limited)
    monkeypatch.setattr(tasks, "_persist_inbound_message", _fake_persist_inbound_message)

    value = WebhookValue.model_validate(
        {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_NUMBER_ID},
            "contacts": [{"wa_id": WA_ID, "profile": {"name": "Ana"}}],
            "messages": [
                {"id": "wamid.AUDIO2", "from": WA_ID, "type": "audio"},
            ],
        }
    )

    await tasks._handle_patient_messages(value, redis=None)

    assert rate_limit_calls == [(PHONE_NUMBER_ID, WA_ID)]
    assert len(persist_calls) == 1
    assert persist_calls[0]["wam_id"] == "wamid.AUDIO2"
    assert persist_calls[0]["body"] is None


# --------------------------------------------------------------------------
# transcribe_audio_message - job happy path, low confidence, idempotency,
# permanent failure, transient failure (real sqlite DB fixture)
# --------------------------------------------------------------------------


async def _seed_tenant(db) -> Tenant:
    """A connected, active tenant matching PHONE_NUMBER_ID.

    `_resolve_tenant` (workers/tasks.py) no longer auto-provisions a tenant
    on an unrecognized number by default (settings.ALLOW_WEBHOOK_AUTOPROVISION
    is off unless a test opts in) - these job tests exercise
    `transcribe_audio_message` itself, not tenant resolution/auto-provision,
    so they seed the tenant explicitly like the rest of the suite
    (test_handover_echoes.py's `_seed_tenant`) instead of relying on that
    scaffold.

    The tenant's own WABA token is stored too: the audio path is fail-closed
    per tenant now (PROMPT_FIX_21) and no longer falls back to the global
    META_ACCESS_TOKEN for either the Graph media download or the clarification
    reply. See `test_missing_tenant_token_skips_transcription` for the
    no-token case.
    """
    async with db() as session:
        tenant = Tenant(
            id=uuid4(), clinic_name="Clinic", phone_number_id=PHONE_NUMBER_ID, is_active=True
        )
        session.add(tenant)
        await session.flush()
        await set_waba_token(session, tenant.id, TENANT_WABA_TOKEN)
        await session.commit()
        await session.refresh(tenant)
        return tenant


def _spy_send_bot_reply(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    async def _fake(reply, redis=None):
        calls.append(reply)

    monkeypatch.setattr(tasks, "_send_bot_reply", _fake)
    return calls


def _spy_send_simple_text(monkeypatch: pytest.MonkeyPatch) -> list:
    calls: list = []

    async def _fake(to, body, client=None):
        calls.append((to, body, client))

    monkeypatch.setattr(tasks, "_send_simple_text", _fake)
    return calls


async def test_happy_path_transcribes_and_persists_like_text(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenant(db)
    transcribe_calls: list[dict] = []

    async def _fake_transcribe(media_id, access_token, *, api_version, config, http_client):
        transcribe_calls.append(
            {
                "media_id": media_id,
                "access_token": access_token,
                "api_version": api_version,
                "config": config,
                "http_client": http_client,
            }
        )
        return TranscriptionResult(
            text="Quero marcar uma consulta amanhã",
            provider_used="openai",
            is_low_confidence=False,
            char_count=31,
        )

    monkeypatch.setattr(tasks, "transcribe_whatsapp_media", _fake_transcribe)
    send_bot_calls = _spy_send_bot_reply(monkeypatch)

    await tasks.transcribe_audio_message(
        {},
        media_id="MEDIA1",
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        message_id="wamid.AUDIO1",
        patient_name="Ana",
    )

    assert len(transcribe_calls) == 1
    assert transcribe_calls[0]["media_id"] == "MEDIA1"
    assert transcribe_calls[0]["api_version"] == get_settings().META_GRAPH_API_VERSION

    async with db() as session:
        message = await session.scalar(select(Message).where(Message.wam_id == "wamid.AUDIO1"))
        assert message is not None
        assert message.body == "Quero marcar uma consulta amanhã"
        assert message.direction == MessageDirection.INBOUND
        assert message.sender == MessageSender.PATIENT

        processed = await session.scalar(
            select(ProcessedEvent).where(ProcessedEvent.event_id == "wamid.AUDIO1")
        )
        assert processed is not None

    assert len(send_bot_calls) == 1
    reply = send_bot_calls[0]
    assert reply.inbound_body == "Quero marcar uma consulta amanhã"
    assert reply.conversation_id is not None


async def test_low_confidence_sends_clarification_no_message_persisted(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenant(db)

    async def _fake_transcribe(media_id, access_token, *, api_version, config, http_client):
        return TranscriptionResult(
            text="", provider_used="openai", is_low_confidence=True, char_count=0
        )

    monkeypatch.setattr(tasks, "transcribe_whatsapp_media", _fake_transcribe)
    simple_calls = _spy_send_simple_text(monkeypatch)
    send_bot_calls = _spy_send_bot_reply(monkeypatch)

    await tasks.transcribe_audio_message(
        {},
        media_id="MEDIA1",
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        message_id="wamid.AUDIO2",
        patient_name="Ana",
    )

    assert len(simple_calls) == 1
    to, body, client = simple_calls[0]
    assert to == WA_ID
    assert body == tasks.AUDIO_UNINTELLIGIBLE_MESSAGE
    assert client is not None
    assert send_bot_calls == []

    async with db() as session:
        message = await session.scalar(select(Message).where(Message.wam_id == "wamid.AUDIO2"))
        assert message is None
        processed = await session.scalar(
            select(ProcessedEvent).where(ProcessedEvent.event_id == "wamid.AUDIO2")
        )
        assert processed is not None


async def test_idempotency_pre_processed_event_skips_transcription(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with db() as session:
        session.add(ProcessedEvent(event_id="wamid.AUDIO3"))
        await session.commit()

    transcribe_calls: list = []

    async def _fake_transcribe(*args, **kwargs):
        transcribe_calls.append((args, kwargs))
        raise AssertionError("transcribe_whatsapp_media must not be called for a duplicate")

    monkeypatch.setattr(tasks, "transcribe_whatsapp_media", _fake_transcribe)
    simple_calls = _spy_send_simple_text(monkeypatch)
    send_bot_calls = _spy_send_bot_reply(monkeypatch)

    await tasks.transcribe_audio_message(
        {},
        media_id="MEDIA1",
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        message_id="wamid.AUDIO3",
        patient_name="Ana",
    )

    assert transcribe_calls == []
    assert simple_calls == []
    assert send_bot_calls == []


async def test_media_too_large_sends_clarification_and_marks_processed(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenant(db)

    async def _fake_transcribe(media_id, access_token, *, api_version, config, http_client):
        raise MediaTooLarge("audio is 20000000 bytes, exceeds max_bytes=16777216")

    monkeypatch.setattr(tasks, "transcribe_whatsapp_media", _fake_transcribe)
    simple_calls = _spy_send_simple_text(monkeypatch)
    send_bot_calls = _spy_send_bot_reply(monkeypatch)

    # Must return normally - no exception propagates for a permanent failure.
    await tasks.transcribe_audio_message(
        {},
        media_id="MEDIA1",
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        message_id="wamid.AUDIO4",
        patient_name="Ana",
    )

    assert len(simple_calls) == 1
    assert simple_calls[0][0] == WA_ID
    assert simple_calls[0][1] == tasks.AUDIO_UNINTELLIGIBLE_MESSAGE
    assert send_bot_calls == []

    async with db() as session:
        processed = await session.scalar(
            select(ProcessedEvent).where(ProcessedEvent.event_id == "wamid.AUDIO4")
        )
        assert processed is not None


async def test_transient_media_fetch_error_propagates_and_stays_unprocessed(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _seed_tenant(db)

    async def _fake_transcribe(media_id, access_token, *, api_version, config, http_client):
        raise MediaFetchError("WhatsApp media metadata fetch failed (status 503)")

    monkeypatch.setattr(tasks, "transcribe_whatsapp_media", _fake_transcribe)
    simple_calls = _spy_send_simple_text(monkeypatch)
    send_bot_calls = _spy_send_bot_reply(monkeypatch)

    with pytest.raises(MediaFetchError):
        await tasks.transcribe_audio_message(
            {},
            media_id="MEDIA1",
            phone_number_id=PHONE_NUMBER_ID,
            wa_id=WA_ID,
            message_id="wamid.AUDIO5",
            patient_name="Ana",
        )

    assert simple_calls == []
    assert send_bot_calls == []

    async with db() as session:
        processed = await session.scalar(
            select(ProcessedEvent).where(ProcessedEvent.event_id == "wamid.AUDIO5")
        )
        # Retry-safe: NOT marked processed, so arq's redelivery will try again.
        assert processed is None


async def test_uses_the_tenant_token_not_the_global_env(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Graph media download authenticates with the TENANT's own token."""
    await _seed_tenant(db)
    seen: dict = {}

    async def _fake_transcribe(media_id, access_token, *, api_version, config, http_client):
        seen["access_token"] = access_token
        return TranscriptionResult(
            text="quero marcar", provider_used="openai", char_count=12, is_low_confidence=False
        )

    monkeypatch.setattr(tasks, "transcribe_whatsapp_media", _fake_transcribe)
    _spy_send_bot_reply(monkeypatch)

    await tasks.transcribe_audio_message(
        {},
        media_id="MEDIA1",
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        message_id="wamid.AUDIO.token",
        patient_name="Ana",
    )

    assert seen["access_token"] == TENANT_WABA_TOKEN
    assert seen["access_token"] != get_settings().META_ACCESS_TOKEN


async def test_missing_tenant_token_skips_transcription(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail closed (PROMPT_FIX_21): no tenant token -> no Graph download, no
    STT spend, no reply. It must NOT fall back to META_ACCESS_TOKEN."""
    async with db() as session:
        tenant = Tenant(
            id=uuid4(), clinic_name="Clinic", phone_number_id=PHONE_NUMBER_ID, is_active=True
        )
        session.add(tenant)
        await session.commit()  # deliberately no WABA token

    async def _explode(*args, **kwargs):
        raise AssertionError("STT was attempted without the tenant's own token")

    monkeypatch.setattr(tasks, "transcribe_whatsapp_media", _explode)
    simple_calls = _spy_send_simple_text(monkeypatch)
    send_bot_calls = _spy_send_bot_reply(monkeypatch)

    await tasks.transcribe_audio_message(
        {},
        media_id="MEDIA1",
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        message_id="wamid.AUDIO.notoken",
        patient_name="Ana",
    )

    assert simple_calls == []
    assert send_bot_calls == []


# --------------------------------------------------------------------------
# Config split: OPENAI_MODEL (legacy) -> OPENAI_SECRETARIA_MODEL, and no
# cross-contamination into the STT config (OPENAI_TRANSCRIPT_MODEL).
# --------------------------------------------------------------------------


def test_openai_secretaria_model_accepts_legacy_alias(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_SECRETARIA_MODEL", raising=False)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.OPENAI_SECRETARIA_MODEL == "gpt-4.1"
    assert not hasattr(settings, "OPENAI_MODEL")


def test_openai_secretaria_model_new_name_wins_over_legacy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    monkeypatch.setenv("OPENAI_SECRETARIA_MODEL", "gpt-5-mini-custom")
    get_settings.cache_clear()

    settings = get_settings()

    assert settings.OPENAI_SECRETARIA_MODEL == "gpt-5-mini-custom"


def test_transcription_config_never_reads_chat_model_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "gpt-4.1")
    monkeypatch.setenv("OPENAI_SECRETARIA_MODEL", "gpt-5-mini-custom")
    monkeypatch.delenv("OPENAI_TRANSCRIPT_MODEL", raising=False)
    get_settings.cache_clear()

    config = tasks._transcription_config()

    # Default STT model, entirely unaffected by the chat-model env vars above.
    assert config.openai_model == "gpt-4o-mini-transcribe"
