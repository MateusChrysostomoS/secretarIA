"""send_clinic_notice: every clinic-action notice on the patient's own channel (TASK-032 R7)."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from secretaria.models import Appointment, Conversation, MessageSender, Patient, Tenant
from secretaria.services import appointment_release, staff_patient_message as spm
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import (
    NOW,
    WA_ID,
    FakeWhatsAppClient,
    fake_waba_token,
    outbound_messages,
    seed_world,
)

BUTTONS = [
    ("remconfirm|r1", "Confirmar"),
    ("remcancel|r1", "Cancelar"),
    ("remedit|r1", "Alterar Dados"),
]
BODY = "Seu médico confirmou sua consulta.\nVocê está ciente?"


@pytest.fixture(autouse=True)
def _wire(monkeypatch: pytest.MonkeyPatch):
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(spm, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(spm, "get_waba_token", fake_waba_token)
    monkeypatch.setattr(spm, "portal_conversation_link", lambda tenant_id: "https://portal/x")


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

    async def _send(to, template, variables) -> EmailOutcome:
        sent.append((to, template, variables))
        return EmailOutcome.SENT

    monkeypatch.setattr(spm, "send_transactional_email_result", _send)
    return sent


async def _notice(db, world, *, buttons=None, allow_paid=False, patient=True):  # noqa: F811
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        who = await session.get(Patient, world.patient.id) if patient else None
        return await spm.send_clinic_notice(
            session,
            tenant,
            appointment,
            who,
            body=BODY,
            buttons=buttons,
            allow_paid=allow_paid,
            usage_key="test",
            now=NOW,
        )


def test_the_vocabulary_is_r4s_plus_whatsapp_sent_and_release_reexports_it():
    assert spm.NOTICE_WHATSAPP_SENT == "whatsapp_sent"
    assert appointment_release.NOTICE_WHATSAPP_QUEUED == spm.NOTICE_WHATSAPP_QUEUED
    assert appointment_release.NOTICE_PORTAL_CHAT_EMAIL == spm.NOTICE_PORTAL_CHAT_EMAIL
    assert spm.DELIVERED_NOTICES == {
        "whatsapp_queued",
        "whatsapp_sent",
        "portal_chat",
        "portal_chat_email",
    }


@pytest.mark.parametrize(
    ("requested", "standing", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
def test_paid_authorisation_is_the_request_or_the_clinics_standing_yes(
    requested, standing, expected
):
    tenant = SimpleNamespace(paid_notices_auto_approved=standing)
    assert spm.paid_notice_authorised(tenant, requested) is expected


async def test_whatsapp_inside_the_window_sends_the_buttons_and_records_the_card(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))

    result = await _notice(db, world, buttons=BUTTONS)

    assert result.code == "whatsapp_sent" and result.delivered and result.whatsapp_link is None
    assert FakeWhatsAppClient.all_sent() == [("buttons", WA_ID, BODY, BUTTONS)]
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN and row.wam_id == "wamid.buttons"
    assert row.interactive["kind"] == "buttons"
    assert [o["id"] for o in row.interactive["options"]] == [b[0] for b in BUTTONS]
    assert result.message_id == row.id
    assert usage == []


async def test_whatsapp_inside_the_window_without_buttons_is_plain_text(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))

    result = await _notice(db, world)

    assert result.code == "whatsapp_sent"
    assert FakeWhatsAppClient.all_sent() == [("text", WA_ID, BODY)]


async def test_outside_the_window_without_authorisation_nothing_leaves(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=30))

    result = await _notice(db, world, buttons=BUTTONS)

    assert result.code == "whatsapp_outside_window" and not result.delivered
    assert result.whatsapp_link == f"https://wa.me/{WA_ID}"
    assert FakeWhatsAppClient.all_sent() == []
    assert await outbound_messages(db, world.conversation.id) == []


async def test_outside_the_window_with_authorisation_the_paid_template_carries_one_line(
    db,  # noqa: F811
    usage,  # noqa: F811
):
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=30))

    result = await _notice(db, world, buttons=BUTTONS, allow_paid=True)

    assert result.code == "whatsapp_sent"
    [(kind, to, template, lang, variables, payloads)] = FakeWhatsAppClient.all_sent()
    assert (kind, to, template, lang) == ("template", WA_ID, "appointment_reminder", "pt_BR")
    assert variables == ["Seu médico confirmou sua consulta. Você está ciente?"]
    assert payloads is None  # no approved button template yet: the text goes alone
    [event] = usage
    assert event["feature"] == "reminders" and event["event_id"].startswith("test:")


async def test_a_refusing_channel_is_notice_failed_and_records_nothing(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    FakeWhatsAppClient.fail_everything = True

    result = await _notice(db, world)

    assert result.code == "notice_failed"
    assert await outbound_messages(db, world.conversation.id) == []


async def test_no_patient_or_a_foreign_patient_is_no_channel(db):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    assert (await _notice(db, world, patient=False)).code == "no_channel"

    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900000002")
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        appointment = await session.get(Appointment, world.appointment.id)
        foreign = await session.get(Patient, other.patient.id)
        result = await spm.send_clinic_notice(
            session, tenant, appointment, foreign, body=BODY, allow_paid=True, usage_key="t"
        )
    assert result.code == "no_channel"
    assert FakeWhatsAppClient.all_sent() == []


async def test_portal_writes_the_card_in_the_chat_and_nudges_by_email(db, mail):  # noqa: F811
    world = await seed_world(
        db,  # noqa: F811 channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="maria@exemplo.com"
    )

    result = await _notice(db, world, buttons=BUTTONS)

    assert result.code == "portal_chat_email" and result.delivered
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.sender == MessageSender.HUMAN
    assert row.interactive["kind"] == "buttons"
    assert result.message_id == row.id
    [(to, template, variables)] = mail
    assert (to, template) == ("maria@exemplo.com", "clinic_message_patient")
    assert BODY not in str(variables)  # the nudge never carries the content
    assert FakeWhatsAppClient.all_sent() == []


async def test_portal_without_email_is_still_delivered_in_the_chat(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email=None)

    result = await _notice(db, world)

    assert result.code == "portal_chat" and mail == []
    assert len(await outbound_messages(db, world.conversation.id)) == 1


async def test_portal_without_a_conversation_is_no_channel(db, mail):  # noqa: F811
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="a@b.c")
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.conversation_id = None
        await session.delete(await session.get(Conversation, world.conversation.id))
        await session.commit()

    result = await _notice(db, world)

    assert result.code == "no_channel" and mail == []


async def test_the_patient_number_wins_over_the_appointment_contact_phone(db, usage):  # noqa: F811
    world = await seed_world(db, last_inbound_at=NOW - timedelta(hours=1))
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.phone = "5511977776666"
        await session.commit()

    await _notice(db, world)

    assert FakeWhatsAppClient.all_sent()[0][1] == WA_ID
