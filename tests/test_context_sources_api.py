"""Hub routes for context sources (TASK-026): create (url/text/file), list, detail, delete, quotas.

Same db / tenant / dependency-override pattern as tests/test_clinic_facts_hub.py.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")

import io  # noqa: E402
import zipfile  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from unittest.mock import AsyncMock  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from fastapi import Depends  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool  # noqa: E402

from secretaria.api.hub.deps import get_current_tenant  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.main import app  # noqa: E402
from secretaria.models import Tenant  # noqa: E402
from secretaria.models.context_source import ContextSource  # noqa: E402
from secretaria.services import text_extract  # noqa: E402


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
        t = Tenant(id=uuid4(), clinic_name="Clinic", phone_number_id=None)
        session.add(t)
        await session.commit()
        await session.refresh(t)
        return t


@pytest.fixture(autouse=True)
def _override(db, tenant):
    async def _fake_get_session():
        async with db() as session:
            yield session

    # Same ORM instance/session as the route's own `Depends(get_session)`, like production.
    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, tenant.id)

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


BASE = "/tenants/me/context-sources"
TEXT = "Estacionamento gratuito para pacientes na garagem do prédio. " * 3
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _docx_bytes(text: str) -> bytes:
    xml = (
        '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/'
        f'wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("word/document.xml", xml)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _arq_pool():
    pool = AsyncMock()
    app.state.arq_pool = pool
    yield pool
    app.state.arq_pool = None


def _limits(monkeypatch, per_day=99, in_flight=99):
    monkeypatch.setattr(
        "secretaria.api.hub.context_sources.get_settings",
        lambda: SimpleNamespace(
            CONTEXT_SOURCES_PER_DAY=per_day, CONTEXT_SOURCES_IN_FLIGHT=in_flight
        ),
    )


async def test_a_text_source_is_accepted_stored_and_enqueued(client, _arq_pool):
    r = await client.post(BASE, json={"kind": "text", "text": TEXT})
    assert r.status_code == 202 and r.json()["status"] == "pending"
    _arq_pool.enqueue_job.assert_awaited_once_with("ingest_context_source", r.json()["id"])


async def test_a_url_is_validated_before_anything_is_queued(client, _arq_pool):
    r = await client.post(BASE, json={"kind": "url", "url": "http://169.254.169.254/latest/meta-data/"})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "private_address"
    _arq_pool.enqueue_job.assert_not_awaited()
    assert (await client.get(BASE)).json()["items"] == []


async def test_the_url_is_labelled_by_host_only(client):
    r = await client.post(BASE, json={"kind": "url", "url": "https://Clinica.Example.com/a/b?token=zzz"})
    assert r.status_code == 202
    item = (await client.get(BASE)).json()["items"][0]
    assert item["label"] == "clinica.example.com" and "zzz" not in repr(item)


@pytest.mark.parametrize(
    "body",
    [
        {"kind": "text", "text": "curto"},
        {"kind": "text", "text": "x" * 60_001},
        {"kind": "url"},
        {"kind": "ftp", "url": "x"},
        {"kind": "text"},
    ],
)
async def test_malformed_or_oversized_bodies_are_rejected(client, body):
    assert (await client.post(BASE, json=body)).status_code == 422


async def test_no_queue_means_not_accepted_and_nothing_stored(client):
    app.state.arq_pool = None
    assert (await client.post(BASE, json={"kind": "text", "text": TEXT})).status_code == 503
    assert (await client.get(BASE)).json()["items"] == []


async def test_the_daily_quota(client, monkeypatch):
    _limits(monkeypatch, per_day=2)
    for _ in range(2):
        assert (await client.post(BASE, json={"kind": "text", "text": TEXT})).status_code == 202
    r = await client.post(BASE, json={"kind": "text", "text": TEXT})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "source_quota"


async def test_the_in_flight_cap(client, monkeypatch):
    _limits(monkeypatch, in_flight=1)
    assert (await client.post(BASE, json={"kind": "text", "text": TEXT})).status_code == 202
    assert (await client.post(BASE, json={"kind": "text", "text": TEXT})).status_code == 429


async def test_another_clinics_source_is_a_404(client, db):
    other_tenant = uuid4()
    other = ContextSource(tenant_id=other_tenant, kind="text", label="x")
    async with db() as s:
        s.add(Tenant(id=other_tenant, clinic_name="Outra", phone_number_id=None))
        s.add(other)
        await s.commit()
    assert (await client.get(f"{BASE}/{other.id}")).status_code == 404
    assert (await client.delete(f"{BASE}/{other.id}")).status_code == 404
    assert (await client.get(BASE)).json()["items"] == []


async def test_detail_includes_the_proposal_only_when_done(client, db, tenant):
    proposal = {
        "fields": [
            {"key": "parking", "label": "Estacionamento", "kind": "text", "value": "Garagem",
             "evidence": "garagem do prédio", "status": "new"}
        ],
        "services": [],
    }
    async with db() as s:
        done = ContextSource(tenant_id=tenant.id, kind="text", label="Texto colado",
                             status="done", proposal=proposal)
        pending = ContextSource(tenant_id=tenant.id, kind="text", label="Texto colado")
        s.add_all([done, pending])
        await s.commit()
    d = (await client.get(f"{BASE}/{done.id}")).json()
    assert d["has_proposal"] is True and d["proposal"]["fields"][0]["key"] == "parking"
    p = (await client.get(f"{BASE}/{pending.id}")).json()
    assert p["has_proposal"] is False and p.get("proposal") is None


async def test_delete_removes_the_row(client):
    created = (await client.post(BASE, json={"kind": "text", "text": TEXT})).json()
    assert (await client.delete(f"{BASE}/{created['id']}")).status_code == 204
    assert (await client.get(BASE)).json()["items"] == []


async def test_a_docx_upload_is_read_in_the_request_and_queued(client, _arq_pool, db):
    docx = _docx_bytes("Cancelamentos com 24 horas de antecedência.")
    files = {"file": ("politica.docx", docx, DOCX_MIME)}
    r = await client.post(f"{BASE}/file", files=files)
    assert r.status_code == 202
    _arq_pool.enqueue_job.assert_awaited_once_with("ingest_context_source", r.json()["id"])
    async with db() as s:
        row = await s.get(ContextSource, UUID(r.json()["id"]))
    assert row.kind == "file" and row.label == "politica.docx" and "24 horas" in row.source_text


async def test_an_unsupported_upload_is_refused_with_a_code(client, _arq_pool):
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 50
    r = await client.post(f"{BASE}/file", files={"file": ("foto.png", png, "image/png")})
    assert r.status_code == 422 and r.json()["detail"]["code"] == "unsupported_type"
    _arq_pool.enqueue_job.assert_not_awaited()


async def test_an_oversized_upload_is_refused(client, monkeypatch):
    monkeypatch.setattr(text_extract, "MAX_UPLOAD_BYTES", 100)
    r = await client.post(f"{BASE}/file", files={"file": ("a.txt", b"x" * 5000, "text/plain")})
    assert r.status_code == 413 and r.json()["detail"]["code"] == "too_large"


async def test_deleting_a_source_does_not_hand_the_daily_quota_back(client, monkeypatch):
    # Delete-then-resubmit would otherwise be unlimited LLM calls (SPEC G5 is a COST cap).
    _limits(monkeypatch, per_day=1)
    created = (await client.post(BASE, json={"kind": "text", "text": TEXT})).json()
    assert (await client.delete(f"{BASE}/{created['id']}")).status_code == 204
    assert (await client.get(BASE)).json()["items"] == []
    assert (await client.get(f"{BASE}/{created['id']}")).status_code == 404
    r = await client.post(BASE, json={"kind": "text", "text": TEXT})
    assert r.status_code == 429 and r.json()["detail"]["code"] == "source_quota"


async def test_deleting_a_source_in_flight_keeps_it_counted_as_in_flight(client, monkeypatch):
    _limits(monkeypatch, in_flight=1)
    created = (await client.post(BASE, json={"kind": "text", "text": TEXT})).json()
    assert (await client.delete(f"{BASE}/{created['id']}")).status_code == 204
    # The job is still running (and billing) until it ends: the slot is not free.
    assert (await client.post(BASE, json={"kind": "text", "text": TEXT})).status_code == 429


async def test_deleting_erases_what_the_source_held(client, db):
    created = (await client.post(BASE, json={"kind": "text", "text": TEXT})).json()
    async with db() as s:
        row = await s.get(ContextSource, UUID(created["id"]))
        row.proposal = {"fields": [{"key": "parking", "evidence": "quote"}], "services": []}
        row.status = "done"
        await s.commit()
    assert (await client.delete(f"{BASE}/{created['id']}")).status_code == 204
    async with db() as s:
        row = await s.get(ContextSource, UUID(created["id"]))
    assert row.proposal is None and row.source_text is None and row.url is None
    assert row.deleted_at is not None


async def test_an_over_quota_upload_is_refused_before_the_file_is_parsed(client, monkeypatch):
    _limits(monkeypatch, per_day=1)
    assert (await client.post(BASE, json={"kind": "text", "text": TEXT})).status_code == 202
    parsed = []
    monkeypatch.setattr(text_extract, "extract_upload_text", lambda *a: parsed.append(a) or TEXT)
    r = await client.post(f"{BASE}/file", files={"file": ("a.txt", TEXT.encode(), "text/plain")})
    assert r.status_code == 429 and parsed == []


async def test_the_upload_is_parsed_off_the_event_loop(client, monkeypatch):
    import threading

    seen = []

    def spy(data, filename):
        seen.append(threading.current_thread())
        return TEXT

    monkeypatch.setattr(text_extract, "extract_upload_text", spy)
    r = await client.post(f"{BASE}/file", files={"file": ("a.txt", TEXT.encode(), "text/plain")})
    assert r.status_code == 202
    assert seen and seen[0] is not threading.main_thread()  # pure-Python PDF/DOCX parsing blocks


async def test_a_row_stuck_in_flight_for_long_stops_counting(client, db, tenant, monkeypatch):
    # A worker killed hard leaves "processing" rows nobody closes: they must not lock the clinic.
    from datetime import UTC, datetime, timedelta

    _limits(monkeypatch, in_flight=1)
    async with db() as s:
        s.add(
            ContextSource(
                tenant_id=tenant.id,
                kind="text",
                label="x",
                status="processing",
                created_at=datetime.now(UTC) - timedelta(minutes=30),
            )
        )
        await s.commit()
    assert (await client.post(BASE, json={"kind": "text", "text": TEXT})).status_code == 202


async def test_a_queue_that_fails_on_enqueue_leaves_no_orphan_row(client, _arq_pool):
    _arq_pool.enqueue_job.side_effect = ConnectionError("redis down")
    r = await client.post(BASE, json={"kind": "text", "text": TEXT})
    assert r.status_code == 503 and r.json()["detail"]["code"] == "queue_unavailable"
    # A row nobody will ever process would count against the in-flight cap forever.
    assert (await client.get(BASE)).json()["items"] == []


async def test_parallel_requests_cannot_exceed_the_daily_quota(client, monkeypatch):
    # The count and the insert are ONE critical section: 8 racing POSTs, 3 slots, exactly 3 rows.
    import asyncio

    _limits(monkeypatch, per_day=3)
    results = await asyncio.gather(
        *[client.post(BASE, json={"kind": "text", "text": TEXT}) for _ in range(8)]
    )
    codes = sorted(r.status_code for r in results)
    assert codes == [202] * 3 + [429] * 5
    assert len((await client.get(BASE)).json()["items"]) == 3


async def test_only_a_couple_of_uploads_are_parsed_at_the_same_time(client, monkeypatch):
    # Parsing is CPU the API shares with the WhatsApp webhook: a burst must queue, not stampede.
    import asyncio
    import threading
    import time

    lock = threading.Lock()
    running = {"now": 0, "max": 0}

    def slow_parse(data, filename):
        with lock:
            running["now"] += 1
            running["max"] = max(running["max"], running["now"])
        time.sleep(0.15)
        with lock:
            running["now"] -= 1
        return TEXT

    monkeypatch.setattr(text_extract, "extract_upload_text", slow_parse)
    _limits(monkeypatch, per_day=99, in_flight=99)
    uploads = [
        client.post(f"{BASE}/file", files={"file": (f"{i}.txt", TEXT.encode(), "text/plain")})
        for i in range(6)
    ]
    results = await asyncio.gather(*uploads)
    assert [r.status_code for r in results] == [202] * 6
    assert running["max"] <= 2
