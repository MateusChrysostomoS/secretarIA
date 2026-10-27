"""POST /appointments/{id}/edit - Editar/Remarcar tells the patient (TASK-032 R7 §1/§2/§3)."""

# ruff: noqa: F811

from datetime import timedelta
from zoneinfo import ZoneInfo

import pytest
from httpx import AsyncClient

from secretaria.models import AppointmentStatus, FlowState
from secretaria.services.calendar import GoogleTokenRevokedError
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


async def test_a_revoked_google_grant_asks_to_reconnect_and_changes_nothing(
    client: AsyncClient, db, acting
):
    """Google refused the clinic's grant (invalid_grant): 409 `google_reconnect_required`,
    never the 502 "tente de novo em instantes" - waiting does not bring it back."""
    world = await _world(db, acting)
    FakeCalendar.get("tenant").fail = GoogleTokenRevokedError("refresh token rejected")

    response = await _post(
        client, world.appointment.id, start_at=_iso(world.start_at + timedelta(hours=3))
    )

    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "google_reconnect_required",
        "message": (
            "A conexão da clínica com o Google Agenda expirou. Reconecte em Contexto → Google."
        ),
    }
    assert (await reload_appointment(db, world.appointment.id)).start_at.replace(
        tzinfo=None
    ) == world.start_at.replace(tzinfo=None)
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


async def test_long_edit_text_keeps_every_change_and_the_current_footer(db, acting):
    from secretaria.models import Appointment, Tenant
    from secretaria.services.clinic_action_notice import edit_text
    from secretaria.services.reminder_text import load_reminder_content
    from secretaria.services.staff_appointment_edit import FieldChange

    world = await _world(db, acting)
    changes = [
        FieldChange("Paciente", "A" * 120, "B" * 120),
        FieldChange("Convênio", "C" * 120, "D" * 120),
        FieldChange("Médico", "E" * 255, "F" * 255),
    ]
    async with db() as session:
        content = await load_reminder_content(
            session,
            await session.get(Tenant, world.tenant.id),
            await session.get(Appointment, world.appointment.id),
        )
    text = edit_text(changes, content)
    for change in changes:
        assert f"{change.before} → {change.after}" in text
    assert text.endswith("com a equipe da Clínica Olhar.") and "Agora:" in text


async def test_long_multi_field_portal_edit_preserves_complete_detail_and_current_footer(
    client, db, acting
):
    from tests._r7_support import add_professional, set_appointment

    world = await _world(
        db, acting, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, professional_name="Ana " * 63
    )
    new_doctor = "Bruno " * 42
    doctor_id = await add_professional(db, world.tenant.id, new_doctor)
    await set_appointment(db, world.appointment.id, insurance="A" * 120, attendee_name="C" * 120)
    response = await _post(
        client,
        world.appointment.id,
        insurance="B" * 120,
        attendee_name="D" * 120,
        professional_id=str(doctor_id),
    )
    assert response.status_code == 200
    rows = await outbound_messages(db, world.conversation.id)
    detail = next(row.body for row in rows if row.interactive is None)
    assert "A" * 120 + " → " + "B" * 120 in detail
    assert "C" * 120 + " → " + "D" * 120 in detail
    assert "Ana " * 62 in detail and new_doctor.strip() in detail
    assert "Agora:" in detail and detail.endswith(new_doctor.strip() + ".")
    assert len([row for row in rows if row.interactive is not None]) == 1


# --------------------------------------------------------------------------
# A PAID sinal and a new SERVICE (plan 2026-10-26 Task 7). Sinal: 10000 cents at 30 %.
# --------------------------------------------------------------------------

PRICED = [
    {"name": "Consulta", "price": "R$ 200,00", "duration_min": 30, "is_active": True},
    {"name": "Avaliação", "price": "R$ 500,00", "duration_min": 50, "is_active": True},
    {"name": "Retorno", "price": "R$ 100,00", "duration_min": 20, "is_active": True},
    {"name": "Sem preço", "duration_min": 30, "is_active": True},
]


@pytest.fixture
def provider(monkeypatch):
    from secretaria.services.payments import registry
    from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
    from tests.payments_support import FakeProvider

    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201pix")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _make_paid(db, world) -> None:
    from secretaria.models import Tenant
    from secretaria.models.clinic_payment_account import ACCOUNT_CONNECTED
    from secretaria.services import tenant_config as cfg
    from secretaria.services.payments import accounts
    from tests._reminders_v2 import seed_paid_deposit

    await seed_paid_deposit(db, world)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.appointment_types = PRICED
        tenant.pix_deposit_enabled = True
        tenant.pix_deposit_percent = 30
        tenant.pix_difference_refund_min_days = 2
        tenant.deposit_hold_ttl_minutes = 30
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="token")
        row.status = ACCOUNT_CONNECTED
        await session.commit()


async def _paid_world(db, acting, **kwargs):
    world = await _world(db, acting, **kwargs)
    await _make_paid(db, world)
    return world


