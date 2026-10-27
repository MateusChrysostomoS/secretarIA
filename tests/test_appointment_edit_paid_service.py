"""The patient changes the SERVICE of a consultation whose sinal is PAID (plan 2026-10-26, Task 6).

The router only drafts and checks the window (`calendar_deferred`); at Confirmar the worker hands
the money to `services/payments/service_change.request_service_change` and acts on its answer:

* dearer by >= R$ 10 -> the patient gets the value, the Pix copia-e-cola and "só muda depois do
  pagamento"; the row and the agenda keep the OLD service; no e-mail to the doctor;
* cheaper (far enough ahead) -> applied now (row + agenda + ONE doctor notice) and the patient is
  asked "quer a diferença de volta?" with two fixed buttons that are ACTIONS on both channels;
* blocked (no price, a difference still open) -> one line, nothing changes;
* the Pix cannot be created (provider down, 2026-10-27) -> applied now, the clinic collects.

WhatsApp and the Portal share the same worker (`flow_runner._apply_flow_result`).
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from secretaria.ai.formatter import TextBubble
from secretaria.models import (
    FlowState,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.clinic_payment_account import ACCOUNT_CONNECTED
from secretaria.models.deposit_adjustment import (
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    STATUS_KEPT_CREDIT,
    STATUS_REFUND_REQUESTED,
    DepositAdjustment,
)
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from secretaria.services import appointment_edit as ae, flow_router as fr, tenant_config as cfg
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.payments import accounts, registry, service_change
from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
from secretaria.workers import tasks
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from secretaria.workers.shared import appointment_edit_support as support
from tests._edit_flow_support import EditCalendar
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, get_conversation, sent, turn, wire
from tests._reminders_v2 import (
    WA_ID,
    add_reminder,
    outbound_messages,
    reload_appointment,
    seed_paid_deposit,
    seed_world,
)
from tests.payments_support import FakeProvider

# Sinal of 30 % paid on R$ 10.000 cents (seed_paid_deposit). New sinal = price x 30 %.
CATALOG = [
    {"name": "Consulta", "price": "R$ 200,00", "duration_min": 30, "is_active": True},
    {"name": "Avaliação", "price": "R$ 500,00", "duration_min": 30, "is_active": True},
    {"name": "Retorno", "price": "R$ 100,00", "duration_min": 20, "is_active": True},
    {"name": "Sem preço", "duration_min": 30, "is_active": True},
]


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201pix")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _world(db, *, channel=None, min_days=2):  # noqa: F811
    kwargs = {"channel": channel} if channel else {}
    world = await seed_world(
        db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-old", **kwargs
    )
    await consent(db, world)
    await seed_paid_deposit(db, world)
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        tenant.appointment_types = CATALOG
        tenant.pix_deposit_enabled = True
        tenant.pix_deposit_percent = 30
        tenant.pix_difference_refund_min_days = min_days
        tenant.deposit_hold_ttl_minutes = 30
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="token")
        row.status = ACCOUNT_CONNECTED
        await session.commit()
        world.tenant = tenant
    return world


def _reply(world, channel=None):
    ref = world.patient.external_id if channel == CHANNEL_BRAIN_MESSAGE else WA_ID
    kwargs = {"channel": channel} if channel else {}
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=ref, inbound_body="x", **kwargs
    )


def _edit(world, service: str, minutes: int, calendar) -> dict:
    """What the router's Confirmar returns for a paid service change (`calendar_deferred`)."""
    start = world.start_at
    return {
        "appointment_id": world.appointment.id,
        "old_google_event_id": "evt-old",
        "google_event_id": "evt-old",
        "google_event_link": None,
        "appointment_type": service,
        "professional_id": None,
        "old_professional_id": None,
        "old_google_calendar_source": None,
        "insurance": None,
        "attendee_name": None,
        "start_at": start,
        "end_at": start + timedelta(minutes=minutes),
        "time_changed": False,
        "doctor_changed": False,
        "calendar_changed": False,
        "calendar": calendar,
        "patient_name": "Maria",
        "calendar_deferred": True,
    }


async def _confirm(world, edit, channel=None):
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body=f"{ae.EDIT_APPLIED}\n\nServiço: {edit['appointment_type']}"),
            fr.MenuBubble(body="menu", labels=["Agendar", "Outro"]),
        ],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
    await tasks._apply_flow_result(
        _reply(world, channel), result, WA_ID, tenant=world.tenant, waba_token="t"
    )


async def _adjustments(db) -> list[DepositAdjustment]:  # noqa: F811
    async with db() as session:
        return list((await session.scalars(select(DepositAdjustment))).all())


