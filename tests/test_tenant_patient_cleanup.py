from uuid import uuid4

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine


@pytest.fixture
async def session():
    from secretaria.core.database import Base

    engine = create_async_engine("sqlite+aiosqlite://")
    event.listen(
        engine.sync_engine, "connect", lambda conn, _: conn.execute("PRAGMA foreign_keys=ON")
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with async_sessionmaker(engine, expire_on_commit=False)() as db:
        yield db
    await engine.dispose()


async def seed(session, name):
    from datetime import UTC, datetime, timedelta

    from secretaria.models import Appointment, Conversation, Message, Patient, Tenant
    from secretaria.models.booking_hold import BookingHold
    from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap
    from secretaria.models.message import MessageDirection, MessageSender
    from secretaria.models.pix_deposit import PixDeposit

    tenant = Tenant(clinic_name=name, phone_number_id=str(uuid4()))
    session.add(tenant)
    await session.flush()
    patient = Patient(tenant_id=tenant.id, wa_id=str(uuid4())[:20], name="Test Patient")
    session.add(patient)
    await session.flush()
    conv = Conversation(tenant_id=tenant.id, patient_id=patient.id)
    session.add(conv)
    await session.flush()
    appt = Appointment(
        tenant_id=tenant.id,
        patient_id=patient.id,
        conversation_id=conv.id,
        google_event_id="preserved-event",
        phone="test-contact",
        attendee_name="Test Attendee",
    )
    session.add(appt)
    await session.flush()
    message = Message(
        conversation_id=conv.id,
        body="patient-content",
        direction=MessageDirection.INBOUND,
        sender=MessageSender.PATIENT,
    )
    session.add_all(
        [
            message,
            ConversationPiiTokenMap(conversation_id=conv.id, tokens={"name": "Test Patient"}),
            PixDeposit(
                tenant_id=tenant.id,
                appointment_id=appt.id,
                patient_id=patient.id,
                amount_cents=100,
                percent_applied=10,
            ),
            BookingHold(
                tenant_id=tenant.id,
                patient_id=patient.id,
                conversation_id=conv.id,
                start_at=datetime.now(UTC),
                end_at=datetime.now(UTC) + timedelta(hours=1),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            ),
        ]
    )
    await session.commit()
    return tenant, patient, conv, appt, message


async def test_preview_is_read_only_and_cleanup_preserves_other_clinic_and_agenda(session):
    from secretaria.models import Appointment, Patient, Tenant
    from secretaria.models.pix_deposit import PixDeposit
    from secretaria.schemas.tenant_patient_cleanup import CleanupRequest
    from secretaria.services.tenant_patient_cleanup import cleanup_patients

    target, patient, _, appt, _ = await seed(session, "Target")
    other, sibling, _, _, _ = await seed(session, "Other")
    target_id, patient_id, other_id, sibling_id, appt_id = (
        target.id,
        patient.id,
        other.id,
        sibling.id,
        appt.id,
    )
    before = await cleanup_patients(session, target.id)
    assert before.status == "ready"
    assert before.counts["patients"] == 1
    assert await session.get(Patient, patient.id) is not None
    result = await cleanup_patients(
        session, target.id, payload=CleanupRequest(confirm=True, clinic_name="Target")
    )
    assert result.status == "completed"
    session.expire_all()
    assert await session.get(Patient, patient_id) is None
    assert await session.get(Patient, sibling_id) is not None
    assert await session.get(Tenant, target_id) is not None
    assert await session.get(Tenant, other_id) is not None
    saved = await session.get(Appointment, appt_id)
    assert saved.patient_id is saved.conversation_id is saved.phone is saved.attendee_name is None
    assert saved.google_event_id == "preserved-event"
    assert await session.scalar(select(func.count()).select_from(PixDeposit)) == 2
    after = await cleanup_patients(session, target_id)
    assert all(count == 0 for count in after.counts.values())
    assert "patient-content" not in result.model_dump_json()


async def test_attachment_failure_keeps_patient_and_key_for_retry(session, monkeypatch):
    from secretaria.models import Message, Patient
    from secretaria.schemas.tenant_patient_cleanup import CleanupRequest
    from secretaria.services import media_storage
    from secretaria.services.tenant_patient_cleanup import cleanup_patients

    tenant, patient, _, _, message = await seed(session, "Target")
    tenant_id, patient_id, message_id = tenant.id, patient.id, message.id
    key = media_storage.object_key_prefix(tenant_id, patient_id) + "file"
    message.attachment = {"r2_object_key": key}
    await session.commit()
    monkeypatch.setattr(media_storage, "is_configured", lambda: True)

    async def fail(_):
        raise media_storage.MediaStorageUnavailable("test failure")

    monkeypatch.setattr(media_storage, "delete_object_required", fail)
    result = await cleanup_patients(
        session, tenant_id, payload=CleanupRequest(confirm=True, clinic_name="Target")
    )
    assert result.status == "failed"
    assert await session.get(Patient, patient_id) is not None
    assert (await session.get(Message, message_id)).attachment["r2_object_key"] == key


async def test_name_and_confirm_guards(session):
    from fastapi import HTTPException

    from secretaria.schemas.tenant_patient_cleanup import CleanupRequest
    from secretaria.services.tenant_patient_cleanup import cleanup_patients

    tenant, *_ = await seed(session, "Target")
    for payload, status in [
        (CleanupRequest(clinic_name="Target"), 400),
        (CleanupRequest(confirm=True, clinic_name="Other"), 409),
    ]:
        with pytest.raises(HTTPException) as error:
            await cleanup_patients(session, tenant.id, payload=payload)
        assert error.value.status_code == status


async def test_detached_appointments_also_lose_patient_contact_and_attendee_name(session):
    from secretaria.models import Appointment
    from secretaria.schemas.tenant_patient_cleanup import CleanupRequest
    from secretaria.services.tenant_patient_cleanup import cleanup_patients

    tenant, *_ = await seed(session, "Target")
    other, *_ = await seed(session, "Other")
    detached = Appointment(tenant_id=tenant.id, google_event_id="legacy-event",
                           phone="legacy-contact", attendee_name="Test Legacy")
    outsider = Appointment(tenant_id=other.id, google_event_id="other-event",
                           phone="other-contact", attendee_name="Other Test")
    session.add_all([detached, outsider])
    await session.commit()
    detached_id, outsider_id = detached.id, outsider.id
    result = await cleanup_patients(
        session, tenant.id, payload=CleanupRequest(confirm=True, clinic_name="Target"))
    assert result.counts["appointments_anonymized"] == 2
    session.expire_all()
    kept = await session.get(Appointment, detached_id)
    assert kept.phone is kept.attendee_name is None
    assert kept.google_event_id == "legacy-event"
    assert (await session.get(Appointment, outsider_id)).attendee_name == "Other Test"


async def test_cleanup_routes_require_service_key(client, monkeypatch):
    from secretaria.config import get_settings

    monkeypatch.setattr(get_settings(), "INTERNAL_API_KEY", "cleanup-test-key")
    path = f"/internal/tenants/{uuid4()}/patient-cleanup"
    assert (await client.get(path + "/preview")).status_code == 401
    assert (
        await client.post(path, json={"confirm": True, "clinic_name": "Clinic"})
    ).status_code == 401
