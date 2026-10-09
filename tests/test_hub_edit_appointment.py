"""POST /appointments/{id}/edit - Editar/Remarcar tells the patient (TASK-032 R7 §1/§2/§3)."""

# ruff: noqa: F811

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from secretaria.models import AppointmentStatus, FlowState
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import STEP_EDIT_MENU
from secretaria.workers.shared import reminder_actions as ra
from secretaria.workers.shared.greeting import _format_appointment_when
from tests._edit_flow_support import draft_for
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeArqPool,
    FakeCalendar,
    acting,
    install_pool,
    patch_status,
    r7,
    recent,
    sent,
    set_tenant,
    setup_world,
    staff_rows,
    tap,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import reminder_rows, set_conversation
from tests._reminders_v2 import WA_ID, outbound_messages, reload_appointment, seed_world
from tests.test_appointment_edit_apply import _apply, _edit

SP = ZoneInfo("America/Sao_Paulo")
SERVICES = [
    {"name": "Consulta", "is_active": True, "duration_min": 30},
    {"name": "Retorno", "is_active": True, "duration_min": 40},
]


async def _world(db, acting, **kwargs):
    kwargs.setdefault("google_event_id", "evt-1")
    world = await setup_world(db, acting, **kwargs)
    await set_tenant(db, world.tenant.id, appointment_types=SERVICES)
    return world


async def _post(client: AsyncClient, appointment_id, **body):
    return await client.post(f"{CALENDAR}/appointments/{appointment_id}/edit", json=body)


def _iso(value) -> str:
    return value.isoformat().replace("+00:00", "Z")


async def test_a_new_time_moves_everything_and_sends_the_change_card(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _world(db, acting, confirmation_count=1, status=AppointmentStatus.CONFIRMED)
    new = world.start_at + timedelta(days=1, hours=1)

    response = await _post(client, world.appointment.id, start_at=_iso(new))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "rescheduled" and body["confirmation_count"] == 0
    assert body["patient_notice"] == "whatsapp_sent" and body["whatsapp_link"] is None
    old, now_ = world.start_at.astimezone(SP), new.astimezone(SP)
    expected = (
        "A clínica alterou sua consulta:\n"
        f"• Data: {old:%d/%m/%Y} → {now_:%d/%m/%Y}\n"
        f"• Horário: {old:%H:%M} → {now_:%H:%M}\n"
        "\n"
        f"Agora: {now_:%d/%m/%Y} às {now_:%H:%M} com a equipe da Clínica Olhar."
    )
    [row] = await staff_rows(db, world.appointment.id, "staff_edit")
    assert sent() == [
        (
            "buttons",
            WA_ID,
            expected,
            [
                (f"remconfirm|{row.id}", "Confirmar"),
                (f"remcancel|{row.id}", "Cancelar"),
                (f"remedit|{row.id}", "Alterar Dados"),
            ],
        )
    ]
    # the reminders follow the new time (R2 hook), the doctor is e-mailed (R6 outbox)
    pending = [r for r in await reminder_rows(db, world.appointment.id) if r.status == "pending"]
    assert pending and all(
        r.appointment_start_at.replace(tzinfo=None) == new.replace(tzinfo=None) for r in pending
    )
    assert [call[0] for call in pool.calls] == ["send_professional_edit_notification"]


async def test_confirmar_on_the_change_card_counts_and_an_older_card_says_horario_antigo(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, professional_name="Dra. Ana")
    await patch_status(client, world.appointment.id, "confirmed")
    [old_card] = await staff_rows(db, world.appointment.id, "staff_confirm")
    new = world.start_at + timedelta(hours=2)
    await _post(client, world.appointment.id, start_at=_iso(new))
    [edit_card] = await staff_rows(db, world.appointment.id, "staff_edit")

    await tap(world, "remconfirm", old_card.id)
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 0
    when = _format_appointment_when(new, "America/Sao_Paulo")
    assert sent()[-1] == ("text", WA_ID, ra.MOVED_TEXT.format(when=when))

    await tap(world, "remconfirm", edit_card.id)
    assert (await reload_appointment(db, world.appointment.id)).confirmation_count == 1


async def test_a_convenio_only_change_keeps_the_status_and_mails_no_doctor(
    client: AsyncClient, db, acting
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _world(db, acting)

    response = await _post(client, world.appointment.id, insurance="Unimed")

    assert response.json()["status"] == "scheduled"
    assert "• Convênio: não informado → Unimed" in sent()[0][2]
    assert pool.calls == []


async def test_a_contact_phone_change_goes_to_the_patients_own_number(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting)

    response = await _post(client, world.appointment.id, phone="+55 11 97777-6666")

    assert response.status_code == 200 and response.json()["phone"] == "5511977776666"
    assert sent()[0][1] == WA_ID  # identity, never the new contact
    assert "• Telefone de contato: não informado → 5511977776666" in sent()[0][2]
    assert FakeCalendar.instances == {}


async def test_a_portal_patient_gets_the_card_in_the_chat_and_an_email(
    client: AsyncClient, db, acting, r7
):
    world = await _world(db, acting, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="m@x.com")

    response = await _post(client, world.appointment.id, attendee_name="João Pedro")

    assert response.json()["patient_notice"] == "portal_chat_email"
    [message] = await outbound_messages(db, world.conversation.id)
    assert message.interactive["body"].startswith("A clínica alterou a consulta de João Pedro:")
    assert sent() == [] and len(r7["mail"]) == 1


async def test_outside_the_window_the_clinics_standing_yes_sends_the_paid_text(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, last_inbound_at=recent(30))
    await set_tenant(db, world.tenant.id, paid_notices_auto_approved=True)

    response = await _post(client, world.appointment.id, service="Retorno")

    assert response.json()["patient_notice"] == "whatsapp_sent"
    assert sent()[0][0] == "template"


async def test_without_authorisation_outside_the_window_the_edit_stands_and_says_so(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting, last_inbound_at=recent(30))

    response = await _post(client, world.appointment.id, service="Retorno")

    body = response.json()
    assert response.status_code == 200 and body["appointment_type"] == "Retorno"
    assert body["patient_notice"] == "whatsapp_outside_window"
    assert body["whatsapp_link"] == f"https://wa.me/{WA_ID}"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"notify_outside_window": True},
        {"insurance": "Unimed", "surprise": 1},
        {"start_at": "2030-01-01T10:00:00"},
        {"service": None},
        {"professional_id": "not-a-uuid"},
    ],
)
async def test_malformed_requests_are_422_and_touch_nothing(
    client: AsyncClient, db, acting, payload
):
    world = await _world(db, acting)

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/edit", json=payload
    )

    assert response.status_code == 422
    assert sent() == [] and FakeCalendar.instances == {}


