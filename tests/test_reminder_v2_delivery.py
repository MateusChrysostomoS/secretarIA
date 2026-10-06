"""services/reminder_delivery.py — one reminder, the patient's own channel (TASK-032 R2)."""

from datetime import timedelta
from uuid import uuid4

import pytest

from secretaria.config import get_settings
from secretaria.core import database as core_database
from secretaria.services import reminder_delivery
from secretaria.services.brain_patients import PatientEmailResult
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.email import EmailOutcome
from secretaria.services.reminder_delivery import ReminderJob, deliver_reminder
from secretaria.services.reminder_text import ReminderContent, single_line_text
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import NOW, WA_ID, FakeWhatsAppClient, outbound_messages, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    monkeypatch.setattr(core_database, "async_session_factory", db)
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(reminder_delivery, "WhatsAppClient", FakeWhatsAppClient)
    yield


@pytest.fixture
def mail(monkeypatch):
    """Portal e-mail: brain-api answers an address; the mailer records the send."""
    sent: list[dict] = []
    state = {"email": "maria@example.com", "outcome": EmailOutcome.SENT}

    async def _fetch(tenant_id, external_id):
        return PatientEmailResult(available=True, email=state["email"])

    async def _send(to, template, variables):
        sent.append({"to": to, "template": template, "variables": variables})
        return state["outcome"]

    monkeypatch.setattr(reminder_delivery, "fetch_patient_email_result", _fetch)
    monkeypatch.setattr(reminder_delivery, "send_transactional_email_result", _send)
    monkeypatch.setattr(
        get_settings(), "BRAIN_MESSAGE_PORTAL_URL", "https://portal.exemplo", raising=False
    )
    return sent, state


def _job(
    world,
    *,
    with_prompt=True,
    deposit_paid=False,
    inbound_ago=None,
    attempt=1,
    waba_token="decrypted-waba-token",
    reminder_id=None,
    chat_written=False,
) -> ReminderJob:
    content = ReminderContent(
        clinic_name=world.tenant.clinic_name,
        timezone="America/Sao_Paulo",
        start_at=world.start_at,
        service_name="Consulta",
        doctor_name="Dra. Ana Souza",
        requirements=("Jejum de 8h",),
    )
    return ReminderJob(
        reminder_id=reminder_id or uuid4(),
        kind="day",
        attempt=attempt,
        tenant=world.tenant,
        patient=world.patient,
        appointment_id=world.appointment.id,
        content=content,
        with_prompt=with_prompt,
        deposit_paid=deposit_paid,
        conversation_id=world.conversation.id,
        waba_token=waba_token,
        last_inbound_at=(NOW - inbound_ago) if inbound_ago is not None else None,
        now=NOW,
        chat_written=chat_written,
    )


# ---- WhatsApp, inside the 24h window ----------------------------------------


async def test_inside_the_window_the_reminder_carries_the_three_buttons(db):  # noqa: F811
    world = await seed_world(db)
    job = _job(world, inbound_ago=timedelta(hours=2))

    outcome = await deliver_reminder(job)

    [(kind, to, body, buttons)] = FakeWhatsAppClient.all_sent()
    assert kind == "buttons" and to == WA_ID
    assert body.startswith("LEMBRE-SE: Sua consulta com Dra. Ana Souza")
    assert buttons == [
        (f"remconfirm|{job.reminder_id}", "Confirmar"),
        (f"remcancel|{job.reminder_id}", "Cancelar"),
        (f"remother|{job.reminder_id}", "Outro"),
    ]
    assert outcome.ok and not outcome.billable and outcome.channel == "whatsapp"
    assert outcome.wam_id == "wamid.buttons"
    assert outcome.history_body.endswith("(opções: Confirmar, Cancelar, Outro)")
    assert outcome.history_interactive["kind"] == "buttons"


async def test_inside_the_window_without_prompt_it_is_plain_text(db):  # noqa: F811
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world, with_prompt=False, inbound_ago=timedelta(hours=2)))

    [(kind, to, body)] = FakeWhatsAppClient.all_sent()
    assert kind == "text" and to == WA_ID and "opções" not in body
    assert outcome.ok and outcome.history_interactive is None


