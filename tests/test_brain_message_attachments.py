"""Brain-Message attachments, part 2 of 3 (secretarIA): storage, and both directions.

Pins §6 of z_prompts/PROMPT_BRAIN_MESSAGE_ANEXOS_SECRETARIA_2_SECRETARIA.md:

  * a patient's file (multipart `POST /internal/brain-message/inbound`) is stored,
    persisted with `attachment`, answered with the fixed receipt and never analysed;
    `body` is never empty;
  * a staff file (console multipart send) goes out through
    `BrainMessageSender.send_media`, never `WhatsAppClient`, is persisted and appears in
    both transcripts;
  * both media routes serve a file only inside its owner's scope - the rest is one 404;
  * a file whose content contradicts its name, of an unaccepted type, or over brain-api's
    own ceiling is refused with a clear 4xx - nothing stored, no job; consent and the
    persisted quota likewise;
  * text without a file is unchanged;
  * no log line carries the file's bytes, its name, or the storage credential.

Storage is `FakeStorage`, patched over services/media_storage.py; that module itself is
exercised against a stubbed boto3 client and httpx's MockTransport. No real bucket.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

import io  # noqa: E402
import logging  # noqa: E402
from contextlib import contextmanager  # noqa: E402
from datetime import UTC, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from botocore.exceptions import ClientError  # noqa: E402
from sqlalchemy import func, select  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.config import Settings  # noqa: E402
from secretaria.core import attachments  # noqa: E402
from secretaria.core.database import Base  # noqa: E402
from secretaria.models import (  # noqa: E402
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)
from secretaria.models.conversation import HandoverState  # noqa: E402
from secretaria.services import media_storage  # noqa: E402
from secretaria.services.channel_sender import (  # noqa: E402
    BrainMessageSender,
    ChannelSender,
    MediaChannelSender,
    sender_sends_media,
)
from secretaria.services.entitlements_client import EntitlementSummary  # noqa: E402
from secretaria.services.whatsapp import WhatsAppClient  # noqa: E402
from secretaria.workers import tasks  # noqa: E402

# The real storage functions, taken before any fixture swaps them for the fake.
REAL_PUT_OBJECT = media_storage.put_object
REAL_OPEN_OBJECT = media_storage.open_object
REAL_DELETE_OBJECT = media_storage.delete_object

GOOD_KEY = "test-internal-key"
KEY_HEADER = {"X-Internal-Api-Key": GOOD_KEY}
EXTERNAL_ID = "bm-attach-001"
SECRET = "r2-secret-that-must-never-be-logged"
PII_NAME = "Joana Silva - exame de sangue.pdf"
MAX = attachments.MAX_ATTACHMENT_BYTES
HTML = b"<!doctype html><script>alert(1)</script>"


def _pdf(size: int = 300) -> bytes:
    return b"%PDF-1.7\n" + b"0" * (size - 9)


def _png(size: int = 128) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + b"\x00" * (size - 8)


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


class FakeStorage:
    """The bucket, in memory."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.deleted: list[str] = []

    async def put_object(self, key, body, content_type):
        body.seek(0)
        self.objects[key] = (body.read(), content_type)

    async def delete_object(self, key):
        self.deleted.append(key)
        self.objects.pop(key, None)

    async def open_object(self, key, *, transport=None):
        if key not in self.objects:
            raise media_storage.MediaObjectMissing(key)
        data = self.objects[key][0]

        async def chunks():
            yield data

        async def aclose():
            return None

        return media_storage.MediaStream(chunks=chunks(), content_length=len(data), aclose=aclose)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr(media_storage, "put_object", fake.put_object)
    monkeypatch.setattr(media_storage, "delete_object", fake.delete_object)
    monkeypatch.setattr(media_storage, "open_object", fake.open_object)
    return fake


class _ExplodingWhatsAppClient:
    """Any use fails the test: no attachment path may touch the Graph API."""

    @classmethod
    def for_tenant(cls, tenant, access_token):
        raise AssertionError("an attachment path built a WhatsApp client")

    @classmethod
    def for_dev_scaffold(cls, settings=None):
        raise AssertionError("an attachment path built a WhatsApp client")


class _FakeArqPool:
    def __init__(self) -> None:
        self.jobs: list[tuple] = []
        self.fail = False

    async def enqueue_job(self, name, *args, **kwargs):
        if self.fail:
            raise RuntimeError("redis down")
        self.jobs.append((name, args, kwargs))


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
    monkeypatch.setattr(tasks, "WhatsAppClient", _ExplodingWhatsAppClient)


@pytest_asyncio.fixture
async def api(db, monkeypatch: pytest.MonkeyPatch, storage: FakeStorage):
    """The real app on the in-memory DB, a recording queue, the fake bucket, and a hub
    token whose tenant the test picks (`api.acting["tenant"]`)."""
    from secretaria.api.hub import conversations as hub_conversations
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
    pool = _FakeArqPool()
    app.state.arq_pool = pool
    monkeypatch.setattr(hub_conversations, "WhatsAppClient", _ExplodingWhatsAppClient)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield SimpleNamespace(client=client, pool=pool, acting=acting, storage=storage)
    app.dependency_overrides.clear()