async def _count(db, model) -> int:  # noqa: F811
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(model))


def _texts() -> list[str]:
    return [item[2] for item in sent() if item[0] in ("text", "buttons", "list")]


# --------------------------------------------------------------------------
# dearer: the Pix of the difference, nothing changes yet
# --------------------------------------------------------------------------


async def test_dearer_service_sends_the_difference_pix_and_changes_nothing(db, provider):  # noqa: F811
    world = await _world(db)
    calendar = EditCalendar()

    await _confirm(world, _edit(world, "Avaliação", 30, calendar))

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Consulta"  # the service changes only after the payment
    assert calendar.detail_updates == [] and calendar.created == []
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.diff_cents) == (STATUS_AWAITING_PAYMENT, 5000)
    texts = _texts()
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in texts)
    [head] = [text for text in texts if "R$ 50,00" in text]
    assert "só muda depois" in head and "Avaliação" in head
    # ONE message: the explanation AND the copia-e-cola (fenced, last) - never a bubble of its own
    assert "```000201pix```" in head and head.rstrip().endswith("```000201pix```")
    assert "000201pix" not in texts and "```000201pix```" not in texts
    assert await _count(db, ProfessionalEditNotice) == 0  # no e-mail to the doctor yet
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
    assert deposit.reschedule_count == 0


async def test_dearer_service_with_the_provider_down_changes_now_and_the_clinic_collects(
    db,  # noqa: F811
    provider,
):
    """Follow-up B (2026-10-27): no Pix could be created - the change stands at Confirmar, the
    patient is told the difference is settled with the clinic, the clinic gets ONE item."""
    from secretaria.services.payments.provider import ProviderError

    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    world = await _world(db)
    calendar = EditCalendar()

    await _confirm(world, _edit(world, "Avaliação", 30, calendar))

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Avaliação"
    [(event_id, _start, _end, summary, _description)] = calendar.detail_updates
    assert event_id == "evt-old" and summary == "Avaliação - Maria"
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.diff_cents) == ("collect_due", 5000)
    async with db() as session:
        [item] = (await session.scalars(select(PaymentAttentionItem))).all()
    assert (item.reason, item.amount_cents) == ("service_diff_collect_due", 5000)
    texts = _texts()
    assert texts[0].startswith(ae.EDIT_APPLIED)
    assert service_change.collect_due_text("Avaliação", 5000) in texts
    assert not any("000201pix" in text for text in texts)
    assert await _count(db, ProfessionalEditNotice) == 1


# --------------------------------------------------------------------------
# cheaper: applied now + the refund question with two fixed ACTION buttons
# --------------------------------------------------------------------------


async def _asked(db, channel=None):  # noqa: F811
    world = await _world(db, channel=channel)
    calendar = EditCalendar()
    await _confirm(world, _edit(world, "Retorno", 20, calendar), channel=channel)
    return world, calendar


async def test_cheaper_service_applies_now_and_asks_about_the_difference(db, provider):  # noqa: F811
    world, calendar = await _asked(db)

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno"
    assert (row.end_at - row.start_at) == timedelta(minutes=20)
    [(event_id, _start, _end, summary, _description)] = calendar.detail_updates
    assert event_id == "evt-old" and summary == "Retorno - Maria"
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.diff_cents) == (STATUS_ASKED, 7000)
    assert await _count(db, ProfessionalEditNotice) == 1  # ONE "Consulta alterada"
    texts = _texts()
    assert texts[0].startswith(ae.EDIT_APPLIED)
    [card] = [item for item in sent() if item[0] == "buttons"]
    assert card[2] == service_change.refund_question_text("Retorno", 7000)
    assert card[3] == service_change.refund_question_buttons(adjustment.id)
    assert provider.calls == []  # no charge on a cheaper service


async def test_yes_opens_one_item_for_the_clinic_and_never_refunds(db, provider):  # noqa: F811
    world, _calendar = await _asked(db)
    [adjustment] = await _adjustments(db)
    reply = _reply(world)

    await tasks._handle_action_button(reply, service_change.ACTION_REFUND_YES, str(adjustment.id))
    await tasks._handle_action_button(reply, service_change.ACTION_REFUND_YES, str(adjustment.id))

    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_REFUND_REQUESTED
    async with db() as session:
        [item] = (await session.scalars(select(PaymentAttentionItem))).all()
    assert (item.reason, item.suggested_refund_cents) == ("service_diff_refund_due", 7000)
    texts = _texts()
    assert texts[-2] == service_change.refund_answer_text("refund_requested", 7000)
    assert texts[-1] == service_change.refund_answer_text("already_answered", 7000)


