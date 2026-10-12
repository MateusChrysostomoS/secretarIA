"""The patient's taps on a slot the clinic released with a PAID deposit (TASK-052).

WhatsApp and the Portal reach the same handler (workers/shared/release_offer_actions.py through
`_handle_action_button`). One test per row of the spec's combination table (§6) that a tap can
reach. "Manter" only ASKS the clinic; "Remarcar" carries the deposit until the clinic marks it
refunded; "Cancelar" always tells the clinic at once. A tap on someone else's offer changes nothing.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.config import get_settings
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    PaymentAttentionItem,
    PixDeposit,
)
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
from secretaria.services import email, release_offer as ro
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE, BrainMessageSender
from secretaria.services.flow_router import STEP_DECLINE_REASON
from secretaria.workers import tasks
from secretaria.workers.orchestrator import _send_bot_reply
from secretaria.workers.portal.inbound import _persist_brain_message_inbound
from secretaria.workers.portal.release_card import offer_card_after_start
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import consent, sent, wire
from tests._reminders_v2 import outbound_messages, seed_paid_deposit, seed_world


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):  # noqa: F811
    wire(monkeypatch, db)


@pytest.fixture
def mailbox(monkeypatch) -> list[dict]:
    box: list[dict] = []
    monkeypatch.setattr(
        email, "_send_sync", lambda to, subject, body: box.append({"to": to, "subject": subject})
    )
    monkeypatch.setattr(get_settings(), "SMTP_HOST", "smtp.exemplo", raising=False)
    return box


async def _released(db, *, starts_in=timedelta(days=2), **kwargs):  # noqa: F811
    """A world whose appointment the clinic released with a PAID deposit, offer open."""
    world = await seed_world(db, start_at=datetime.now(UTC) + starts_in, **kwargs)
    await seed_paid_deposit(db, world)
    async with db() as session:
        appointment = await session.get(Appointment, world.appointment.id)
        appointment.status = AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appointment.id)
        )
        tenant = await session.get(type(world.tenant), world.tenant.id)
        tenant.contact_email = "clinica@exemplo.com"
        await ro.open_offer(session, tenant=tenant, appointment=appointment, deposit=deposit)
        await session.commit()
    return world


async def _move(db, **fields):  # noqa: F811
    """Shift the offer (e.g. pretend B, S or E already passed, or the clinic decided)."""
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        for name, value in fields.items():
            setattr(offer, name, value)
        await session.commit()


def _ago(minutes: int = 1) -> datetime:
    return datetime.now(UTC) - timedelta(minutes=minutes)


def _reply(world, conversation_id=None):
    return tasks._ReplyContext(
        conversation_id=conversation_id or world.conversation.id,
        patient_ref=world.patient.external_id or world.patient.wa_id,
        inbound_body="",
    )


async def _tap(world, action, conversation_id=None):
    await tasks._handle_action_button(
        _reply(world, conversation_id), action, str(world.appointment.id)
    )


async def _offer(db, world) -> DepositReleaseOffer:  # noqa: F811
    async with db() as session:
        return await session.scalar(
            select(DepositReleaseOffer).where(
                DepositReleaseOffer.released_appointment_id == world.appointment.id
            )
        )


async def _items(db):  # noqa: F811
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem)))


async def _sends(db) -> dict:  # noqa: F811
    async with db() as session:
        return {
            (row.kind, row.seq): row.outcome
            for row in await session.scalars(select(DepositReleaseEmail))
        }


async def _mark(db, world):  # noqa: F811
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        return await ro.mark_refunded(
            session,
            tenant_id=world.tenant.id,
            offer_id=offer.id,
            note="Estornado no painel do Asaas",
            resolved_by_name="Ana",
        )


def _texts() -> list[str]:
    return [entry[2] for entry in sent() if entry[0] in ("text", "buttons")]


def _last_buttons() -> list[str]:
    kind, _to, _body, buttons = [s for s in sent() if s[0] == "buttons"][-1]
    return [label for _id, label in buttons]


# --- Manter: the clinic decides ------------------------------------------------------------------


async def test_manter_before_s_asks_the_clinic_and_recreates_nothing(db, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relkeep")

    assert _texts()[-1].startswith("Avisamos a clínica que você quer manter o horário de")
    offer = await _offer(db, world)
    assert offer.decision == "keep_requested" and offer.new_appointment_id is None
    assert offer.first_contact_at is not None and offer.keep_deadline_at is not None
    assert await _sends(db) == {("clinic_keep_request", 0): "sent"}  # right after the tap
    assert "manter o horário" in mailbox[0]["subject"]
    async with db() as session:
        live = await session.scalar(
            select(Appointment).where(Appointment.status == AppointmentStatus.SCHEDULED)
        )
    assert live is None


async def test_manter_after_b_is_still_asked(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago())

    await _tap(world, "relkeep")

    offer = await _offer(db, world)
    assert offer.decision == "keep_requested" and offer.refund_status == "due"  # B ran first
    assert _texts()[-1].startswith("Avisamos a clínica")


async def test_manter_after_s_on_whatsapp_answers_with_remarcar_and_cancelar_only(db):  # noqa: F811
    world = await _released(db, starts_in=timedelta(hours=1))
    await _move(db, slot_start_at=_ago(5))

    await _tap(world, "relkeep")

    assert _texts()[-1] == ro.AFTER_START_TEXT
    assert _last_buttons() == ["Remarcar", "Cancelar"]
    assert (await _offer(db, world)).decision == "none"


async def test_manter_twice_tells_the_clinic_once(db, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relkeep")
    await _tap(world, "relkeep")

    assert _texts()[-1] == ro.KEEP_ALREADY_REQUESTED_TEXT and len(mailbox) == 1


async def test_manter_after_the_refund_was_marked_or_after_a_refusal(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago())
    await _tap(world, "relgot")  # settles B (refund due) and answers "not yet"
    assert await _mark(db, world) == "marked"

    await _tap(world, "relkeep")
    assert _texts()[-1] == ro.KEEP_AFTER_REFUND_TEXT and _last_buttons() == ["Remarcar", "Cancelar"]

    refused = await _released(db, phone_number_id="pnid-8", wa_id="5511900000088")
    await _tap(refused, "relkeep")
    async with db() as session:
        row = await session.scalar(
            select(DepositReleaseOffer).where(
                DepositReleaseOffer.released_appointment_id == refused.appointment.id
            )
        )
        await ro.decline_keep(session, offer=row, outcome="refused", resolved_by="Ana")
        await session.commit()
    await _tap(refused, "relkeep")
    assert _texts()[-1] == ro.KEEP_DECLINED_TAP_TEXT


async def test_manter_after_cancelar_is_refused(db, mailbox):  # noqa: F811
    world = await _released(db)

    await _tap(world, "relcancel")
    await _tap(world, "relkeep")

    assert _texts()[-1] == ro.TAP_KEEP_AFTER_CANCEL_TEXT
    assert (await _offer(db, world)).decision == "cancelled"


# --- Remarcar ------------------------------------------------------------------------------------


@pytest.mark.parametrize("moment", ["before_b", "after_s", "after_b", "after_cancel"])
async def test_remarcar_carries_the_deposit_whenever_the_refund_is_not_marked(  # noqa: F811
    db, mailbox, moment  # noqa: F811
):
    starts_in = timedelta(hours=1) if moment == "after_s" else timedelta(days=2)
    world = await _released(db, starts_in=starts_in)
    if moment == "after_s":
        await _move(db, slot_start_at=_ago(5))
    if moment == "after_b":
        await _move(db, decide_by_at=_ago())
    if moment == "after_cancel":
        await _tap(world, "relcancel")

    await _tap(world, "relresched")

    kind, _to, body, _label, rows, _section = sent()[-1]
    assert kind == "list" and body.startswith(ro.RESCHEDULE_PREFIX)
    assert rows[0][0].startswith("day|")
    assert (await _offer(db, world)).decision == "rescheduling"
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG


async def test_remarcar_after_the_refund_was_marked_charges_again(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago())
    await _tap(world, "relgot")
    await _mark(db, world)

    await _tap(world, "relresched")

    assert sent()[-1][2].startswith(ro.RESCHEDULE_NEW_CHARGE_PREFIX)


# --- Cancelar ------------------------------------------------------------------------------------


@pytest.mark.parametrize("moment", ["before_s", "after_s", "after_b"])
async def test_cancelar_always_tells_the_clinic_now_and_asks_why(db, mailbox, moment):  # noqa: F811
    starts_in = timedelta(hours=1) if moment == "after_s" else timedelta(days=2)
    world = await _released(db, starts_in=starts_in)
    if moment == "after_s":
        await _move(db, slot_start_at=_ago(5))
    if moment == "after_b":
        await _move(db, decide_by_at=_ago())

    await _tap(world, "relcancel")

    assert any("sua consulta foi cancelada" in text and "R$ 100,00" in text for text in _texts())
    offer = await _offer(db, world)
    assert (offer.decision, offer.refund_status) == ("cancelled", "due")
    assert (await _sends(db))[("clinic_refund_cancel", 0)] == "sent"  # right after the tap
    assert len(await _items(db)) == 1
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    assert conversation.flow_step == STEP_DECLINE_REASON


async def test_cancelar_after_marked_refunded_sends_nothing_new(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago())
    await _tap(world, "relgot")
    await _mark(db, world)

    await _tap(world, "relcancel")

    assert ro.CANCEL_AFTER_MARKED_TEXT in _texts()
    assert ("clinic_refund_cancel", 0) not in await _sends(db)


async def test_cancelar_twice_or_after_a_kept_slot(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _tap(world, "relcancel")
    await _tap(world, "relcancel")
    assert _texts()[-1] == ro.TAP_REFUND_ALREADY_TEXT and len(mailbox) == 1

    kept = await _released(db, phone_number_id="pnid-7", wa_id="5511900000077")
    async with db() as session:
        row = await session.scalar(
            select(DepositReleaseOffer).where(
                DepositReleaseOffer.released_appointment_id == kept.appointment.id
            )
        )
        row.decision = "kept"
        await session.commit()
    await _tap(kept, "relcancel")
    assert _texts()[-1] == ro.TAP_CANCEL_AFTER_KEEP_TEXT


# --- T + 24 h ------------------------------------------------------------------------------------


@pytest.mark.parametrize("action", ["relkeep", "relresched", "relcancel"])
async def test_any_tap_after_e_is_answered_and_reopens_nothing(db, mailbox, action):  # noqa: F811
    world = await _released(db)
    await _move(db, decide_by_at=_ago(60 * 18), expires_at=_ago())

    await _tap(world, action)

    assert _texts()[-1] == ro.OFFER_CLOSED_NO_ANSWER_TEXT
    offer = await _offer(db, world)
    assert (offer.decision, offer.refund_status) == ("no_answer_cancelled", "due")
    assert len(await _items(db)) == 1


# --- Recebido / Não recebido --------------------------------------------------------------------


async def test_recebido_and_nao_recebido_follow_the_clinics_marking(db, mailbox):  # noqa: F811
    world = await _released(db)
    await _tap(world, "relgot")
    assert _texts()[-1] == ro.CHECK_TOO_EARLY_TEXT

    await _tap(world, "relcancel")
    assert await _mark(db, world) == "marked"

    await _tap(world, "relmiss")
    assert _texts()[-1] == ro.NOT_RECEIVED_TEXT
    await _tap(world, "relmiss")
    assert _texts()[-1] == ro.NOT_RECEIVED_RECENT_TEXT
    sends = await _sends(db)
    assert sends[("clinic_refund_reminder", 1)] == "sent"
    assert ("clinic_refund_reminder", 2) not in sends

    await _tap(world, "relgot")
    assert _texts()[-1] == ro.RECEIVED_TEXT
    await _tap(world, "relgot")
    assert _texts()[-1] == ro.CHECK_CLOSED_TEXT


# --- isolation and the Portal --------------------------------------------------------------------


async def test_another_patients_tap_is_an_unknown_appointment_and_changes_nothing(db):  # noqa: F811
    world = await _released(db)
    async with db() as session:
        intruder = Patient(id=uuid4(), tenant_id=world.tenant.id, wa_id="5511900000009")
        session.add(intruder)
        await session.flush()
        theirs = Conversation(id=uuid4(), tenant_id=world.tenant.id, patient_id=intruder.id)
        session.add(theirs)
        await session.commit()

    await _tap(world, "relcancel", conversation_id=theirs.id)

    assert _texts()[-1] == ro.NOT_FOUND_TEXT
    offer = await _offer(db, world)
    assert offer.decision == "none" and offer.first_contact_at is None


async def test_another_clinics_offer_is_never_reached(db):  # noqa: F811
    world = await _released(db)
    other = await seed_world(db, phone_number_id="pnid-2", wa_id="5511900000002")

    await tasks._handle_action_button(
        _reply(other, conversation_id=other.conversation.id), "relcancel", str(world.appointment.id)
    )

    assert (await _offer(db, world)).decision == "none" and await _items(db) == []


async def test_a_portal_tap_on_the_recorded_card_is_the_same_action(db, mailbox):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)
    async with db() as session:
        sender = BrainMessageSender(conversation_id=world.conversation.id, session=session)
        await sender.send_buttons(
            to=world.patient.external_id,
            body=ro.OFFER_HEAD,
            buttons=ro.offer_buttons(world.appointment.id),
        )
        await session.commit()

    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text="Manter horário",
        interactive_reply_id=f"relkeep|{world.appointment.id}",
    )
    await _send_bot_reply(reply)

    assert reply.action_button == ("relkeep", str(world.appointment.id))
    assert (await _offer(db, world)).decision == "keep_requested"
    bodies = [m.body for m in await outbound_messages(db, world.conversation.id)]
    assert any(body.startswith("Avisamos a clínica") for body in bodies)


async def test_a_portal_id_never_shown_on_a_card_is_not_an_action(db):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)
    await consent(db, world)

    reply = await _persist_brain_message_inbound(
        tenant_id=world.tenant.id,
        external_id=world.patient.external_id,
        text="Cancelar",
        interactive_reply_id=f"relcancel|{world.appointment.id}",
    )

    assert reply.action_button is None
    assert (await _offer(db, world)).decision == "none"


async def test_opening_the_portal_after_s_shows_remarcar_and_cancelar_once(db):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE, starts_in=timedelta(hours=1))
    await _move(db, slot_start_at=_ago(5))

    assert await offer_card_after_start(world.tenant.id, world.patient.external_id)
    assert not await offer_card_after_start(world.tenant.id, world.patient.external_id)

    [card] = await outbound_messages(db, world.conversation.id)
    assert card.interactive["body"] == ro.AFTER_START_TEXT  # `body` is the history line + options
    assert [o["title"] for o in card.interactive["options"]] == ["Remarcar", "Cancelar"]


async def test_opening_the_portal_before_s_sends_nothing_extra(db):  # noqa: F811
    world = await _released(db, channel=CHANNEL_BRAIN_MESSAGE)

    assert not await offer_card_after_start(world.tenant.id, world.patient.external_id)
    assert await outbound_messages(db, world.conversation.id) == []
