"""Compareceu -> the post-consult message now, once per appointment (TASK-032 R7 §2)."""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

from httpx import AsyncClient
from sqlalchemy import update

from secretaria.models import AppointmentStatus, Message
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.patient_context import find_post_consult_followup
from tests._r7_support import (  # noqa: F401
    acting,
    patch_status,
    r7,
    recent,
    sent,
    set_appointment,
    set_tenant,
    setup_world,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment

DEFAULT = (
    "Como foi a sua consulta com Dra. Ana? Conte pra gente como você está se sentindo. "
    "Se precisar de algo, é só responder por aqui."
)


def _just_now() -> datetime:
    return datetime.now(UTC) - timedelta(minutes=40)


async def test_attended_sends_the_clinics_own_text_and_marks_the_appointment(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=_just_now())
    await set_tenant(db, world.tenant.id, post_consult_message="Obrigado pela visita! 💙")

    response = await patch_status(client, world.appointment.id, "attended")

    assert response.status_code == 200
    assert response.json()["patient_notice"] == "whatsapp_sent"
    assert sent() == [("text", WA_ID, "Obrigado pela visita! 💙")]
    assert (await reload_appointment(db, world.appointment.id)).post_consult_notified_at


async def test_without_a_configured_text_the_default_asks_how_it_went(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=_just_now(), professional_name="Dra. Ana")

    await patch_status(client, world.appointment.id, "attended")

    assert sent() == [("text", WA_ID, DEFAULT)]


async def test_marked_twice_or_refused_correction_it_is_sent_once(client: AsyncClient, db, acting):
    world = await setup_world(db, acting, start_at=_just_now())

    await patch_status(client, world.appointment.id, "attended")
    await patch_status(client, world.appointment.id, "attended")  # no-op
    refused = await patch_status(client, world.appointment.id, "no_show")
    assert refused.status_code == 409 and refused.json()["detail"]["code"] == "not_live"
    again = await patch_status(
        client, world.appointment.id, "attended"
    )  # repeated original outcome stays a no-op

    assert again.status_code == 200 and again.json()["patient_notice"] is None
    assert len(sent()) == 1


async def test_not_delivered_releases_the_marker_for_the_next_open_follow_up(
    client: AsyncClient, db, acting
):
    world = await setup_world(db, acting, start_at=_just_now(), last_inbound_at=recent(30))

    response = await patch_status(client, world.appointment.id, "attended")

    assert response.json()["patient_notice"] == "whatsapp_outside_window"
    assert sent() == []
    assert (await reload_appointment(db, world.appointment.id)).post_consult_notified_at is None


async def test_a_portal_patient_gets_it_in_the_chat_and_the_next_open_does_not_repeat_it(
    client: AsyncClient, db, acting, r7
):
    start = datetime.now(UTC) - timedelta(hours=3)
    world = await setup_world(
        db, acting, start_at=start, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="m@x.com"
    )
    # Marked "Compareceu" while the consult was still running: the message is older
    # than the consult's end, which alone would not stop the next-open question.
    await set_appointment(db, world.appointment.id, end_at=datetime.now(UTC) + timedelta(hours=1))

    response = await patch_status(client, world.appointment.id, "attended")
    assert response.json()["patient_notice"] == "portal_chat_email"
    assert len(await outbound_messages(db, world.conversation.id)) == 1

    # Later: the consult ended AFTER every message of the conversation, so only the
    # marker can stop the next open from asking "como foi?" a second time.
    async with db() as session:
        await session.execute(
            update(Message)
            .where(Message.conversation_id == world.conversation.id)
            .values(created_at=datetime.now(UTC) - timedelta(hours=2))
        )
        await session.commit()
    await set_appointment(db, world.appointment.id, end_at=datetime.now(UTC) - timedelta(minutes=1))
    async with db() as session:
        followup = await find_post_consult_followup(
            session, world.tenant.id, world.patient.id, world.conversation.id
        )
    assert followup is None


async def test_without_the_marker_the_next_open_follow_up_is_unchanged(db, acting):
    start = datetime.now(UTC) - timedelta(hours=3)
    world = await setup_world(db, acting, start_at=start, last_inbound_at=None)
    await set_appointment(db, world.appointment.id, status=AppointmentStatus.ATTENDED)

    async with db() as session:
        followup = await find_post_consult_followup(
            session, world.tenant.id, world.patient.id, world.conversation.id
        )

    assert followup is not None and followup["id"] == str(world.appointment.id)