async def _adjustments(db):
    from sqlalchemy import select

    from secretaria.models.deposit_adjustment import DepositAdjustment

    async with db() as session:
        return list(await session.scalars(select(DepositAdjustment)))


async def test_paid_dearer_service_answers_awaiting_payment_and_sends_the_pix(
    client: AsyncClient, db, acting, provider
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _paid_world(db, acting)

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["appointment_type"] == "Consulta"  # changes only after the payment
    [adjustment] = await _adjustments(db)
    change = body["service_change"]
    assert change == {
        "status": "awaiting_payment",
        "diff_cents": 5000,
        "old_service": "Consulta",
        "new_service": "Avaliação",
        "adjustment_id": str(adjustment.id),
        "expires_at": change["expires_at"],
        "patient_notice": "whatsapp_sent",
    }
    assert change["expires_at"] is not None
    assert "000201pix" not in response.text  # the Pix code goes to the patient only
    assert body["patient_notice"] == "whatsapp_sent"
    texts = [item[2] for item in sent()]
    assert len(texts) == 1  # ONE message: the explanation and the code together
    assert "R$ 50,00" in texts[0] and "só muda depois" in texts[0] and "Avaliação" in texts[0]
    # the clinic asked for it, not the patient: never "Para trocar sua consulta…"
    assert texts[0].startswith("A clínica quer trocar o serviço da sua consulta para Avaliação.")
    assert "Para trocar sua consulta" not in texts[0]
    assert texts[0].rstrip().endswith("```000201pix```")  # the copia-e-cola, fenced, last
    assert not any(text.startswith("A clínica alterou") for text in texts)
    assert pool.calls == []  # no doctor e-mail while the difference is unpaid
    assert FakeCalendar.get("tenant").updated == []


async def test_paid_dearer_service_and_a_new_time_tell_the_time_now_and_the_pix(
    client: AsyncClient, db, acting, provider
):
    world = await _paid_world(db, acting)
    new = world.start_at + timedelta(days=1)

    response = await _post(client, world.appointment.id, service="Avaliação", start_at=_iso(new))

    body = response.json()
    assert response.status_code == 200 and body["service_change"]["status"] == "awaiting_payment"
    assert body["appointment_type"] == "Consulta" and body["status"] == "rescheduled"
    card, pix = sent()
    assert card[0] == "buttons" and "• Data:" in card[2] and "Serviço" not in card[2]
    assert "R$ 50,00" in pix[2] and pix[2].rstrip().endswith("```000201pix```")


async def test_paid_cheaper_service_applies_and_asks_the_refund_question(
    client: AsyncClient, db, acting, provider
):
    from secretaria.models.deposit_adjustment import STATUS_REFUND_REQUESTED
    from secretaria.services.payments import service_change

    pool = FakeArqPool()
    install_pool(pool)
    world = await _paid_world(db, acting)

    response = await _post(client, world.appointment.id, service="Retorno")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["appointment_type"] == "Retorno"
    [adjustment] = await _adjustments(db)
    assert body["service_change"]["status"] == "asked"
    assert body["service_change"]["diff_cents"] == 7000
    assert body["service_change"]["adjustment_id"] == str(adjustment.id)
    assert body["service_change"]["expires_at"] is None
    card, question = sent()
    assert card[0] == "buttons" and "• Serviço: Consulta → Retorno" in card[2]
    assert question == (
        "buttons",
        WA_ID,
        service_change.refund_question_text("Retorno", 7000),
        service_change.refund_question_buttons(adjustment.id),
    )
    assert body["service_change"]["patient_notice"] == "whatsapp_sent"
    # exactly ONE "Consulta alterada" e-mail to the doctor
    assert [call[0] for call in pool.calls] == ["send_professional_edit_notification"]

    await tap(world, service_change.ACTION_REFUND_YES, adjustment.id)
    [answered] = await _adjustments(db)
    assert answered.status == STATUS_REFUND_REQUESTED


async def test_paid_dearer_service_with_the_provider_down_applies_and_tells_the_patient(
    client: AsyncClient, db, acting, provider
):
    """Follow-up B (2026-10-27): no Pix could be created - the clinic's change stands now, the
    patient reads that the difference is settled with the clinic, the clinic gets ONE item."""
    from sqlalchemy import select

    from secretaria.models import PaymentAttentionItem
    from secretaria.services.payments import service_change
    from secretaria.services.payments.provider import ProviderError

    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    world = await _paid_world(db, acting)

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["appointment_type"] == "Avaliação"
    [adjustment] = await _adjustments(db)
    assert body["service_change"]["status"] == "applied_now"
    assert body["service_change"]["diff_cents"] == 5000
    assert body["service_change"]["adjustment_id"] == str(adjustment.id)
    assert body["service_change"]["patient_notice"] == "whatsapp_sent"
    card, money = sent()
    assert card[0] == "buttons" and "• Serviço: Consulta → Avaliação" in card[2]
    assert money[2] == service_change.collect_due_text("Avaliação", 5000, source="hub")
    assert "R$ 50,00" in money[2] and "clínica" in money[2]
    async with db() as session:
        [item] = list(await session.scalars(select(PaymentAttentionItem)))
    assert (item.reason, item.amount_cents) == ("service_diff_collect_due", 5000)


async def test_paid_dearer_service_outside_the_window_says_so_and_sends_no_code(
    client: AsyncClient, db, acting, provider
):
    world = await _paid_world(db, acting, last_inbound_at=recent(30))

    response = await _post(client, world.appointment.id, service="Avaliação")

    body = response.json()
    assert response.status_code == 200
    assert body["service_change"]["status"] == "awaiting_payment"
    assert body["service_change"]["patient_notice"] == "whatsapp_outside_window"
    assert body["patient_notice"] == "whatsapp_outside_window"
    assert body["whatsapp_link"] == f"https://wa.me/{WA_ID}"
    assert sent() == []


async def test_a_portal_patient_gets_the_refund_question_card_in_the_chat(
    client: AsyncClient, db, acting, provider
):
    world = await _paid_world(
        db, acting, channel=CHANNEL_BRAIN_MESSAGE, wa_id=None, email="m@x.com"
    )

    response = await _post(client, world.appointment.id, service="Retorno")

    assert response.json()["service_change"]["patient_notice"] in (
        "portal_chat",
        "portal_chat_email",
    )
    [adjustment] = await _adjustments(db)
    cards = [
        row.interactive
        for row in await outbound_messages(db, world.conversation.id)
        if row.interactive is not None
    ]
    assert len(cards) == 2  # "A clínica alterou" + the refund question
    assert "diferença de volta" in cards[1]["body"]
    assert f"svcdiffyes|{adjustment.id}" in str(cards[1])
    assert sent() == []


async def test_paid_service_change_refused_is_422_and_tells_nobody(
    client: AsyncClient, db, acting, provider
):
    world = await _paid_world(db, acting)

    response = await _post(client, world.appointment.id, service="Sem preço")

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert (detail["code"], detail["reason"]) == ("service_change_blocked", "price_unavailable")
    assert "Nada foi alterado" in detail["message"]
    assert sent() == [] and await _adjustments(db) == []
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"


async def test_paid_service_change_on_another_clinics_appointment_is_404(
    client: AsyncClient, db, acting, provider
):
    world = await _paid_world(db, acting)
    foreign = await seed_world(db, phone_number_id="pnid-9", start_at=world.start_at)
    await _make_paid(db, foreign)
    acting["tenant_id"] = world.tenant.id

    response = await _post(client, foreign.appointment.id, service="Avaliação")

    assert response.status_code == 404
    assert provider.calls == [] and await _adjustments(db) == []
    assert sent() == [] and FakeCalendar.instances == {}
    assert (await reload_appointment(db, foreign.appointment.id)).appointment_type == "Consulta"


async def test_paid_service_change_outside_a_restricted_doctors_scope_is_404(
    client: AsyncClient, db, acting, provider
):
    from secretaria.services.agenda_visibility import AgendaViewer
    from tests._agenda_viewer import view_as
    from tests._r7_support import add_professional

    world = await _paid_world(db, acting, professional_name="Dra. Ana")
    beto = await add_professional(db, world.tenant.id, "Dr. Beto")
    view_as(AgendaViewer("own", beto))

    hidden = await _post(client, world.appointment.id, service="Avaliação")

    assert hidden.status_code == 404
    assert provider.calls == [] and await _adjustments(db) == [] and sent() == []

    view_as(AgendaViewer("own", world.appointment.professional_id))  # her own: as before
    own = await _post(client, world.appointment.id, service="Avaliação")
    assert own.status_code == 200 and own.json()["service_change"]["status"] == "awaiting_payment"


async def test_cancelling_with_a_difference_pix_waiting_says_it_was_cancelled(
    client: AsyncClient, db, acting, provider
):
    """Browser proof 2026-10-26: the clinic's cancel result and the patient's cancel message
    did not mention the pending difference Pix the cancellation voided."""
    pool = FakeArqPool()
    install_pool(pool)
    from tests._r7_support import set_appointment

    world = await _paid_world(db, acting)
    await set_appointment(db, world.appointment.id, phone=WA_ID)
    edited = await _post(client, world.appointment.id, service="Avaliação")
    assert edited.json()["service_change"]["status"] == "awaiting_payment"

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True}
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["voided_difference_cents"] == 5000
    # the patient's cancel notice (WhatsApp: the cancellation job) carries the sentence
    assert body["patient_notice"] == "whatsapp_queued", pool.calls
    [job] = [call for call in pool.calls if call[0] == "send_cancellation_notice"]
    deposit_notice = job[5]
    assert deposit_notice.endswith(
        "O Pix da diferença do sinal (R$ 50,00) foi cancelado: não pague esse código."
    )
    [adjustment] = await _adjustments(db)
    assert adjustment.status == "cancelled"


async def test_a_plain_cancel_reports_no_voided_difference(
    client: AsyncClient, db, acting, provider
):
    world = await _paid_world(db, acting)

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True}
    )

    assert response.status_code == 200
    assert response.json()["voided_difference_cents"] is None
