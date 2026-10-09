"""POST /appointments/{id}/message (TASK-032 R4, spec 4.4)."""

# ruff: noqa: F811  (pytest fixtures imported and re-requested by name)

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from secretaria.api.hub.deps import get_current_tenant
from secretaria.core.database import get_session
from secretaria.models import Appointment, MessageSender, Tenant
from secretaria.services import staff_patient_message as spm
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    FakeWhatsAppClient,
    fake_waba_token,
    outbound_messages,
    seed_world,
)

CALENDAR = "/tenants/me/calendar"


def _recent(hours: float):
    return datetime.now(UTC) - timedelta(hours=hours)


@pytest.fixture
def acting() -> dict:
    return {"tenant_id": None}


@pytest.fixture(autouse=True)
def _wire(db, acting, monkeypatch: pytest.MonkeyPatch):  # noqa: F811
    from fastapi import Depends

    from secretaria.main import app

    async def _fake_get_session():
        async with db() as session:
            yield session

    async def _fake_get_current_tenant(session: AsyncSession = Depends(get_session)) -> Tenant:
        return await session.get(Tenant, acting["tenant_id"])

    app.dependency_overrides[get_session] = _fake_get_session
    app.dependency_overrides[get_current_tenant] = _fake_get_current_tenant
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(spm, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(spm, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: "https://portal/x")

    async def _emit(**kwargs) -> bool:
        return True

    monkeypatch.setattr(spm, "emit_usage_event", _emit)
    yield
    app.dependency_overrides.pop(get_session, None)
    app.dependency_overrides.pop(get_current_tenant, None)


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []

    async def _send(to, template, variables):
        sent.append((to, template, variables))
        return EmailOutcome.SENT

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    return sent


async def _world(db, acting, **kwargs):  # noqa: F811
    world = await seed_world(db, **kwargs)
    acting["tenant_id"] = world.tenant.id
    return world


async def _message(client: AsyncClient, appointment_id, text="Podemos confirmar?", **extra):
    return await client.post(
        f"{CALENDAR}/appointments/{appointment_id}/message", json={"text": text, **extra}
    )


async def test_inside_the_window_it_is_plain_text(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting, last_inbound_at=_recent(1))

    response = await _message(client, world.appointment.id, "  Podemos confirmar?  ")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delivery"] == "whatsapp_text" and body["email_nudge"] is None
    assert body["message_id"]
    assert FakeWhatsAppClient.all_sent() == [("text", "5511988887777", "Podemos confirmar?")]
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN


async def test_outside_the_window_it_is_refused_with_the_free_alternative(  # noqa: F811
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, last_inbound_at=_recent(30))

    response = await _message(client, world.appointment.id)

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "outside_window_not_authorised"
    assert detail["whatsapp_link"] == "https://wa.me/5511988887777"
    assert "template_cost_brl" in detail and "message" in detail
    assert FakeWhatsAppClient.all_sent() == []


async def test_outside_the_window_authorised_it_is_the_template(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting, last_inbound_at=_recent(30))

    response = await _message(client, world.appointment.id, notify_outside_window=True)

    assert response.status_code == 200 and response.json()["delivery"] == "whatsapp_template"
    assert FakeWhatsAppClient.all_sent()[0][0] == "template"


async def test_a_portal_patient_without_email_is_messaged_in_the_chat(  # noqa: F811
    client: AsyncClient, db, acting, mail
):
    world = await _world(db, acting, channel=CHANNEL_BRAIN_MESSAGE, email=None)

    response = await _message(client, world.appointment.id)

    assert response.status_code == 200
    assert response.json()["delivery"] == "portal_chat"
    assert response.json()["email_nudge"] == "no_email"
    assert mail == [] and len(await outbound_messages(db, world.conversation.id)) == 1


async def test_a_portal_patient_with_email_is_nudged(client: AsyncClient, db, acting, mail):  # noqa: F811
    world = await _world(db, acting, channel=CHANNEL_BRAIN_MESSAGE, email="p@x.com")

    response = await _message(client, world.appointment.id)

    assert response.json()["email_nudge"] == "sent" and mail[0][0] == "p@x.com"


async def test_another_clinics_staff_gets_a_404_and_nothing_is_sent(
    client: AsyncClient, db, acting
):  # noqa: F811
    mine = await seed_world(db, last_inbound_at=_recent(1))
    # the caller belongs to ANOTHER clinic (distinct ids: the fixture's defaults collide)
    await _world(db, acting, phone_number_id="pnid-2", wa_id="5511900000002")

    response = await _message(client, mine.appointment.id)

    assert response.status_code == 404
    assert FakeWhatsAppClient.all_sent() == []
    assert await outbound_messages(db, mine.conversation.id) == []


async def test_an_appointment_without_a_patient_has_no_channel(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting)
    async with db() as session:
        await session.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment.id)
            .values(patient_id=None)
        )
        await session.commit()

    response = await _message(client, world.appointment.id)

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "no_channel"


async def test_a_refusal_by_the_channel_is_a_502_and_records_nothing(
    client: AsyncClient, db, acting
):  # noqa: F811
    world = await _world(db, acting, last_inbound_at=_recent(1))
    FakeWhatsAppClient.fail_everything = True

    response = await _message(client, world.appointment.id)

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "delivery_failed"
    assert await outbound_messages(db, world.conversation.id) == []


async def test_a_failing_portal_write_is_a_502_and_records_nothing(
    client: AsyncClient, db, acting, monkeypatch
):  # noqa: F811
    world = await _world(db, acting, channel=CHANNEL_BRAIN_MESSAGE)

    async def _boom(self, **kwargs):
        raise RuntimeError("db down")

    monkeypatch.setattr(spm.BrainMessageSender, "send_text_message", _boom)

    response = await _message(client, world.appointment.id)

    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "delivery_failed"
    assert await outbound_messages(db, world.conversation.id) == []


@pytest.mark.parametrize("text", ["", "   ", "x" * 1001])
async def test_blank_or_huge_text_is_rejected(client: AsyncClient, db, acting, text):  # noqa: F811
    world = await _world(db, acting, last_inbound_at=_recent(1))

    response = await _message(client, world.appointment.id, text)

    assert response.status_code == 422
    assert FakeWhatsAppClient.all_sent() == []


async def test_it_works_with_the_clinic_switch_off(client: AsyncClient, db, acting):  # noqa: F811
    world = await _world(db, acting, v2=False, last_inbound_at=_recent(1))

    assert (await _message(client, world.appointment.id)).status_code == 200


async def test_malformed_ids_are_a_404(client: AsyncClient, db, acting):  # noqa: F811
    await _world(db, acting)

    assert (await _message(client, "nope")).status_code == 404