async def _seed_tenant(db) -> Tenant:
    async with db() as session:
        tenant = Tenant(
            id=uuid4(),
            clinic_name="Clinic",
            phone_number_id=uuid4().hex[:12],
            is_active=True,
            clinic_description="Oftalmologia.",
            initial_flows={},
        )
        session.add(tenant)
        await session.commit()
        await session.refresh(tenant)
        return tenant


async def _seed_patient(
    db,
    tenant: Tenant,
    *,
    external_id: str = EXTERNAL_ID,
    channel: str = "brain_message",
    consented: bool = True,
    handover: HandoverState = HandoverState.BOT_ACTIVE,
) -> tuple[Patient, Conversation]:
    whatsapp = channel == "whatsapp"
    async with db() as session:
        patient = Patient(
            tenant_id=tenant.id,
            channel=channel,
            external_id="5511999999999" if whatsapp else external_id,
            wa_id="5511999999999" if whatsapp else None,
            name="Maria",
            lgpd_accepted_at=datetime.now(UTC) if consented else None,
        )
        session.add(patient)
        await session.flush()
        conversation = Conversation(
            tenant_id=tenant.id, patient_id=patient.id, handover_state=handover
        )
        session.add(conversation)
        await session.commit()
        await session.refresh(patient)
        await session.refresh(conversation)
        return patient, conversation


async def _rows(db, conversation_id) -> list[Message]:
    async with db() as session:
        return list(
            (
                await session.scalars(
                    select(Message).where(Message.conversation_id == conversation_id)
                )
            ).all()
        )


async def _post_file(
    api,
    tenant: Tenant,
    content: bytes,
    filename: str,
    *,
    external_id: str = EXTERNAL_ID,
    text: str | None = None,
    part: str = "file",
    extra: dict | None = None,
    headers: dict | None = KEY_HEADER,
):
    data = {"tenant_id": str(tenant.id), "external_id": external_id, **(extra or {})}
    if text is not None:
        data["text"] = text
    return await api.client.post(
        "/internal/brain-message/inbound",
        headers=headers,
        data=data,
        files={part: (filename, content, "application/octet-stream")},
    )


async def _run_jobs(api) -> None:
    """Run what the endpoint enqueued, exactly as arq would."""
    for name, args, kwargs in api.pool.jobs:
        assert name == "process_brain_message_inbound"
        await tasks.process_brain_message_inbound({}, *args, **kwargs)
    api.pool.jobs.clear()


def _staff_send(api, conversation_id, **kwargs):
    return api.client.post(f"/tenants/me/conversations/{conversation_id}/messages", **kwargs)


# ----------------------------------------------------------------- pure: the contract


def test_the_limits_are_brain_apis_byte_for_byte() -> None:
    assert attachments.MAX_ATTACHMENT_BYTES == 20_971_520
    assert attachments.MULTIPART_OVERHEAD_BYTES == 64 * 1024
    assert set(attachments.ALLOWED_KINDS) == {
        "image/jpeg",
        "image/png",
        "image/webp",
        "image/gif",
        "application/pdf",
    }
    brain_api = {
        "attachment_empty": 422,
        "attachment_too_large": 413,
        "attachment_type_unsupported": 415,
        "attachment_type_mismatch": 422,
        "attachment_malformed": 422,
        "attachment_not_found": 404,
    }
    assert {code: attachments.REFUSALS[code][0] for code in brain_api} == brain_api


def test_sending_a_file_is_a_capability_whatsapp_does_not_grow() -> None:
    whatsapp = WhatsAppClient(phone_number_id="1", access_token="t")
    assert isinstance(whatsapp, ChannelSender)  # the seam's founding claim still holds
    assert not sender_sends_media(whatsapp)
    assert not hasattr(WhatsAppClient, "send_media")  # decision 5
    brain_message = BrainMessageSender(conversation_id=uuid4(), session_factory=None)
    assert isinstance(brain_message, MediaChannelSender)
    assert sender_sends_media(brain_message)


# --------------------------------------------------- 1. a patient's file, end to end


async def test_a_patient_file_is_stored_persisted_and_only_acknowledged(api, db) -> None:
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_patient(db, tenant)

    response = await _post_file(api, tenant, _pdf(), "exame.pdf")
    assert response.status_code == 202, response.text
    assert response.json() == {"status": "queued"}

    # The bytes are in the bucket - stored by the API, under this patient's prefix -
    # and the job carries only a reference to them.
    ((key, stored),) = api.storage.objects.items()
    assert stored == (_pdf(), "application/pdf")
    assert key.startswith(f"brain-message/{tenant.id}/{patient.id}/")
    ((_, _, kwargs),) = api.pool.jobs
    assert kwargs["attachment"] == {
        "r2_object_key": key,
        "content_type": "application/pdf",
        "size_bytes": 300,
        "filename": "exame.pdf",
    }

    await _run_jobs(api)
    rows = await _rows(db, conversation.id)
    inbound = [r for r in rows if r.direction == MessageDirection.INBOUND]
    outbound = [r for r in rows if r.direction == MessageDirection.OUTBOUND]
    assert len(inbound) == 1
    assert inbound[0].attachment["r2_object_key"] == key
    # Decision 4: a file without a caption still has a body.
    assert inbound[0].body == "[anexo: exame.pdf]"
    # Decision 3: the whole answer is the fixed receipt - nothing reads the content.
    assert [(r.sender, r.body, r.attachment) for r in outbound] == [
        (MessageSender.BOT, tasks.ATTACHMENT_RECEIVED_MESSAGE, None)
    ]
    async with db() as session:
        after = await session.get(Conversation, conversation.id)
    assert after.flow_state == conversation.flow_state