# ---- WhatsApp, outside the window -------------------------------------------


async def test_outside_the_window_while_unapproved_the_plain_template_goes(db):  # noqa: F811
    world = await seed_world(db)
    job = _job(world, inbound_ago=timedelta(hours=30))

    outcome = await deliver_reminder(job)

    [(kind, to, template, lang, variables, payloads)] = FakeWhatsAppClient.all_sent()
    assert (kind, to, template, lang) == ("template", WA_ID, "appointment_reminder", "pt_BR")
    assert variables == [single_line_text(job.content)]
    assert payloads is None
    assert outcome.ok and outcome.billable and outcome.error_code == "plain_template"


async def test_outside_the_window_once_approved_the_v2_template_has_the_buttons(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "REMINDER_V2_TEMPLATE_APPROVED", True, raising=False)
    world = await seed_world(db)
    job = _job(world)  # never wrote: outside the window

    outcome = await deliver_reminder(job)

    [(_, _, template, _, variables, payloads)] = FakeWhatsAppClient.all_sent()
    assert template == "lembrete_consulta_v2"
    assert len(variables) == 6 and variables[0] == "Sua consulta"
    assert payloads == [
        f"remconfirm|{job.reminder_id}",
        f"remcancel|{job.reminder_id}",
        f"remother|{job.reminder_id}",
    ]
    assert outcome.ok and outcome.billable and outcome.error_code is None


async def test_a_template_meta_refuses_falls_back_to_the_plain_one(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "REMINDER_V2_TEMPLATE_APPROVED", True, raising=False)
    FakeWhatsAppClient.failing_templates = {"lembrete_consulta_v2"}
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world))

    [(_, _, template, _, _, payloads)] = FakeWhatsAppClient.all_sent()
    assert template == "appointment_reminder" and payloads is None
    assert outcome.ok and outcome.error_code == "plain_template"


async def test_after_two_confirmations_outside_the_window_no_buttons_ever(db, monkeypatch):  # noqa: F811
    monkeypatch.setattr(get_settings(), "REMINDER_V2_TEMPLATE_APPROVED", True, raising=False)
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world, with_prompt=False))

    [(_, _, template, _, _, payloads)] = FakeWhatsAppClient.all_sent()
    assert template == "appointment_reminder" and payloads is None
    assert outcome.ok and outcome.error_code is None


# ---- Pix paid deposit keeps its 3-button variant ----------------------------


async def test_pix_paid_inside_the_window_keeps_confirm_reschedule_cancel(db):  # noqa: F811
    world = await seed_world(db)
    job = _job(world, deposit_paid=True, inbound_ago=timedelta(hours=1))

    await deliver_reminder(job)

    [(_, _, _, buttons)] = FakeWhatsAppClient.all_sent()
    assert buttons == [
        (f"remconfirm|{job.reminder_id}", "Confirmar"),
        (f"apptresched|{world.appointment.id}", "Reagendar"),
        (f"apptcancel|{world.appointment.id}", "Cancelar"),
    ]


async def test_pix_paid_outside_the_window_uses_the_deposit_template(db):  # noqa: F811
    world = await seed_world(db)  # v2 template NOT approved: irrelevant for the deposit one
    job = _job(world, deposit_paid=True)

    outcome = await deliver_reminder(job)

    [(_, _, template, _, variables, payloads)] = FakeWhatsAppClient.all_sent()
    assert template == "appointment_reminder_deposit"
    assert variables == [single_line_text(job.content)]
    assert payloads == [
        f"remconfirm|{job.reminder_id}",
        f"apptresched|{world.appointment.id}",
        f"apptcancel|{world.appointment.id}",
    ]
    assert outcome.ok and outcome.billable


# ---- WhatsApp failures -----------------------------------------------------


async def test_a_whatsapp_patient_without_phone_is_a_permanent_no_channel(db):  # noqa: F811
    world = await seed_world(db, wa_id=None)

    outcome = await deliver_reminder(_job(world))

    assert FakeWhatsAppClient.created == []
    assert (outcome.ok, outcome.error_code, outcome.permanent) == (False, "no_channel", True)


