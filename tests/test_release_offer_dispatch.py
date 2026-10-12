"""services/release_offer_dispatch.py - what a released PAID slot scheduled, sent once (TASK-052).

Each row is claimed (and committed) BEFORE its send, re-checked at send time, retried when nothing
left and given up after 5 tries. Clinic notices carry the amount, the slot, the console link (and
the Asaas steps when a refund is due) - never the patient's name, phone or e-mail. A WhatsApp
patient never gets an e-mail; a Portal patient gets the card and, for the refund check, an e-mail.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.models import (
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PaymentAttentionItem,
)
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import email, release_offer as ro, release_offer_dispatch
from secretaria.services.payments import attention_view
from tests._reminders_v2 import FakeWhatsAppClient, fake_waba_token
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import RELEASED_START, T, emails, make_released, new_appointment

B = T + timedelta(hours=6)
E = T + timedelta(hours=24)
CLINIC = "clinica@exemplo.com"
PATIENT = "maria@exemplo.com"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture
def mailbox(monkeypatch) -> list[dict]:
    sent: list[dict] = []

    def _send(to_email, subject, body):
        sent.append({"to": to_email, "subject": subject, "body": body})

    monkeypatch.setattr(email, "_send_sync", _send)
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    monkeypatch.setattr(
        get_settings(), "BRAIN_MESSAGE_PORTAL_URL", "https://portal.exemplo", raising=False
    )
    return sent


@pytest.fixture(autouse=True)
def whatsapp(monkeypatch):
    FakeWhatsAppClient.reset()
    monkeypatch.setattr(release_offer_dispatch, "WhatsAppClient", FakeWhatsAppClient)
    monkeypatch.setattr(release_offer_dispatch, "get_waba_token", fake_waba_token)

    async def _usage(**kwargs):
        return True

    monkeypatch.setattr(release_offer_dispatch, "emit_usage_event", _usage)


async def _opened(db, *, channel="whatsapp", patient_email=None, paid=True, **kw):
    async with db() as session:
        released = await make_released(session, channel=channel, **kw)
        if patient_email:
            patient = await session.get(Patient, released.patient.id)
            patient.email = patient_email
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
            paid_notice_allowed=paid,
        )
        await session.commit()
    return released, offer


async def _call(db, fn, offer_id, at, **kw):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        result = await fn(session, offer=row, now=at, **kw)
        await session.commit()
    return result


async def _mark(db, released, offer_id, at=T + timedelta(hours=3)):
    async with db() as session:
        await ro.mark_refunded(
            session,
            tenant_id=released.tenant_id,
            offer_id=offer_id,
            note="Estornado no painel do Asaas",
            resolved_by_name="Ana",
            now=at,
        )


async def _dispatch(db, now, **kw):
    async with db() as session:
        return await release_offer_dispatch.dispatch_due(session, now=now, **kw)


async def _sweep(db, at):
    async with db() as session:
        await ro.expire_offers(session, now=at)


async def _outcomes(db, offer_id) -> dict:
    async with db() as session:
        return {(e.kind, e.seq): e.outcome for e in await emails(session, offer_id)}


def _clinic(mailbox) -> list[dict]:
    return [m for m in mailbox if m["to"] == CLINIC]


async def _inbound(db, released):
    async with db() as session:
        session.add(
            Message(
                conversation_id=released.conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                body="oi",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
        await session.commit()


async def test_cancel_tells_the_clinic_now_with_the_asaas_steps_and_no_patient_data(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))

    assert (await _dispatch(db, T + timedelta(hours=1))).sent == 1

    (mail,) = mailbox
    assert mail["to"] == CLINIC and "Devolver sinal" in mail["subject"]
    body = mail["body"]
    assert "R$ 60,00" in body and "08/03 às 10:00" in body and "paga em 01/03" in body
    assert f"https://portal.exemplo/chat/?conversationId={released.conversation.id}" in body
    for step in ("Cobranças", "estornar o valor total", "Não é preciso digitar chave Pix",
                 "saldo insuficiente", "comprovante"):  # fmt: skip
        assert step in body
    assert "Maria" not in body and released.patient.wa_id not in body


async def test_silence_tells_the_clinic_at_b_and_says_overdue_at_e(db, mailbox):
    _released, offer = await _opened(db)
    await _sweep(db, B)
    await _dispatch(db, B)
    assert "não respondeu até 20:00 de 05/03" in mailbox[-1]["body"]

    await _sweep(db, E)
    await _dispatch(db, E)

    assert "prazo vencido" in mailbox[-1]["subject"].lower()
    assert "Devolva agora" in mailbox[-1]["body"]


async def test_manter_tells_the_clinic_with_the_link_and_pauses_the_refund_notices(db, mailbox):
    released, offer = await _opened(db)
    await _sweep(db, B)  # refund due; B's notice and E's reminder scheduled
    await _call(db, ro.request_keep, offer.id, B - timedelta(minutes=30))  # tapped just before

    await _dispatch(db, E + timedelta(minutes=1))

    (mail,) = _clinic(mailbox)
    assert "manter o horário" in mail["subject"]
    assert "08/03 às 10:00" in mail["body"] and "conversationId=" in mail["body"]
    outcomes = await _outcomes(db, offer.id)
    assert outcomes[("clinic_refund_noanswer", 0)] == "skipped"
    assert outcomes[("clinic_refund_day", 0)] == "skipped"


async def test_a_refusal_sends_new_refund_steps_and_the_patient_gets_remarcar_or_cancelar(
    db, mailbox
):
    released, offer = await _opened(db)
    await _inbound(db, released)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, offer.id, T + timedelta(hours=2), outcome="refused")

    await _dispatch(db, T + timedelta(hours=2))

    # The request notice is dropped (the clinic already decided); the refusal carries the steps.
    (declined,) = _clinic(mailbox)
    assert "não foi mantido" in declined["subject"] and "estornar o valor total" in declined["body"]
    (card,) = FakeWhatsAppClient.all_sent()
    assert card[0] == "buttons" and card[2].startswith("A clínica não conseguiu manter")
    assert [label for _id, label in card[3]] == ["Remarcar", "Cancelar"]


async def test_an_accepted_keep_tells_the_patient_on_whatsapp_never_by_email(db, mailbox):
    released, offer = await _opened(db, patient_email=PATIENT)
    await _inbound(db, released)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        again = await new_appointment(session, released, start=RELEASED_START)
        await ro.claim_keep(
            session, offer=row, new_appointment=again, resolved_by="Ana", now=T + timedelta(hours=2)
        )
        await session.commit()

    await _dispatch(db, T + timedelta(hours=2))

    texts = [s for s in FakeWhatsAppClient.all_sent() if s[0] == "text"]
    assert texts and texts[0][2].startswith("A clínica confirmou: seu horário de 08/03 às 10:00")
    assert [m for m in mailbox if m["to"] == PATIENT] == []


async def test_a_confirmed_remarcar_sends_the_rebooked_email_and_cancels_refund_notices(
    db, mailbox
):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))  # cancel notice due now
    await _call(db, ro.choose_reschedule, offer.id, T + timedelta(hours=2),
                conversation_id=released.conversation.id)  # fmt: skip
    async with db() as session:
        await ro.record_carry_check(  # the provider said "paid, no refund" at the confirmation
            session, tenant_id=released.tenant_id, offer_id=offer.id, outcome="reusable",
            now=T + timedelta(hours=3),
        )  # fmt: skip
        fresh = await new_appointment(session, released, start=RELEASED_START + timedelta(days=4))
        await ro.claim_carry_over(
            session,
            tenant_id=released.tenant_id,
            conversation_id=released.conversation.id,
            new_appointment=fresh,
            now=T + timedelta(hours=3),
        )
        await session.commit()

    await _dispatch(db, E + timedelta(hours=1))

    (mail,) = _clinic(mailbox)
    assert "REMARCADA levando o sinal" in mail["subject"]
    assert "não há reembolso a fazer" in mail["body"]
    assert "08/03 às 10:00" in mail["body"] and "12/03 às 10:00" in mail["body"]
    outcomes = await _outcomes(db, offer.id)
    assert outcomes[("clinic_refund_cancel", 0)] == "skipped"
    assert outcomes[("clinic_refund_day", 0)] == "skipped"


async def test_the_day_reminder_skips_only_when_marked_and_received(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _dispatch(db, T + timedelta(hours=1))  # the cancel notice
    await _mark(db, released, offer.id)
    await _call(db, ro.record_received, offer.id, T + timedelta(hours=5))

    await _dispatch(db, E)

    assert len(_clinic(mailbox)) == 1
    assert (await _outcomes(db, offer.id))[("clinic_refund_day", 0)] == "skipped"


async def test_the_day_reminder_still_goes_when_marked_but_unanswered(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _mark(db, released, offer.id)

    await _dispatch(db, E)

    assert (await _outcomes(db, offer.id))[("clinic_refund_day", 0)] == "sent"


async def test_the_day_reminder_still_goes_after_not_received(db, mailbox):
    released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _mark(db, released, offer.id)
    await _call(db, ro.request_reminder, offer.id, T + timedelta(hours=5))

    await _dispatch(db, E)

    outcomes = await _outcomes(db, offer.id)
    assert outcomes[("clinic_refund_reminder", 1)] == "sent"
    assert outcomes[("clinic_refund_day", 0)] == "sent"


async def test_a_clinic_decision_other_than_refund_stops_every_notice(db, mailbox):
    released, offer = await _opened(db)
    await _sweep(db, B)
    async with db() as session:
        item = await session.scalar(select(PaymentAttentionItem))
    async with db() as session:
        await attention_view.resolve_item(
            session,
            released.tenant_id,
            item.id,
            action="kept_as_credit",
            note="Crédito para a próxima consulta",
            resolved_by_name="Ana",
        )

    assert (await _dispatch(db, E)).sent == 0 and mailbox == []


async def test_each_scheduled_email_goes_out_once_even_dispatched_twice(db, mailbox):
    _released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))

    await _dispatch(db, T + timedelta(hours=1))
    await _dispatch(db, T + timedelta(hours=1, minutes=5))
    assert len(mailbox) == 1

    async with db() as session:  # another worker already claimed the E reminder
        for row in await emails(session, offer.id):
            if row.kind == "clinic_refund_day":
                row.claimed_at = E
                row.outcome = "sending"
        await session.commit()
    await _dispatch(db, E)
    assert len(mailbox) == 1


async def test_a_send_that_did_not_leave_is_retried_then_given_up(db, mailbox, monkeypatch):
    _released, offer = await _opened(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))

    def _refuse(to_email, subject, body):
        raise OSError("smtp down")

    monkeypatch.setattr(email, "_send_sync", _refuse)
    reports = [await _dispatch(db, T + timedelta(hours=1, minutes=m)) for m in range(5)]

    assert [r.retried for r in reports] == [1, 1, 1, 1, 0] and reports[-1].given_up == 1
    async with db() as session:
        (row,) = [e for e in await emails(session, offer.id) if e.kind == "clinic_refund_cancel"]
    assert (row.outcome, row.attempts) == ("given_up", 5)


async def test_a_clinic_without_contact_email_is_skipped_not_retried(db, mailbox):
    _released, offer = await _opened(db, contact_email=None)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))

    first = await _dispatch(db, T + timedelta(hours=1))
    again = await _dispatch(db, T + timedelta(hours=1, minutes=5))

    assert (first.no_contact, again.no_contact, mailbox) == (1, 0, [])


async def test_marking_refunded_asks_a_portal_patient_by_card_and_email_link(db, mailbox):
    released, offer = await _opened(db, channel="brain_message", patient_email=PATIENT)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _mark(db, released, offer.id)

    await _dispatch(db, T + timedelta(hours=3), offer_id=offer.id)

    (patient_mail,) = [m for m in mailbox if m["to"] == PATIENT]
    assert "R$ 60,00" in patient_mail["body"] and "conta de onde saiu o Pix" in patient_mail["body"]
    assert "banco" not in patient_mail["body"].lower()
    assert f"https://portal.exemplo/clinicas/?convite={released.tenant_id}" in patient_mail["body"]
    async with db() as session:
        card = await session.scalar(
            select(Message)
            .where(Message.conversation_id == released.conversation.id)
            .order_by(Message.created_at.desc())
            .limit(1)
        )
    assert [o["id"] for o in card.interactive["options"]] == [
        f"relgot|{released.appointment.id}",
        f"relmiss|{released.appointment.id}",
    ]


async def test_outside_the_window_whatsapp_uses_the_pending_templates_or_nothing(db, mailbox):
    released, offer = await _opened(db, patient_email=PATIENT)  # paid permission given
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    await _mark(db, released, offer.id)
    await _dispatch(db, T + timedelta(hours=3), offer_id=offer.id)
    (template,) = FakeWhatsAppClient.all_sent()
    assert template[0] == "template" and template[2] == get_settings().REFUND_CHECK_TEMPLATE_NAME
    assert "\n" not in template[4][0]
    assert [m for m in mailbox if m["to"] == PATIENT] == []  # WhatsApp: never an e-mail

    FakeWhatsAppClient.reset()
    other, declined = await _opened(db)
    await _call(db, ro.request_keep, declined.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, declined.id, T + timedelta(hours=2), outcome="refused")
    await _dispatch(db, T + timedelta(hours=2), offer_id=declined.id)
    (template,) = FakeWhatsAppClient.all_sent()
    assert template[2] == get_settings().RELEASE_OFFER_TEMPLATE_NAME  # the 3-button one, reused

    FakeWhatsAppClient.reset()
    _x, silent = await _opened(db, paid=False)
    await _call(db, ro.request_keep, silent.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, silent.id, T + timedelta(hours=2), outcome="refused")
    report = await _dispatch(db, T + timedelta(hours=2), offer_id=silent.id)
    assert FakeWhatsAppClient.all_sent() == [] and report.no_contact == 1


def test_still_relevant_is_the_spec_table():
    offer = DepositReleaseOffer(deposit_paid=True, decision="keep_requested", refund_status="due")
    assert not release_offer_dispatch.still_relevant("clinic_refund_noanswer", offer)  # paused
    assert not release_offer_dispatch.still_relevant("clinic_refund_day", offer)
    assert release_offer_dispatch.still_relevant("clinic_keep_request", offer)
    offer.decision, offer.refund_status = "no_answer_cancelled", "marked"
    assert release_offer_dispatch.still_relevant("clinic_refund_day", offer)
    offer.refund_status = "received"
    assert not release_offer_dispatch.still_relevant("clinic_refund_day", offer)
    offer.decision, offer.refund_status = "rebooked", "not_due"
    assert release_offer_dispatch.still_relevant("clinic_rebooked_deposit", offer)
    assert not release_offer_dispatch.still_relevant("clinic_refund_cancel", offer)