async def test_the_caption_is_the_body_and_the_listing_shows_the_file_never_its_key(
    api, db
) -> None:
    tenant = await _seed_tenant(db)
    await _seed_patient(db, tenant)
    response = await _post_file(api, tenant, _png(), "foto.png", text="segue a foto")
    assert response.status_code == 202, response.text
    await _run_jobs(api)

    listing = await api.client.get(
        f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages",
        params={"tenant_id": str(tenant.id)},
        headers=KEY_HEADER,
    )
    assert listing.status_code == 200, listing.text
    rows = listing.json()["data"]
    assert [(r["body"], r["attachment"]) for r in rows if r["direction"] == "inbound"] == [
        ("segue a foto", {"content_type": "image/png", "size_bytes": 128, "filename": "foto.png"})
    ]
    assert all(r["attachment"] is None for r in rows if r["direction"] == "outbound")
    (key,) = api.storage.objects
    assert key not in listing.text
    assert "r2_object_key" not in listing.text


async def test_with_a_human_in_charge_the_file_is_recorded_and_the_bot_stays_quiet(api, db) -> None:
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_patient(db, tenant, handover=HandoverState.HUMAN_ACTIVE)
    assert (await _post_file(api, tenant, _png(), "foto.png")).status_code == 202
    await _run_jobs(api)
    rows = await _rows(db, conversation.id)
    assert [(r.direction, r.attachment is not None) for r in rows] == [
        (MessageDirection.INBOUND, True)
    ]


# ----------------------------------------------------- 2. a staff file, end to end