async def test_missing_clinic_credentials_are_permanent(db):  # noqa: F811
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world, waba_token=None))

    assert (outcome.ok, outcome.error_code, outcome.permanent) == (
        False,
        "whatsapp_credential_missing",
        True,
    )


async def test_a_meta_outage_is_a_retryable_failure(db):  # noqa: F811
    FakeWhatsAppClient.fail_everything = True
    world = await seed_world(db)

    outcome = await deliver_reminder(_job(world, inbound_ago=timedelta(hours=1)))

    assert (outcome.ok, outcome.error_code, outcome.permanent) == (False, "RuntimeError", False)


# ---- Portal ----------------------------------------------------------------


async def test_portal_patient_gets_the_chat_card_and_the_email_with_the_link(db, mail):  # noqa: F811
    sent, _ = mail
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    job = _job(world)

    outcome = await deliver_reminder(job)

    assert FakeWhatsAppClient.created == []  # never the WhatsApp client
    [row] = await outbound_messages(db, world.conversation.id)
    assert row.body.startswith("LEMBRE-SE: Sua consulta")
    assert [o["id"] for o in row.interactive["options"]] == [
        f"remconfirm|{job.reminder_id}",
        f"remcancel|{job.reminder_id}",
        f"remother|{job.reminder_id}",
    ]
    [mail_sent] = sent
    assert mail_sent["to"] == "maria@example.com"
    assert mail_sent["template"] == "appointment_reminder_patient"
    assert mail_sent["variables"]["link_line"] == (
        f"https://portal.exemplo/clinicas/?convite={world.tenant.id}\n"
    )
    assert mail_sent["variables"]["reminder_text"].startswith("LEMBRE-SE:")
    assert outcome.ok and outcome.channel == "email" and outcome.history_body is None


async def test_portal_patient_without_email_keeps_the_chat_copy_and_fails_permanently(db, mail):  # noqa: F811
    sent, state = mail
    state["email"] = None
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE, email=None)

    outcome = await deliver_reminder(_job(world))

    assert len(await outbound_messages(db, world.conversation.id)) == 1
    assert sent == []
    assert (outcome.ok, outcome.channel, outcome.error_code, outcome.permanent) == (
        False,
        "email",
        "no_email",
        True,
    )


async def test_a_portal_retry_with_the_card_already_there_writes_no_second_copy(db, mail):  # noqa: F811
    sent, _ = mail
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)

    await deliver_reminder(_job(world, attempt=2, chat_written=True))

    assert await outbound_messages(db, world.conversation.id) == []
    assert len(sent) == 1


async def test_a_portal_retry_without_the_card_writes_it(db, mail):  # noqa: F811
    sent, _ = mail
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)

    await deliver_reminder(_job(world, attempt=2, chat_written=False))

    assert len(await outbound_messages(db, world.conversation.id)) == 1
    assert len(sent) == 1


async def test_a_crash_reports_the_patients_own_channel(db, monkeypatch):  # noqa: F811
    async def _boom(job):
        raise RuntimeError("bug")

    monkeypatch.setattr(reminder_delivery, "_deliver_portal", _boom)
    monkeypatch.setattr(reminder_delivery, "_deliver_whatsapp", _boom)
    portal = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)
    wa = await seed_world(db, phone_number_id="pnid-2")

    portal_outcome = await deliver_reminder(_job(portal))
    wa_outcome = await deliver_reminder(_job(wa))

    assert (portal_outcome.ok, portal_outcome.channel) == (False, "email")
    assert (wa_outcome.ok, wa_outcome.channel) == (False, "whatsapp")


async def test_a_transient_mail_failure_is_retryable(db, mail):  # noqa: F811
    _, state = mail
    state["outcome"] = EmailOutcome.SEND_FAILED
    world = await seed_world(db, channel=CHANNEL_BRAIN_MESSAGE)

    outcome = await deliver_reminder(_job(world))

    assert (outcome.ok, outcome.error_code, outcome.permanent) == (
        False,
        "email_send_failed",
        False,
    )