async def test_no_keeps_the_difference_as_credit_without_an_item(db, provider):  # noqa: F811
    world, _calendar = await _asked(db)
    [adjustment] = await _adjustments(db)

    await tasks._handle_action_button(
        _reply(world), service_change.ACTION_REFUND_NO, str(adjustment.id)
    )

    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_KEPT_CREDIT
    assert await _count(db, PaymentAttentionItem) == 0
    assert _texts()[-1] == service_change.refund_answer_text("kept_credit", 7000)


async def test_a_tap_from_another_patient_changes_nothing(db, provider):  # noqa: F811
    world, _calendar = await _asked(db)
    [adjustment] = await _adjustments(db)
    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900002222")

    await tasks._handle_action_button(
        _reply(other), service_change.ACTION_REFUND_YES, str(adjustment.id)
    )

    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_ASKED
    assert await _count(db, PaymentAttentionItem) == 0


async def test_the_portal_gets_the_question_and_its_tap_is_the_same_action(db, provider):  # noqa: F811
    world, _calendar = await _asked(db, channel=CHANNEL_BRAIN_MESSAGE)
    [adjustment] = await _adjustments(db)
    cards = [m for m in await outbound_messages(db, world.conversation.id) if m.interactive]
    question = cards[-1]
    assert [o["title"] for o in question.interactive["options"]] == [
        service_change.LABEL_REFUND_YES,
        service_change.LABEL_REFUND_NO,
    ]

    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text=service_change.LABEL_REFUND_NO,
        interactive_reply_id=f"{service_change.ACTION_REFUND_NO}|{adjustment.id}",
    )
    await _send_bot_reply(reply)

    assert reply.action_button == (service_change.ACTION_REFUND_NO, str(adjustment.id))
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_KEPT_CREDIT
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert bodies[-1] == service_change.refund_answer_text("kept_credit", 7000)


# --------------------------------------------------------------------------
# applied now without a question, blocked, agenda failure
# --------------------------------------------------------------------------


async def test_cheaper_close_to_the_day_applies_without_asking(db, provider):  # noqa: F811
    world = await _world(db, min_days=5)  # 3 days ahead < 5: the clinic keeps the difference
    calendar = EditCalendar()

    await _confirm(world, _edit(world, "Retorno", 20, calendar))

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Retorno"
    assert len(calendar.detail_updates) == 1
    buttons = [item[3] for item in sent() if item[0] == "buttons"]
    assert not any("svcdiff" in button_id for card in buttons for button_id, _label in card)
    assert _texts()[0].startswith(ae.EDIT_APPLIED)
    [adjustment] = await _adjustments(db)
    assert adjustment.status == "clinic_kept"


async def test_a_service_without_a_price_is_refused_with_one_line(db, provider):  # noqa: F811
    world = await _world(db)
    calendar = EditCalendar()

    await _confirm(world, _edit(world, "Sem preço", 30, calendar))

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    assert calendar.detail_updates == [] and await _adjustments(db) == []
    texts = _texts()
    assert texts[0] == service_change.blocked_text("price_unavailable")
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in texts)


async def test_a_second_change_while_the_pix_is_open_is_refused(db, provider):  # noqa: F811
    world = await _world(db)
    await _confirm(world, _edit(world, "Avaliação", 30, EditCalendar()))

    await _confirm(world, _edit(world, "Retorno", 20, EditCalendar()))

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    assert len(await _adjustments(db)) == 1
    assert service_change.blocked_text("adjustment_pending") in _texts()


async def test_an_agenda_failure_rolls_the_whole_change_back(db, provider):  # noqa: F811
    world = await _world(db)
    calendar = EditCalendar(fail_update=True)

    await _confirm(world, _edit(world, "Retorno", 20, calendar))

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    assert await _adjustments(db) == []
    assert await _count(db, ProfessionalEditNotice) == 0
    assert not any(text.startswith(ae.EDIT_APPLIED) for text in _texts())


async def test_a_doctor_change_with_a_paid_sinal_is_still_refused(db, provider):  # noqa: F811
    world = await _world(db)
    edit = _edit(world, "Retorno", 20, EditCalendar())
    edit["professional_id"] = uuid4()
    edit["doctor_changed"] = True

    await _confirm(world, edit)

    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.professional_id) == ("Consulta", None)
    assert await _adjustments(db) == []