async def test_a_staff_file_goes_out_through_send_media_and_lands_in_both_transcripts(
    api, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _seed_tenant(db)
    api.acting["tenant"] = tenant
    patient, conversation = await _seed_patient(db, tenant)
    calls = []
    original = BrainMessageSender.send_media

    async def spy(self, to, **kwargs):
        calls.append((to, kwargs["attachment"].kind.content_type, kwargs["caption"]))
        return await original(self, to, **kwargs)

    monkeypatch.setattr(BrainMessageSender, "send_media", spy)

    response = await _staff_send(
        api,
        conversation.id,
        data={"body": "Seu laudo"},
        files={"file": ("laudo.pdf", _pdf(), "application/pdf")},
    )
    assert response.status_code == 200, response.text
    sent = response.json()
    assert (sent["direction"], sent["sender"], sent["body"]) == ("outbound", "human", "Seu laudo")
    assert sent["attachment"] == {
        "content_type": "application/pdf",
        "size_bytes": 300,
        "filename": "laudo.pdf",
    }
    assert calls == [(EXTERNAL_ID, "application/pdf", "Seu laudo")]
    ((key, (data, _)),) = api.storage.objects.items()
    assert data == _pdf()
    assert key.startswith(f"brain-message/{tenant.id}/{patient.id}/")

    # Without a caption, the placeholder - never an empty body.
    bare = await _staff_send(
        api, conversation.id, files={"file": ("foto.png", _png(), "image/png")}
    )
    assert bare.status_code == 200, bare.text
    assert bare.json()["body"] == "[anexo: foto.png]"

    history = await api.client.get(f"/tenants/me/conversations/{conversation.id}/messages")
    by_id = {m["id"]: m["attachment"] for m in history.json()}
    assert by_id[sent["id"]] == sent["attachment"]
    portal = await api.client.get(
        f"/internal/brain-message/conversations/{EXTERNAL_ID}/messages",
        params={"tenant_id": str(tenant.id)},
        headers=KEY_HEADER,
    )
    assert sent["attachment"] in [m["attachment"] for m in portal.json()["data"]]
    async with db() as session:
        after = await session.get(Conversation, conversation.id)
    assert after.handover_state == HandoverState.HUMAN_ACTIVE


async def test_a_file_for_a_whatsapp_patient_is_refused_and_nothing_is_stored(api, db) -> None:
    tenant = await _seed_tenant(db)
    api.acting["tenant"] = tenant
    _, conversation = await _seed_patient(db, tenant, channel="whatsapp")
    response = await _staff_send(
        api, conversation.id, files={"file": ("foto.png", _png(), "image/png")}
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "attachment_unsupported_for_channel"
    assert api.storage.objects == {}
    assert await _rows(db, conversation.id) == []


async def test_staff_text_without_a_file_is_unchanged(api, db) -> None:
    tenant = await _seed_tenant(db)
    api.acting["tenant"] = tenant
    _, conversation = await _seed_patient(db, tenant)
    ok = await _staff_send(api, conversation.id, json={"body": "oi"})
    assert ok.status_code == 200, ok.text
    assert (ok.json()["body"], ok.json()["attachment"]) == ("oi", None)
    empty = await _staff_send(api, conversation.id, json={"body": ""})
    assert empty.status_code == 422
    assert empty.json()["detail"][0]["loc"] == ["body", "body"]
    assert api.storage.objects == {}


# ------------------------------------------------------ 3. media only for its owner


async def test_internal_media_is_served_only_inside_the_owners_conversation(api, db) -> None:
    tenant = await _seed_tenant(db)
    other = await _seed_tenant(db)
    await _seed_patient(db, tenant)
    await _seed_patient(db, tenant, external_id="bm-neighbour")
    await _seed_patient(db, other)  # the very same handle, under another clinic
    assert (await _post_file(api, tenant, _png(), "foto.png")).status_code == 202
    await _run_jobs(api)
    async with db() as session:
        rows = (await session.execute(select(Message.id, Message.attachment))).all()
    file_id = next(r.id for r in rows if r.attachment is not None)
    receipt_id = next(r.id for r in rows if r.attachment is None)

    def media(message_id, tenant_id, external_id, headers=KEY_HEADER):
        return api.client.get(
            f"/internal/brain-message/media/{message_id}",
            params={"tenant_id": str(tenant_id), "external_id": external_id},
            headers=headers,
        )

    ok = await media(file_id, tenant.id, EXTERNAL_ID)
    assert ok.status_code == 200, ok.text
    assert ok.content == _png()
    assert ok.headers["content-type"] == "image/png"
    assert ok.headers["x-content-type-options"] == "nosniff"
    assert ok.headers["content-disposition"] == 'inline; filename="anexo.png"'

    refused = [
        await media(file_id, tenant.id, "bm-neighbour"),  # another patient, same clinic
        await media(file_id, other.id, EXTERNAL_ID),  # same handle, another clinic
        await media(uuid4(), tenant.id, EXTERNAL_ID),  # no such message
        await media("not-a-uuid", tenant.id, EXTERNAL_ID),
        await media(receipt_id, tenant.id, EXTERNAL_ID),  # a message without a file
    ]
    assert [r.status_code for r in refused] == [404] * 5
    assert len({r.text for r in refused}) == 1
    assert refused[0].json()["detail"]["code"] == "attachment_not_found"
    assert (await media(file_id, tenant.id, EXTERNAL_ID, headers={})).status_code == 401


async def test_staff_media_is_scoped_to_the_hub_tenant_and_the_conversation(api, db) -> None:
    tenant = await _seed_tenant(db)
    other = await _seed_tenant(db)
    api.acting["tenant"] = tenant
    _, conversation = await _seed_patient(db, tenant)
    _, neighbour = await _seed_patient(db, tenant, external_id="bm-neighbour")
    sent = await _staff_send(
        api, conversation.id, files={"file": ("laudo.pdf", _pdf(), "application/pdf")}
    )
    message_id = sent.json()["id"]
    path = f"/tenants/me/conversations/{conversation.id}/messages/{message_id}/media"

    ok = await api.client.get(path)
    assert ok.status_code == 200, ok.text
    assert ok.content == _pdf()
    assert ok.headers["content-type"] == "application/pdf"
    assert ok.headers["content-disposition"] == 'attachment; filename="anexo.pdf"'
    assert ok.headers["content-security-policy"] == "default-src 'none'; sandbox"

    wrong_conversation = await api.client.get(
        f"/tenants/me/conversations/{neighbour.id}/messages/{message_id}/media"
    )
    api.acting["tenant"] = other  # another clinic's hub token
    wrong_tenant = await api.client.get(path)
    api.acting["tenant"] = tenant
    malformed = await api.client.get(
        f"/tenants/me/conversations/{conversation.id}/messages/nope/media"
    )
    refused = [wrong_conversation, wrong_tenant, malformed]
    assert [r.status_code for r in refused] == [404, 404, 404]
    assert len({r.text for r in refused}) == 1


# ------------------------------------------ 4. refusals: clear 4xx, nothing stored


@pytest.mark.parametrize(
    ("content", "filename", "status", "code"),
    [
        (_pdf(), "exame.jpg", 422, "attachment_type_mismatch"),
        (_png(), "programa.exe", 422, "attachment_type_mismatch"),
        (HTML, "foto.png", 415, "attachment_type_unsupported"),
        (b"", "vazio.pdf", 422, "attachment_empty"),
    ],
    ids=["pdf-named-jpg", "png-named-exe", "html-named-png", "empty"],
)
async def test_a_file_that_is_not_what_it_claims_is_refused_and_nothing_is_stored(
    api, db, content, filename, status, code
) -> None:
    tenant = await _seed_tenant(db)
    await _seed_patient(db, tenant)
    response = await _post_file(api, tenant, content, filename)
    assert response.status_code == status, response.text
    assert response.json()["detail"]["code"] == code
    assert api.storage.objects == {}
    assert api.pool.jobs == []


async def test_the_ceiling_is_brain_apis_twenty_mib_inclusive(api, db) -> None:
    tenant = await _seed_tenant(db)
    await _seed_patient(db, tenant)
    at_ceiling = await _post_file(api, tenant, _pdf(MAX), "grande.pdf")
    assert at_ceiling.status_code == 202, at_ceiling.text
    one_over = await _post_file(api, tenant, _pdf(MAX + 1), "grande.pdf")
    assert one_over.status_code == 413
    assert one_over.json()["detail"] == {
        "code": "attachment_too_large",
        "message": attachments.REFUSALS["attachment_too_large"][1],
        "max_bytes": 20_971_520,
    }
    # A body past the whole-request cap is refused on its declared length, unparsed.
    body_over = await _post_file(
        api, tenant, _pdf(MAX + attachments.MULTIPART_OVERHEAD_BYTES), "grande.pdf"
    )
    assert body_over.status_code == 413
    assert len(api.storage.objects) == 1
    assert len(api.pool.jobs) == 1


async def test_a_malformed_or_unauthenticated_upload_is_refused_before_storage(api, db) -> None:
    tenant = await _seed_tenant(db)
    await _seed_patient(db, tenant)
    wrong_part = await _post_file(api, tenant, _png(), "foto.png", part="arquivo")
    assert wrong_part.status_code == 422
    assert wrong_part.json()["detail"]["code"] == "attachment_malformed"
    unknown_field = await _post_file(api, tenant, _png(), "foto.png", extra={"clinic": "x"})
    assert unknown_field.status_code == 422
    assert unknown_field.json()["detail"][0]["type"] == "extra_forbidden"
    # The multipart variant takes no idempotency key (brain-api §4.1 sends none).
    replayable = await _post_file(api, tenant, _png(), "foto.png", extra={"dedupe_id": "d-1"})
    assert replayable.status_code == 422
    assert replayable.json()["detail"][0]["loc"] == ["body", "dedupe_id"]
    anonymous = await _post_file(api, tenant, _png(), "foto.png", headers={})
    assert anonymous.status_code == 401
    assert api.storage.objects == {}
    assert api.pool.jobs == []


async def test_a_file_before_the_lgpd_terms_is_refused_and_nothing_is_stored(api, db) -> None:
    tenant = await _seed_tenant(db)
    await _seed_patient(db, tenant, consented=False)
    not_yet = await _post_file(api, tenant, _png(), "foto.png")
    stranger = await _post_file(api, tenant, _png(), "foto.png", external_id="bm-never-seen")
    for response in (not_yet, stranger):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "attachment_consent_required"
    assert api.storage.objects == {}
    assert api.pool.jobs == []


async def test_the_daily_quota_is_persisted_per_patient_and_per_clinic(
    api, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.api import internal as internal_api

    monkeypatch.setattr(
        internal_api,
        "get_settings",
        lambda: Settings(
            INTERNAL_API_KEY=GOOD_KEY,
            ATTACHMENT_DAILY_BYTES_PER_PATIENT=1000,
            ATTACHMENT_DAILY_BYTES_PER_TENANT=1500,
        ),
    )
    tenant = await _seed_tenant(db)
    _, first = await _seed_patient(db, tenant)
    _, second = await _seed_patient(db, tenant, external_id="bm-second")
    now = datetime.now(UTC)

    def row(conversation, size, *, hours_ago=0, staff=False):
        return Message(
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND if staff else MessageDirection.INBOUND,
            sender=MessageSender.HUMAN if staff else MessageSender.PATIENT,
            body="[anexo: a.png]",
            attachment={
                "r2_object_key": f"k-{uuid4().hex}",
                "content_type": "image/png",
                "size_bytes": size,
                "filename": "a.png",
            },
            created_at=now - timedelta(hours=hours_ago),
        )

    async with db() as session:
        session.add_all(
            [
                row(first, 700, hours_ago=2),
                row(first, 5000, hours_ago=30),  # outside the window: no longer counts
                row(first, 5000, staff=True),  # staff's upload: never counts
                row(second, 600),
            ]
        )
        await session.commit()

    fits = await _post_file(api, tenant, _png(128), "foto.png")  # 828 / 1428
    assert fits.status_code == 202, fits.text
    over_patient = await _post_file(api, tenant, _png(400), "foto.png")  # 1100 > 1000
    over_clinic = await _post_file(  # patient 900 fits; clinic 1600 > 1500
        api, tenant, _png(300), "foto.png", external_id="bm-second"
    )
    for response in (over_patient, over_clinic):
        assert response.status_code == 429, response.text
        assert response.json()["detail"]["code"] == "attachment_quota_exceeded"
    assert len(api.storage.objects) == 1
    assert len(api.pool.jobs) == 1


async def test_storage_trouble_is_a_retryable_503_and_a_failed_enqueue_cleans_up(
    api, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    tenant = await _seed_tenant(db)
    await _seed_patient(db, tenant)

    async def _down(key, body, content_type):
        raise media_storage.MediaStorageUnavailable("down")

    with monkeypatch.context() as patched:
        patched.setattr(media_storage, "put_object", _down)
        down = await _post_file(api, tenant, _png(), "foto.png")
    assert down.status_code == 503
    assert down.json()["detail"]["code"] == "attachment_storage_unavailable"
    assert api.pool.jobs == []

    api.pool.fail = True
    with pytest.raises(RuntimeError):
        await _post_file(api, tenant, _png(), "foto.png")
    assert api.storage.objects == {}
    assert len(api.storage.deleted) == 1


# -------------------------------------------------------- 5. text is unchanged


async def test_text_without_a_file_travels_exactly_as_before(api, db) -> None:
    tenant = await _seed_tenant(db)
    response = await api.client.post(
        "/internal/brain-message/inbound",
        headers=KEY_HEADER,
        json={"tenant_id": str(tenant.id), "external_id": EXTERNAL_ID, "text": "oi"},
    )
    assert response.status_code == 202, response.text
    # The very job a worker that predates files can run: no `attachment` argument.
    assert api.pool.jobs == [
        (
            "process_brain_message_inbound",
            (str(tenant.id), EXTERNAL_ID),
            {"text": "oi", "patient_name": None, "dedupe_id": None, "interactive_reply_id": None},
        )
    ]
    await _run_jobs(api)
    async with db() as session:
        total = await session.scalar(select(func.count()).select_from(Message))
        null = await session.scalar(
            select(func.count()).select_from(Message).where(Message.attachment.is_(None))
        )
    # SQL NULL, never JSON 'null': the quota index's predicate depends on it.
    assert total >= 1
    assert null == total

    bad = await api.client.post(
        "/internal/brain-message/inbound",
        headers=KEY_HEADER,
        json={"tenant_id": "not-a-uuid", "external_id": EXTERNAL_ID},
    )
    assert bad.status_code == 422
    assert bad.json()["detail"][0]["loc"][0] == "body"
    assert api.storage.objects == {}


# ----------------------------------------- 6. no file name, byte or secret in a log


class _RecordingLogger:
    """Stands in for a module's structlog logger and keeps every call it gets."""

    def __init__(self, sink: list[dict]) -> None:
        self._sink = sink

    def bind(self, **kwargs):
        return self

    def __getattr__(self, level: str):
        def emit(event, *args, **kwargs):
            self._sink.append({"event": event, "level": level, "args": args, **kwargs})

        return emit


@contextmanager
def _recording_loggers(monkeypatch: pytest.MonkeyPatch):
    """Every logger an attachment path writes through, recorded directly: the app's
    structlog caches loggers on first use, which `structlog.testing.capture_logs` can't see."""
    from secretaria.api import internal as internal_api
    from secretaria.api.hub import conversations as hub_conversations
    from secretaria.services import channel_sender

    events: list[dict] = []
    for module in (
        internal_api,
        hub_conversations,
        media_storage,
        attachments,
        channel_sender,
        tasks,
    ):
        monkeypatch.setattr(module, "logger", _RecordingLogger(events))
    yield events


async def test_no_log_line_carries_the_file_name_its_bytes_or_the_storage_secret(
    api, db, monkeypatch: pytest.MonkeyPatch, caplog, capsys
) -> None:
    tenant = await _seed_tenant(db)
    api.acting["tenant"] = tenant
    _, conversation = await _seed_patient(db, tenant)
    marker = b"%PDF-1.7\nBYTES-THAT-MUST-NOT-BE-LOGGED" + b"0" * 64
    # The level the service runs at. Below it, the TEST driver (aiosqlite) traces every
    # SQL parameter - message bodies included - which production's asyncpg never logs.
    caplog.set_level(logging.INFO)

    with _recording_loggers(monkeypatch) as events:
        assert (await _post_file(api, tenant, marker, PII_NAME)).status_code == 202
        refused = await _post_file(api, tenant, _pdf(), "Joana Silva.jpg")
        assert refused.status_code == 422
        await _run_jobs(api)
        sent = await _staff_send(
            api, conversation.id, files={"file": (PII_NAME, marker, "application/pdf")}
        )
        assert sent.status_code == 200, sent.text
        media = await api.client.get(
            f"/tenants/me/conversations/{conversation.id}/messages/{sent.json()['id']}/media"
        )
        assert media.status_code == 200

        # The REAL storage module, failing, with a real secret configured and an error
        # whose own message repeats it.
        monkeypatch.setattr(
            media_storage,
            "get_settings",
            lambda: Settings(
                ATTACHMENTS_R2_ACCOUNT_ID="acct",
                ATTACHMENTS_R2_ACCESS_KEY_ID="AKID",
                ATTACHMENTS_R2_SECRET_ACCESS_KEY=SECRET,
                ATTACHMENTS_R2_BUCKET="bucket",
            ),
        )
        monkeypatch.setattr(media_storage, "_client", None)

        class _Denied:
            def put_object(self, **kwargs):
                raise ClientError(
                    {"Error": {"Code": "AccessDenied", "Message": f"bad key {SECRET}"}},
                    "PutObject",
                )

        monkeypatch.setattr(media_storage.boto3, "client", lambda *a, **k: _Denied())
        with pytest.raises(media_storage.MediaStorageUnavailable):
            await REAL_PUT_OBJECT("brain-message/t/p/k", io.BytesIO(marker), "application/pdf")

    names = {event["event"] for event in events}
    assert {
        "brain_message_inbound_queued",
        "brain_message_attachment_refused",
        "hub_conversation_message_sent",
        "media_storage_put_failed",
    } <= names
    everything = repr(events) + caplog.text + "".join(capsys.readouterr())
    for leak in ("Joana", "exame de sangue", "BYTES-THAT-MUST-NOT-BE-LOGGED", SECRET):
        assert leak not in everything, leak


# ------------------------------------------- the storage module, against stubs only


async def test_storage_uses_its_own_bucket_and_streams_back_without_handing_out_a_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(
        ATTACHMENTS_R2_ACCOUNT_ID="acct123",
        ATTACHMENTS_R2_ACCESS_KEY_ID="AKID",
        ATTACHMENTS_R2_SECRET_ACCESS_KEY=SECRET,
        ATTACHMENTS_R2_BUCKET="secretaria-anexos",
        ATTACHMENTS_R2_SIGNED_URL_TTL_SECONDS=45,
    )
    monkeypatch.setattr(media_storage, "get_settings", lambda: settings)
    monkeypatch.setattr(media_storage, "_client", None)

    class _FakeS3:
        def __init__(self) -> None:
            self.puts: list[dict] = []
            self.deletes: list[dict] = []

        def put_object(self, **kwargs):
            self.puts.append({**kwargs, "Body": kwargs["Body"].read()})

        def delete_object(self, **kwargs):
            self.deletes.append(kwargs)

        def generate_presigned_url(self, operation, Params, ExpiresIn):  # noqa: N803
            return (
                f"https://acct123.r2.cloudflarestorage.com/{Params['Bucket']}/{Params['Key']}"
                f"?X-Amz-Expires={ExpiresIn}&X-Amz-Signature=abc"
            )

    s3 = _FakeS3()
    built: dict = {}

    def _client(service, **kwargs):
        built.update(kwargs, service=service)
        return s3

    monkeypatch.setattr(media_storage.boto3, "client", _client)

    await REAL_PUT_OBJECT("brain-message/t/p/k1", io.BytesIO(_pdf()), "application/pdf")
    assert s3.puts == [
        {
            "Bucket": "secretaria-anexos",
            "Key": "brain-message/t/p/k1",
            "Body": _pdf(),
            "ContentType": "application/pdf",
        }
    ]
    assert built["service"] == "s3"
    assert built["endpoint_url"] == "https://acct123.r2.cloudflarestorage.com"
    assert built["config"].signature_version == "s3v4"

    fetched: list[str] = []

    def _bucket(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if "missing" in request.url.path:
            return httpx.Response(404)
        if "broken" in request.url.path:
            return httpx.Response(500)
        return httpx.Response(200, content=_pdf(), headers={"content-length": "300"})

    transport = httpx.MockTransport(_bucket)
    stream = await REAL_OPEN_OBJECT("brain-message/t/p/k1", transport=transport)
    assert stream.content_length == 300
    assert b"".join([chunk async for chunk in stream.chunks]) == _pdf()
    assert "X-Amz-Expires=45" in fetched[0]
    with pytest.raises(media_storage.MediaObjectMissing):
        await REAL_OPEN_OBJECT("brain-message/t/p/missing", transport=transport)
    with pytest.raises(media_storage.MediaStorageUnavailable):
        await REAL_OPEN_OBJECT("brain-message/t/p/broken", transport=transport)

    def _offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    with pytest.raises(media_storage.MediaStorageUnavailable):
        await REAL_OPEN_OBJECT("brain-message/t/p/k1", transport=httpx.MockTransport(_offline))

    await REAL_DELETE_OBJECT("brain-message/t/p/k1")
    assert s3.deletes == [{"Bucket": "secretaria-anexos", "Key": "brain-message/t/p/k1"}]

    # Unconfigured: fails closed, and never builds a client.
    monkeypatch.setattr(media_storage, "_client", None)
    monkeypatch.setattr(
        media_storage,
        "get_settings",
        lambda: Settings(
            ATTACHMENTS_R2_ACCOUNT_ID="",
            ATTACHMENTS_R2_ACCESS_KEY_ID="",
            ATTACHMENTS_R2_SECRET_ACCESS_KEY="",
            ATTACHMENTS_R2_BUCKET="",
        ),
    )
    built.clear()
    with pytest.raises(media_storage.MediaStorageUnavailable):
        await REAL_PUT_OBJECT("k", io.BytesIO(b"x"), "image/png")
    assert built == {}


# ------------------------------------- review fixes: nothing left behind, however it ends


async def test_a_dropped_download_still_releases_the_storage_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ASGI >= 2.4: the socket dies mid-stream, Starlette raises ClientDisconnect before its
    `background` would run - the connection to the bucket is released anyway."""
    from starlette.requests import ClientDisconnect

    from secretaria.api.attachment_http import stream_attachment

    released: list[bool] = []

    async def chunks():
        for part in (_png(8), b"a" * 10, b"b" * 10):
            yield part

    async def release():
        released.append(True)

    async def fake_open(key, *, transport=None):
        return media_storage.MediaStream(chunks=chunks(), content_length=28, aclose=release)

    monkeypatch.setattr(media_storage, "open_object", fake_open)
    record = attachments.StoredAttachment(
        r2_object_key="brain-message/t/p/k",
        content_type="image/png",
        size_bytes=28,
        filename="a.png",
    )
    response = await stream_attachment(record)
    delivered: list[bytes] = []

    async def send(message):
        if message["type"] == "http.response.body":
            if delivered:
                raise OSError("the client went away")
            delivered.append(message["body"])

    async def receive():
        return {"type": "http.disconnect"}

    with pytest.raises(ClientDisconnect):
        await response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
    assert delivered == [_png(8)]
    assert released == [True]


async def test_a_fetch_cancelled_in_flight_closes_its_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    monkeypatch.setattr(
        media_storage,
        "get_settings",
        lambda: Settings(
            ATTACHMENTS_R2_ACCOUNT_ID="acct",
            ATTACHMENTS_R2_ACCESS_KEY_ID="AKID",
            ATTACHMENTS_R2_SECRET_ACCESS_KEY=SECRET,
            ATTACHMENTS_R2_BUCKET="bucket",
        ),
    )
    monkeypatch.setattr(media_storage, "_client", None)

    class _Presigner:
        def generate_presigned_url(self, operation, Params, ExpiresIn):  # noqa: N803
            return "https://acct.r2.cloudflarestorage.com/bucket/k"

    monkeypatch.setattr(media_storage.boto3, "client", lambda *a, **k: _Presigner())
    clients: list[httpx.AsyncClient] = []
    real_client = httpx.AsyncClient

    class _TrackedClient(real_client):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            clients.append(self)

    monkeypatch.setattr(media_storage.httpx, "AsyncClient", _TrackedClient)

    async def _cancelled(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await REAL_OPEN_OBJECT("brain-message/t/p/k", transport=httpx.MockTransport(_cancelled))
    assert len(clients) == 1
    assert clients[0].is_closed


async def test_a_file_the_worker_cannot_record_is_reported_with_its_key(
    db, monkeypatch: pytest.MonkeyPatch
) -> None:
    events: list[dict] = []
    monkeypatch.setattr(tasks, "logger", _RecordingLogger(events))
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_patient(db, tenant)

    def record(key: str) -> dict:
        return {
            "r2_object_key": key,
            "content_type": "image/png",
            "size_bytes": 128,
            "filename": "foto.png",
        }

    # A replayed idempotency key: the first job writes its row, the replay is dropped.
    first = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id,
        external_id=EXTERNAL_ID,
        text=None,
        dedupe_id="d-1",
        attachment=record("brain-message/k-first"),
    )
    assert first is not None
    replay = await tasks._persist_brain_message_inbound(
        tenant_id=tenant.id,
        external_id=EXTERNAL_ID,
        text=None,
        dedupe_id="d-1",
        attachment=record("brain-message/k-replay"),
    )
    gone = await tasks._persist_brain_message_inbound(
        tenant_id=uuid4(),
        external_id=EXTERNAL_ID,
        text=None,
        attachment=record("brain-message/k-gone"),
    )
    assert replay is None
    assert gone is None
    discarded = [
        (e["reason"], e["r2_object_key"])
        for e in events
        if e["event"] == "brain_message_attachment_discarded"
    ]
    assert discarded == [
        ("duplicate", "brain-message/k-replay"),
        ("tenant_unresolved", "brain-message/k-gone"),
    ]
    rows = await _rows(db, conversation.id)
    assert [r.attachment["r2_object_key"] for r in rows if r.attachment] == [
        "brain-message/k-first"
    ]


async def test_a_failed_staff_commit_removes_the_uploaded_file(
    api, db, monkeypatch: pytest.MonkeyPatch
) -> None:
    from secretaria.api.hub import conversations as hub_conversations

    tenant = await _seed_tenant(db)
    api.acting["tenant"] = tenant
    _, conversation = await _seed_patient(db, tenant)

    async def _database_went_away(self, conversation):
        raise RuntimeError("database went away")

    monkeypatch.setattr(hub_conversations.HandoverManager, "set_human_active", _database_went_away)
    with pytest.raises(RuntimeError):
        await _staff_send(
            api, conversation.id, files={"file": ("laudo.pdf", _pdf(), "application/pdf")}
        )
    assert api.storage.objects == {}
    assert len(api.storage.deleted) == 1
    assert await _rows(db, conversation.id) == []
