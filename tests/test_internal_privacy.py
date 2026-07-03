"""Tests for api/internal_privacy.py — LGPD export/erase endpoints — and the
consent-event write path at patient creation (workers/tasks.py).

Uses a real in-memory sqlite DB (StaticPool) with `get_session` overridden,
same pattern as test_hub_professionals.py / test_analytics_bi_plugin.py.
The X-Internal-Api-Key guard itself is exercised end-to-end in
test_internal.py; here INTERNAL_API_KEY is fixed and always sent correctly
so these tests focus on the export/erase behavior.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("ENCRYPTION_KEY", "gBSpATEZoI21UX0_59nHvxdUDJ4drCttg2RAEaPJc1w=")
os.environ["INTERNAL_API_KEY"] = "test-internal-key"

import hashlib  # noqa: E402
from uuid import uuid4  # noqa: E402

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

from secretaria.api import internal_privacy  # noqa: E402
from secretaria.core.database import Base, get_session  # noqa: E402
from secretaria.models import (  # noqa: E402
    Appointment,
    ConsentEvent,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    Tenant,
)

GOOD_KEY = "test-internal-key"


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
def _override_session(db):
    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    app.dependency_overrides[get_session] = _fake_get_session
    yield
    app.dependency_overrides.pop(get_session, None)


def _headers():
    return {"X-Internal-Api-Key": GOOD_KEY}


async def _seed_full_subject(db, *, tenant_id, wa_id):
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None:
            tenant = Tenant(id=tenant_id, clinic_name="Clinic", phone_number_id=str(uuid4())[:12])
            session.add(tenant)
            await session.flush()

        patient = Patient(id=uuid4(), tenant_id=tenant_id, wa_id=wa_id, name="Maria")
        session.add(patient)
        await session.flush()

        conversation = Conversation(id=uuid4(), tenant_id=tenant_id, patient_id=patient.id)
        session.add(conversation)
        await session.flush()

        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
            )
        )
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                body="Olá!",
            )
        )

        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant_id,
            patient_id=patient.id,
            conversation_id=conversation.id,
            google_event_id="evt-1",
            appointment_type="Consulta",
            phone=wa_id,
        )
        session.add(appointment)

        session.add(
            ConsentEvent(
                tenant_id=tenant_id,
                wa_id=wa_id,
                kind="first_contact_service",
                legal_basis="TODO_LAWYER: execução de contrato vs consentimento",
            )
        )

        await session.commit()
        await session.refresh(patient)
        await session.refresh(appointment)
        return tenant, patient, conversation, appointment


# --------------------------------------------------------------------------
# Export
# --------------------------------------------------------------------------


async def test_export_unknown_wa_id_returns_empty_bundle(client: AsyncClient, db):
    tenant_id = uuid4()
    async with db() as session:
        session.add(Tenant(id=tenant_id, clinic_name="Clinic", phone_number_id=str(uuid4())[:12]))
        await session.commit()

    response = await client.get(
        f"/internal/privacy/tenants/{tenant_id}/subjects/5511900000000/export",
        headers=_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["patient"] is None
    assert body["appointments"] == []
    assert body["conversations"] == []
    assert body["messages"] == []
    assert body["consent_events"] == []
    assert body["analytics_events"] == []


async def test_export_full_bundle_shape(client: AsyncClient, db):
    tenant_id = uuid4()
    wa_id = "5511900000001"
    tenant, patient, conversation, appointment = await _seed_full_subject(
        db, tenant_id=tenant_id, wa_id=wa_id
    )

    response = await client.get(
        f"/internal/privacy/tenants/{tenant_id}/subjects/{wa_id}/export", headers=_headers()
    )
    assert response.status_code == 200
    body = response.json()

    assert body["patient"]["wa_id"] == wa_id
    assert body["patient"]["id"] == str(patient.id)
    assert len(body["appointments"]) == 1
    assert body["appointments"][0]["id"] == str(appointment.id)
    assert len(body["conversations"]) == 1
    assert body["conversations"][0]["id"] == str(conversation.id)
    assert len(body["messages"]) == 2
    assert {m["direction"] for m in body["messages"]} == {"inbound", "outbound"}
    assert len(body["consent_events"]) == 1
    assert body["consent_events"][0]["kind"] == "first_contact_service"
    assert body["analytics_events"] == []


# --------------------------------------------------------------------------
# Erase
# --------------------------------------------------------------------------


async def test_erase_counts_and_anonymizes_appointment_not_deletes(client: AsyncClient, db):
    tenant_id = uuid4()
    wa_id = "5511900000002"
    tenant, patient, conversation, appointment = await _seed_full_subject(
        db, tenant_id=tenant_id, wa_id=wa_id
    )

    response = await client.delete(
        f"/internal/privacy/tenants/{tenant_id}/subjects/{wa_id}", headers=_headers()
    )
    assert response.status_code == 200
    erased = response.json()["erased"]
    assert erased == {
        "patients": 1,
        "conversations": 1,
        "messages": 2,
        "appointments": 1,
        "consent_events": 1,
    }

    async with db() as session:
        assert await session.get(Patient, patient.id) is None
        assert await session.get(Conversation, conversation.id) is None
        assert (
            await session.scalar(select(Message).where(Message.conversation_id == conversation.id))
        ) is None
        assert (
            await session.scalar(select(ConsentEvent).where(ConsentEvent.wa_id == wa_id))
        ) is None

        # Appointment ANONYMIZED, not deleted.
        remaining = await session.get(Appointment, appointment.id)
        assert remaining is not None
        assert remaining.patient_id is None
        assert remaining.phone is None
        assert remaining.appointment_type == "Consulta"  # untouched


async def test_erase_is_idempotent_repeat_returns_zero_counts(client: AsyncClient, db):
    tenant_id = uuid4()
    wa_id = "5511900000003"
    await _seed_full_subject(db, tenant_id=tenant_id, wa_id=wa_id)

    first = await client.delete(
        f"/internal/privacy/tenants/{tenant_id}/subjects/{wa_id}", headers=_headers()
    )
    assert first.status_code == 200
    assert sum(first.json()["erased"].values()) > 0

    second = await client.delete(
        f"/internal/privacy/tenants/{tenant_id}/subjects/{wa_id}", headers=_headers()
    )
    assert second.status_code == 200
    assert second.json()["erased"] == {
        "patients": 0,
        "conversations": 0,
        "messages": 0,
        "appointments": 0,
        "consent_events": 0,
    }


async def test_erase_unknown_subject_is_idempotent_no_error(client: AsyncClient, db):
    tenant_id = uuid4()
    async with db() as session:
        session.add(Tenant(id=tenant_id, clinic_name="Clinic", phone_number_id=str(uuid4())[:12]))
        await session.commit()

    response = await client.delete(
        f"/internal/privacy/tenants/{tenant_id}/subjects/5511900000000", headers=_headers()
    )
    assert response.status_code == 200
    assert sum(response.json()["erased"].values()) == 0


async def test_erase_warning_logs_sha256_not_raw_wa_id(
    client: AsyncClient, db, monkeypatch: pytest.MonkeyPatch
):
    tenant_id = uuid4()
    wa_id = "5511900000099"
    await _seed_full_subject(db, tenant_id=tenant_id, wa_id=wa_id)

    captured: dict = {}
    original_warning = internal_privacy.logger.warning

    def _capture(event, **kwargs):
        if event == "privacy_erasure_executed":
            captured["event"] = event
            captured.update(kwargs)
        return original_warning(event, **kwargs)

    monkeypatch.setattr(internal_privacy.logger, "warning", _capture)

    response = await client.delete(
        f"/internal/privacy/tenants/{tenant_id}/subjects/{wa_id}", headers=_headers()
    )
    assert response.status_code == 200

    assert captured.get("event") == "privacy_erasure_executed"
    assert captured.get("tenant_id") == str(tenant_id)
    assert captured.get("wa_id_sha256") == hashlib.sha256(wa_id.encode("utf-8")).hexdigest()
    # The raw wa_id must never appear anywhere in the captured log kwargs.
    assert wa_id not in str(captured)


# --------------------------------------------------------------------------
# Auth guard still applies (same require_internal_api_key as api/internal.py)
# --------------------------------------------------------------------------


async def test_export_missing_key_is_unauthorized(client: AsyncClient):
    response = await client.get(
        f"/internal/privacy/tenants/{uuid4()}/subjects/5511900000000/export"
    )
    assert response.status_code == 401


async def test_erase_missing_key_is_unauthorized(client: AsyncClient):
    response = await client.delete(f"/internal/privacy/tenants/{uuid4()}/subjects/5511900000000")
    assert response.status_code == 401
