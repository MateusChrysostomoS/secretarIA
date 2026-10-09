"""Staff free text on the patient's own channel (TASK-032 R4, spec 4.4)."""

from datetime import timedelta

import pytest

from secretaria.models import Appointment, MessageSender, Patient, Tenant
from secretaria.services import staff_patient_message as spm
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    NOW,
    FakeWhatsAppClient,
    fake_waba_token,
    outbound_messages,
    seed_world,
)

LINK = "https://portal.exemplo/clinicas/?convite=abc"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch):
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(spm, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(spm, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: LINK)


@pytest.fixture
def usage(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    events: list[dict] = []

    async def _emit(**kwargs) -> bool:
        events.append(kwargs)
        return True

    monkeypatch.setattr(spm, "emit_usage_event", _emit)
    return events


@pytest.fixture
def mail(monkeypatch: pytest.MonkeyPatch) -> list[tuple]:
    sent: list[tuple] = []

    async def _send(to: str, template: str, variables: dict) -> EmailOutcome:
        sent.append((to, template, variables))
        return EmailOutcome.SENT

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    return sent


async def _send(db, world, text="Olá, tudo bem?", *, allow_paid=False):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        patient = await session.get(Patient, world.patient.id)
        return await spm.send_staff_message(
            session, tenant, appointment, patient, text, allow_paid=allow_paid, now=NOW
        )


# ------------------------------------------------------------------ WhatsApp


async def test_inside_the_window_it_is_plain_text_and_recorded_as_the_clinic(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))

    result = await _send(db, world)

    assert result.delivery == spm.DELIVERY_WHATSAPP_TEXT and result.email_nudge is None
    assert FakeWhatsAppClient.all_sent() == [("text", "5511988887777", "Olá, tudo bem?")]
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN and row.body == "Olá, tudo bem?"
    assert row.wam_id == "wamid.text"
    assert usage == []  # free inside the window


async def test_outside_the_window_it_needs_the_clinics_authorisation(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=30))

    with pytest.raises(spm.OutsideWindowError) as err:
        await _send(db, world)

    assert err.value.code == "outside_window_not_authorised"
    assert err.value.whatsapp_link == "https://wa.me/5511988887777"
    assert FakeWhatsAppClient.all_sent() == []
    assert await outbound_messages(db, world.conversation.id) == []


async def test_a_patient_who_never_wrote_is_outside_the_window(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=None)

    with pytest.raises(spm.OutsideWindowError):
        await _send(db, world)


async def test_authorised_outside_the_window_it_is_a_one_line_template_and_metered(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=30))

    result = await _send(db, world, "Olá!\r\n\r\nPodemos  confirmar?\tAté logo", allow_paid=True)

    assert result.delivery == spm.DELIVERY_WHATSAPP_TEMPLATE
    [sent] = FakeWhatsAppClient.all_sent()
    assert sent[0] == "template" and sent[2] == "appointment_reminder" and sent[3] == "pt_BR"
    assert sent[4] == ["Olá! Podemos confirmar? Até logo"]  # no newline, tab or run of spaces
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.body == "Olá!\r\n\r\nPodemos  confirmar?\tAté logo"  # history keeps what staff typed
    assert [e["feature"] for e in usage] == ["reminders"] and usage[0]["amount"] == 1


async def test_when_meta_fails_nothing_is_recorded_and_the_error_is_typed(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    FakeWhatsAppClient.fail_everything = True

    with pytest.raises(spm.DeliveryFailedError) as err:
        await _send(db, world)

    assert err.value.code == "delivery_failed"
    assert await outbound_messages(db, world.conversation.id) == []


async def test_an_appointment_without_a_patient_has_no_channel(db, usage):  # noqa: F811
    world = await seed_world(db)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        with pytest.raises(spm.NoChannelError):
            await spm.send_staff_message(session, tenant, appointment, None, "oi", now=NOW)


# -------------------------------------------------------------------- Portal


async def test_a_portal_patient_gets_the_chat_message_and_an_email_nudge_without_the_text(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")

    result = await _send(db, world, "Exame com resultado alterado, ligue.")

    assert result.delivery == spm.DELIVERY_PORTAL_CHAT and result.email_nudge == spm.EMAIL_SENT
    assert result.message_id is not None
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN and row.body == "Exame com resultado alterado, ligue."
    [(to, template, variables)] = mail
    assert (to, template) == ("paciente@x.com", "clinic_message_patient")
    assert variables == {"clinic_name": "Clínica Olhar", "link_line": LINK}
    assert "Exame" not in repr(variables)  # the text never travels by e-mail
    assert FakeWhatsAppClient.all_sent() == []


async def test_a_portal_patient_without_email_still_gets_the_chat_message(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email=None)

    result = await _send(db, world)

    assert result.delivery == spm.DELIVERY_PORTAL_CHAT
    assert result.email_nudge == spm.EMAIL_NO_ADDRESS
    assert mail == []
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_a_failing_email_does_not_fail_the_message(db, monkeypatch):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")

    async def _down(to, template, variables):
        return EmailOutcome.SEND_FAILED

    monkeypatch.setattr(spm, "send_transactional_email_result", _down)

    result = await _send(db, world)

    assert result.email_nudge == spm.EMAIL_NOT_SENT
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_without_a_portal_link_the_nudge_still_says_where_to_go(db, mail, monkeypatch):  # noqa: F811
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: None)
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")

    await _send(db, world)

    assert "portal" in mail[0][2]["link_line"].lower()


async def test_a_portal_patient_without_a_conversation_has_no_channel(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email="paciente@x.com")
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.conversation_id = None
        await session.delete(await session.get(type(world.conversation), world.conversation.id))
        await session.commit()

    with pytest.raises(spm.NoChannelError):
        await _send(db, world)

    assert mail == []


# ------------------------------------------------------------------ isolation


async def test_a_conversation_of_another_clinic_is_never_used(db):  # noqa: F811
    mine = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    theirs = await seed_world(
        db, channel=CHANNEL_BRAIN_MESSAGE, phone_number_id="pnid-2", wa_id="5511900000002"
    )
    async with db() as session:
        appointment = await session.get(Appointment, mine.appointment.id)
        appointment.conversation_id = theirs.conversation.id  # corrupt link
        await session.commit()
        appointment = await session.get(Appointment, mine.appointment.id)
        patient = await session.get(Patient, mine.patient.id)

        found = await spm.conversation_id_for(session, mine.tenant.id, appointment, patient)

    assert found == mine.conversation.id  # fell back to MY patient's own conversation