async def test_a_router_written_paid_service_change_is_never_charged_twice(db, provider):  # noqa: F811
    """A card confirmed before the sinal was paid already moved the agenda (no
    `calendar_deferred`): the worker refuses it as before instead of guessing the money."""
    world = await _world(db)
    edit = _edit(world, "Retorno", 20, EditCalendar())
    edit["calendar_deferred"] = False

    await _confirm(world, edit)

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    assert await _adjustments(db) == []


async def test_the_whole_turn_on_whatsapp_changes_the_service_and_asks(  # noqa: F811
    db, provider, monkeypatch  # noqa: F811
):
    """Reminder "Alterar Dados" -> "Mudar serviço" -> Retorno -> Confirmar, through the real
    router and worker: the agenda is written ONCE (by the worker), then the question."""
    calendar = EditCalendar()

    class _Service:
        @classmethod
        def from_tenant_config(cls, config):
            return calendar

    monkeypatch.setattr(support, "CalendarService", _Service)
    monkeypatch.setattr(fr, "_hold_windows", AsyncMock(return_value=[]))
    world = await _world(db)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2))

    await tasks._handle_action_button(_reply(world), "remedit", str(rid))
    await turn(db, world, ae.LABEL_EDIT_SERVICE)
    await turn(db, world, fr._service_row_title("Retorno"))
    assert (await get_conversation(db, world)).flow_step == fr.STEP_EDIT_CONFIRM
    assert calendar.detail_updates == []
    await turn(db, world, fr.LABEL_CONFIRM)

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno"
    assert len(calendar.detail_updates) == 1
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_ASKED
    [card] = [item for item in sent() if item[0] == "buttons" and "svcdiff" in item[3][0][0]]
    assert card[3] == service_change.refund_question_buttons(adjustment.id)
    assert (await get_conversation(db, world)).flow_state == FlowState.MENU


async def test_a_deferred_change_whose_sinal_is_no_longer_paid_still_reaches_the_agenda(db):  # noqa: F811
    world = await seed_world(
        db, start_at=datetime.now(UTC) + timedelta(days=3), google_event_id="evt-old"
    )
    calendar = EditCalendar()

    await _confirm(world, _edit(world, "Consulta", 20, calendar) | {"appointment_type": "Retorno"})

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno"
    assert len(calendar.detail_updates) == 1
    assert await _adjustments(db) == []


# --------------------------------------------------------------------------
# The confirmation card shows the difference BEFORE Confirmar (browser proof 2026-10-26)
# --------------------------------------------------------------------------


async def _card_for(db, monkeypatch, service: str):  # noqa: F811
    calendar = EditCalendar()

    class _Service:
        @classmethod
        def from_tenant_config(cls, config):
            return calendar

    monkeypatch.setattr(support, "CalendarService", _Service)
    monkeypatch.setattr(fr, "_hold_windows", AsyncMock(return_value=[]))
    world = await _world(db)
    rid = await add_reminder(db, world, kind="day", due_at=datetime.now(UTC) + timedelta(days=2))
    await tasks._handle_action_button(_reply(world), "remedit", str(rid))
    await turn(db, world, ae.LABEL_EDIT_SERVICE)
    await turn(db, world, fr._service_row_title(service))
    assert (await get_conversation(db, world)).flow_step == fr.STEP_EDIT_CONFIRM
    [card] = [
        item for item in sent()
        if item[0] == "buttons" and fr.LABEL_CONFIRM in str(item[3])
    ]
    return world, card[2]


async def test_the_confirmation_card_shows_the_difference_to_pay(db, provider, monkeypatch):  # noqa: F811
    world, body = await _card_for(db, monkeypatch, "Avaliação")

    assert body.rstrip().endswith(service_change.confirm_card_line_text("increase_charge", 5000))
    assert "Diferença do sinal: R$ 50,00" in body
    # a preview only: nothing charged, nothing written before Confirmar
    assert provider.calls == [] and await _adjustments(db) == []
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"


async def test_the_confirmation_card_shows_a_cheaper_difference(db, provider, monkeypatch):  # noqa: F811
    _world_, body = await _card_for(db, monkeypatch, "Retorno")

    assert "R$ 70,00" in body
    assert "perguntamos se você quer essa diferença de volta" in body
    assert await _adjustments(db) == []


def test_the_card_line_never_promises_a_refund():
    for action in ("increase_charge", "increase_free", "decrease_ask", "decrease_keep"):
        text = service_change.confirm_card_line_text(action, 3000)
        assert "R$ 30,00" in text
        assert "devolv" not in text.lower() and "reembols" not in text.lower()
    assert service_change.confirm_card_line_text("blocked", 0) is None
    assert service_change.confirm_card_line_text("no_change", 0) is None
