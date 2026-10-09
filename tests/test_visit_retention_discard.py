# ruff: noqa: F811 - `db`/`api` are pytest fixtures imported from test_visit_merge
"""TASK-042: brain-api's retention job discards a Portal visit that only opened the link.

`POST /internal/brain-message/visits/discard` is synchronous because brain-api deletes its
own rows only after this side confirmed. The emptiness rule lives here (brain-api cannot see
the conversation): a patient message, any appointment or a live hold -> 409, nothing deleted.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from secretaria.models import (
    ConsentEvent,
    Conversation,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
)
from secretaria.models.appointment import Appointment
from secretaria.models.booking_hold import BookingHold
from secretaria.models.conversation_pii_token_map import ConversationPiiTokenMap
from secretaria.services.visit_merge import discard_empty_visit
from tests.test_visit_merge import (  # noqa: F401 - fixtures
    KEY,
    VISIT,
    _count,
    _portal_patient,
    _tenant,
    api,
    db,
)

URL = "/internal/brain-message/visits/discard"


async def _add_inbound(db, conversation) -> None:
    async with db() as session:
        session.add(
            Message(
                conversation_id=conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
            )
        )
        await session.commit()


async def test_an_empty_visit_is_discarded_with_its_bot_messages(db, api) -> None:
    tenant = await _tenant(db)
    _, conversation = await _portal_patient(db, tenant, VISIT, messages=2)
    async with db() as session:
        session.add(ConversationPiiTokenMap(conversation_id=conversation.id, tokens={"x": "y"}))
        await session.commit()

    resp = await api.client.post(
        URL, headers=KEY, json={"tenant_id": str(tenant.id), "external_id": VISIT}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "discarded"}
    assert await _count(db, Patient) == 0
    assert await _count(db, Conversation) == 0
    assert await _count(db, Message) == 0
    assert await _count(db, ConversationPiiTokenMap) == 0
    assert await _count(db, ConsentEvent) == 1, "the audit trail is never deleted"


async def test_a_retry_after_the_discard_is_absent_and_still_200(db, api) -> None:
    tenant = await _tenant(db)
    await _portal_patient(db, tenant, VISIT, messages=1)
    body = {"tenant_id": str(tenant.id), "external_id": VISIT}

    first = await api.client.post(URL, headers=KEY, json=body)
    second = await api.client.post(URL, headers=KEY, json=body)

    assert first.json() == {"status": "discarded"}
    assert second.status_code == 200
    assert second.json() == {"status": "absent"}


async def test_a_visit_the_patient_wrote_in_is_refused_and_kept(db, api) -> None:
    tenant = await _tenant(db)
    _, conversation = await _portal_patient(db, tenant, VISIT, messages=1)
    await _add_inbound(db, conversation)

    resp = await api.client.post(
        URL, headers=KEY, json={"tenant_id": str(tenant.id), "external_id": VISIT}
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "visit_not_empty"
    assert await _count(db, Patient) == 1
    assert await _count(db, Message) == 2


async def test_a_visit_with_any_appointment_is_refused(db) -> None:
    tenant = await _tenant(db)
    patient, _ = await _portal_patient(db, tenant, VISIT, messages=0)
    async with db() as session:
        session.add(
            Appointment(tenant_id=tenant.id, patient_id=patient.id, google_event_id="evt-1")
        )
        await session.commit()
    async with db() as session:
        result = await discard_empty_visit(session, tenant.id, VISIT)
    assert result.status == "not_empty"
    assert await _count(db, Patient) == 1


async def test_a_booking_linked_only_by_conversation_is_refused(db) -> None:
    tenant = await _tenant(db)
    _, conversation = await _portal_patient(db, tenant, VISIT, messages=0)
    async with db() as session:
        session.add(
            Appointment(
                tenant_id=tenant.id, conversation_id=conversation.id, google_event_id="evt-2"
            )
        )
        await session.commit()
    async with db() as session:
        assert (await discard_empty_visit(session, tenant.id, VISIT)).status == "not_empty"


async def test_a_visit_with_a_live_hold_is_refused_but_an_expired_one_is_not(db) -> None:
    tenant = await _tenant(db)
    patient, conversation = await _portal_patient(db, tenant, VISIT, messages=0)
    now = datetime.now(UTC)
    async with db() as session:
        hold = BookingHold(
            tenant_id=tenant.id,
            conversation_id=conversation.id,
            patient_id=patient.id,
            start_at=now + timedelta(days=1),
            end_at=now + timedelta(days=1, hours=1),
            expires_at=now + timedelta(minutes=5),
        )
        session.add(hold)
        await session.commit()
        hold_id = hold.id
    async with db() as session:
        assert (await discard_empty_visit(session, tenant.id, VISIT)).status == "not_empty"

    async with db() as session:
        row = await session.get(BookingHold, hold_id)
        row.expires_at = now - timedelta(hours=23)
        await session.commit()
    async with db() as session:
        assert (await discard_empty_visit(session, tenant.id, VISIT)).status == "discarded"
        await session.commit()
    assert await _count(db, BookingHold) == 0


async def test_scope_is_tenant_and_brain_message_channel(db, api) -> None:
    mine = await _tenant(db)
    other = await _tenant(db)
    await _portal_patient(db, other, VISIT, messages=0)
    await _portal_patient(db, mine, VISIT, messages=0, channel="whatsapp")

    resp = await api.client.post(
        URL, headers=KEY, json={"tenant_id": str(mine.id), "external_id": VISIT}
    )

    assert resp.json() == {"status": "absent"}
    assert await _count(db, Patient) == 2


async def test_the_contract_is_frozen_and_keyed(db, api) -> None:
    body = {"tenant_id": str(uuid4()), "external_id": VISIT}
    assert (await api.client.post(URL, json=body)).status_code == 401
    extra = await api.client.post(URL, headers=KEY, json={**body, "email": "a@b.c"})
    assert extra.status_code == 422
    empty = await api.client.post(URL, headers=KEY, json={**body, "external_id": ""})
    assert empty.status_code == 422
