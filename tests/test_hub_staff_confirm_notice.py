"""Staff "Confirmar" tells the patient and reuses the reminder's tap flow (TASK-032 R7 §2)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from httpx import AsyncClient

from secretaria.core import database as core_database
from secretaria.models import AppointmentStatus, Tenant
from secretaria.services import reminder_opening
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._r7_support import (  # noqa: F401
    acting,
    patch_status,
    r7,
    recent,
    sent,
    set_appointment,
    set_tenant,
    setup_world,
    staff_rows,
    tap,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment

SP = ZoneInfo("America/Sao_Paulo")


def _expected_text(world, doctor: str = "Dra. Ana", subject: str = "sua consulta") -> str:
    local = world.start_at.astimezone(SP)
    return (
        f"Seu médico confirmou {subject} de {local:%d/%m/%Y} às {local:%H:%M} "
        f"com {doctor}. Você está ciente?"
    )


async def test_confirm_counts_once_and_sends_the_card_with_the_three_buttons(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, professional_name="Dra. Ana")

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "confirmed" and body["confirmation_count"] == 1
    assert body["patient_notice"] == "whatsapp_sent" and body["whatsapp_link"] is None
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")
    assert row.status == "sent" and row.warn_due_at is None and row.invalidated_at is None
    assert sent() == [
        (
            "buttons",
            WA_ID,
            _expected_text(world),
            [
                (f"remconfirm|{row.id}", "Confirmar"),
                (f"remcancel|{row.id}", "Cancelar"),
                (f"remedit|{row.id}", "Alterar Dados"),
            ],
        )
    ]


async def test_the_patient_confirming_the_card_reaches_two_and_stops_the_prompts(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    await patch_status(client, world.appointment.id, "confirmed")
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")

    await tap(world, "remconfirm", row.id)

    appointment = await reload_appointment(db, world.appointment.id)
    assert appointment.confirmation_count == 2
    when = _format_appointment_when(world.start_at, "America/Sao_Paulo")
    assert ("text", WA_ID, ra.CONFIRMED_TEXT.format(when=when)) in sent()
    async with core_database.async_session_factory() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        decision = await reminder_opening.decide_reminder_opening(
            session,
            tenant=tenant,
            patient_id=world.patient.id,
            last_activity_at=datetime.now(UTC) - timedelta(hours=7),
            now=datetime.now(UTC),
        )
    assert decision.reason == reminder_opening.SKIP_CONFIRMED_TWICE


async def test_cancelar_and_alterar_dados_follow_the_reminder_paths(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting)
    await patch_status(client, world.appointment.id, "confirmed")
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")
    reply = ra._ReplyContext(
        channel="whatsapp",
        conversation_id=world.conversation.id,
        patient_ref=WA_ID,
        inbound_body="x",
    )

    assert await ra.handle_reminder_button(reply, "remedit", str(row.id)) == (
        ra.CONTINUE_EDIT,
        str(world.appointment.id),
    )
    await tap(world, "remcancel", row.id)

    last = sent()[-1]
    assert last[0] == "buttons" and last[2].startswith("Tem certeza que quer cancelar")
    assert (
        await reload_appointment(db, world.appointment.id)
    ).status == AppointmentStatus.CONFIRMED


async def test_a_second_confirm_for_the_same_time_sends_nothing_more(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting)
    first = await patch_status(client, world.appointment.id, "confirmed")

    again = await patch_status(client, world.appointment.id, "confirmed")

    assert first.json()["patient_notice"] == "whatsapp_sent"
    assert again.status_code == 200 and again.json()["patient_notice"] is None
    assert len(sent()) == 1
    assert len(await staff_rows(db, world.appointment.id, "staff_confirm")) == 1


async def test_outside_the_window_it_asks_and_a_paid_retry_sends_the_template(
    client: AsyncClient, db, acting, r7
):
    world = await setup_world(db, acting, last_inbound_at=recent(30))

    refused = await patch_status(client, world.appointment.id, "confirmed")

    assert refused.json()["patient_notice"] == "whatsapp_outside_window"
    assert refused.json()["whatsapp_link"] == f"https://wa.me/{WA_ID}"
    assert sent() == []
    [retired] = await staff_rows(db, world.appointment.id, "staff_confirm")
    assert retired.invalidated_at is not None  # no card went out: a retry gets a fresh row

    paid = await patch_status(client, world.appointment.id, "confirmed", notify_outside_window=True)

    assert paid.json()["patient_notice"] == "whatsapp_sent"
    [(kind, to, template, _lang, variables, _payloads)] = sent()
    assert (kind, to, template) == ("template", WA_ID, "appointment_reminder")
    assert variables == [_expected_text(world, doctor="a equipe da Clínica Olhar")]
    assert len(r7["usage"]) == 1


async def test_the_clinics_standing_yes_needs_no_question(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, last_inbound_at=recent(30))
    await set_tenant(db, world.tenant.id, paid_notices_auto_approved=True)

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["patient_notice"] == "whatsapp_sent"
    assert sent()[0][0] == "template"


async def test_with_reminders_switched_off_the_text_goes_without_dead_buttons(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, v2=False)

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["patient_notice"] == "whatsapp_sent"
    assert sent()[0][0] == "text"
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")
    assert row.with_prompt is False


async def test_a_patient_who_already_confirmed_twice_gets_the_text_only(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, confirmation_count=2)
    await set_appointment(db, world.appointment.id, status=AppointmentStatus.CONFIRMED)

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["confirmation_count"] == 2
    assert sent()[0][0] == "text"


async def test_an_appointment_for_someone_else_names_them(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, attendee_name="João Pedro", professional_name="Dra. Ana")

    await patch_status(client, world.appointment.id, "confirmed")

    assert sent()[0][2] == _expected_text(world, subject="a consulta de João Pedro")


async def test_a_portal_patient_gets_the_card_in_the_chat_and_an_email(
    client: AsyncClient, db, acting, r7
):
    world = await setup_world(
        db, acting, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="maria@exemplo.com"
    )

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["patient_notice"] == "portal_chat_email"
    [row] = await staff_rows(db, world.appointment.id, "staff_confirm")
    assert row.channel == "chat"
    [message] = await outbound_messages(db, world.conversation.id)
    assert [o["id"] for o in message.interactive["options"]][0] == f"remconfirm|{row.id}"
    assert [m[1] for m in r7["mail"]] == ["clinic_message_patient"]
    assert sent() == []


async def test_a_confirm_after_the_start_counts_but_sends_nothing(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, start_at=datetime.now(UTC) - timedelta(minutes=10))

    response = await patch_status(client, world.appointment.id, "confirmed")

    assert response.json()["confirmation_count"] == 1
    assert response.json()["patient_notice"] is None and sent() == []