async def test_business_refusals_carry_their_code(client: AsyncClient, db, acting):
    world = await _world(db, acting)
    FakeCalendar.get("tenant").free = False

    taken = await _post(
        client, world.appointment.id, start_at=_iso(world.start_at + timedelta(hours=3))
    )
    same = await _post(client, world.appointment.id, insurance=None)

    assert taken.status_code == 409 and taken.json()["detail"]["code"] == "slot_unavailable"
    assert same.status_code == 422 and same.json()["detail"]["code"] == "nothing_changed"
    assert sent() == []


async def test_another_clinics_appointment_is_404_and_google_is_untouched(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting)
    foreign = await seed_world(db, phone_number_id="pnid-9", start_at=world.start_at)

    response = await _post(client, foreign.appointment.id, insurance="Unimed")

    assert response.status_code == 404
    assert FakeCalendar.instances == {} and sent() == []


async def test_a_stale_alterar_dados_draft_cannot_overwrite_the_clinics_edit(
    client: AsyncClient, db, acting
):
    world = await _world(db, acting)
    draft = draft_for(
        {
            "id": str(world.appointment.id),
            "google_event_id": "evt-1",
            "appointment_type": "Consulta",
            "professional_id": None,
            "start_at": world.appointment.start_at,
            "end_at": world.appointment.end_at,
            "insurance": world.appointment.insurance,
            "attendee_name": None,
        }
    )
    await set_conversation(
        db,
        world,
        flow_state=FlowState.EDIT_BOOKING,
        flow_step=STEP_EDIT_MENU,
        flow_managing_appointment_id=world.appointment.id,
        flow_edit_draft=draft.to_json(),
    )
    new = world.start_at + timedelta(hours=2)
    await _post(client, world.appointment.id, start_at=_iso(new))

    await _apply(world, _edit(world, insurance="Amil", original=dict(draft.original)))

    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance != "Amil"
    assert row.start_at.replace(tzinfo=None) == new.replace(tzinfo=None)
