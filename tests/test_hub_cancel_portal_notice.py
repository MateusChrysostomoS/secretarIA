"""POST /appointments/{id}/cancel: Portal patients are told too (TASK-032 R7 §2)."""

# ruff: noqa: F811

from httpx import AsyncClient

from secretaria.models import AppointmentStatus
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeArqPool,
    acting,
    install_pool,
    r7,
    recent,
    set_appointment,
    set_tenant,
    setup_world,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment

PORTAL_TEXT = (
    "O médico Dra. Ana desmarcou a sua consulta!\n\n"
    'Justificativa do médico: "Imprevisto"\n\n'
    "Para marcar um novo horário, é só me escrever por aqui."
)


async def _cancel(client: AsyncClient, appointment_id, **body):
    return await client.post(
        f"{CALENDAR}/appointments/{appointment_id}/cancel", json={"confirm": True, **body}
    )


async def _portal(db, acting, **kwargs):
    return await setup_world(
        db,
        acting,
        channel=CHANNEL_BRAIN_MESSAGE,
        wa_id=None,
        professional_name="Dra. Ana",
        **kwargs,
    )


async def test_a_portal_patient_gets_the_cancellation_in_the_chat_and_an_email(
    client: AsyncClient, db, acting, r7
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _portal(db, acting, email="maria@exemplo.com")

    response = await _cancel(client, world.appointment.id, justification="Imprevisto")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "cancelled" and body["patient_notice"] == "portal_chat_email"
    assert body["whatsapp_link"] is None
    [message] = await outbound_messages(db, world.conversation.id)
    assert message.body == PORTAL_TEXT
    assert [m[1] for m in r7["mail"]] == ["clinic_message_patient"]
    assert pool.calls == []  # the WhatsApp job never sees a Portal patient


async def test_a_portal_patient_with_a_contact_phone_is_still_told_in_the_chat(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _portal(db, acting, email=None)
    await set_appointment(db, world.appointment.id, phone="5511977776666")

    response = await _cancel(client, world.appointment.id)

    assert response.json()["patient_notice"] == "portal_chat"
    assert pool.calls == []


async def test_a_whatsapp_patient_keeps_todays_job_exactly(client: AsyncClient, db, acting):
    pool = FakeArqPool()
    install_pool(pool)
    world = await setup_world(db, acting, professional_name="Dra. Ana")
    await set_appointment(db, world.appointment.id, phone=WA_ID)

    response = await _cancel(client, world.appointment.id, justification="Imprevisto")

    assert response.json()["patient_notice"] == "whatsapp_queued"
    assert pool.calls == [
        (
            "send_cancellation_notice",
            str(world.tenant.id),
            str(world.appointment.id),
            "Dra. Ana",
            "Imprevisto",
            None,
            False,
        )
    ]


async def test_outside_the_window_the_job_is_still_queued_and_the_answer_says_so(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await setup_world(db, acting, last_inbound_at=recent(30))
    await set_appointment(db, world.appointment.id, phone=WA_ID)

    body = (await _cancel(client, world.appointment.id)).json()

    assert body["patient_notice"] == "whatsapp_outside_window"
    assert body["whatsapp_link"] == f"https://wa.me/{WA_ID}"
    assert pool.calls[0][-1] is False  # the job itself sends nothing without authorisation


async def test_the_clinics_standing_yes_authorises_the_paid_cancellation(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await setup_world(db, acting, last_inbound_at=recent(30))
    await set_appointment(db, world.appointment.id, phone=WA_ID)
    await set_tenant(db, world.tenant.id, paid_notices_auto_approved=True)

    body = (await _cancel(client, world.appointment.id)).json()

    assert body["patient_notice"] == "whatsapp_queued" and pool.calls[0][-1] is True


async def test_a_dead_queue_or_no_number_never_undoes_the_cancellation(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting)
    await set_appointment(db, world.appointment.id, phone=WA_ID)
    no_queue = await _cancel(client, world.appointment.id)
    assert no_queue.json()["patient_notice"] == "queue_unavailable"

    other = await setup_world(db, acting, phone_number_id="pnid-2", wa_id="5511900000002")
    install_pool(FakeArqPool())
    no_number = await _cancel(client, other.appointment.id)  # appointment.phone is empty
    assert no_number.json()["patient_notice"] == "no_channel"
    for appointment_id in (world.appointment.id, other.appointment.id):
        assert (await reload_appointment(db, appointment_id)).status == AppointmentStatus.CANCELLED
