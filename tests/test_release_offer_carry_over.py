"""The booking that ends a "Remarcar" carries the released slot's PAID deposit (TASK-052).

The confirmation asks the payment provider first (owner 2026-10-12): paid and not refunded ->
the deposit moves to the new appointment in the booking's own transaction, without a new charge
and without touching `reschedule_count`; the `before` gate asks for no Pix. Refunded / unknown ->
the booking stands on its own (new charge) and says so. The clinic already marked the refund ->
new charge, no provider call.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.ai.formatter import TextBubble
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    FlowState,
    PaymentAttentionItem,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.models.deposit_release_offer import DepositReleaseEmail, DepositReleaseOffer
from secretaria.services import booking_hold as holds, release_carry_check, release_offer
from secretaria.services.channel_sender import CHANNEL_BRAIN_MESSAGE
from secretaria.services.flow_router import FlowRouterResult
from secretaria.services.payments import registry
from secretaria.services.payments.provider import ChargeState, PaymentAccount, PaymentStatus
from secretaria.workers import tasks
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_r3 import sent, wire
from tests._reminders_v2 import WA_ID, seed_paid_deposit, seed_world
from tests.before_support import entitlement_summary
from tests.payments_support import FakeProvider

CONFIRMED = "Pronto! Seu agendamento está confirmado."
PAID = ChargeState(status=PaymentStatus.PAID, paid_amount_cents=10000)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(state=PAID)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db, provider):  # noqa: F811
    wire(monkeypatch, db)
    monkeypatch.setattr(holds, "async_session_factory", db)
    monkeypatch.setattr(release_carry_check, "async_session_factory", db)

    async def _account(session, tenant_id, provider_name):  # a placeholder, never a real key
        return PaymentAccount(tenant_id=tenant_id, provider=provider_name, api_key="placeholder")

    monkeypatch.setattr(release_carry_check, "_account", _account)


def _future(days: int) -> datetime:
    return datetime.now(UTC) + timedelta(days=days)


async def _rescheduling(db, *, tap: bool = True):  # noqa: F811
    world = await seed_world(db, start_at=_future(2), google_event_id="evt-released")
    await seed_paid_deposit(db, world)
    async with db() as session:
        released = await session.get(Appointment, world.appointment.id)
        released.status = AppointmentStatus.CANCELLED
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == released.id)
        )
        tenant = await session.get(type(world.tenant), world.tenant.id)
        offer = await release_offer.open_offer(
            session, tenant=tenant, appointment=released, deposit=deposit
        )
        if tap:
            await release_offer.choose_reschedule(
                session, offer=offer, conversation_id=world.conversation.id
            )
        await session.commit()
    return world


def _booking(start: datetime) -> FlowRouterResult:
    return FlowRouterResult(
        action="reply",
        bubbles=[TextBubble(body=CONFIRMED)],
        flow_state=FlowState.IDLE,
        appointment={
            "google_event_id": "evt-new",
            "appointment_type": "Consulta",
            "start_at": start,
            "end_at": start + timedelta(minutes=30),
        },
    )


async def _confirm(world, result) -> None:
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="Confirmar"
    )
    await tasks._apply_flow_result(reply, result, WA_ID, tenant=world.tenant, waba_token="t")


async def _state(db):  # noqa: F811
    async with db() as session:
        new = await session.scalar(
            select(Appointment).where(Appointment.google_event_id == "evt-new")
        )
        deposit = await session.scalar(select(PixDeposit))
        offer = await session.scalar(select(DepositReleaseOffer))
        items = list(await session.scalars(select(PaymentAttentionItem)))
    return new, deposit, offer, items


async def test_the_new_booking_carries_the_paid_deposit_and_says_so(db, provider):  # noqa: F811
    world = await _rescheduling(db)

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, items = await _state(db)
    assert new is not None and new.status == AppointmentStatus.SCHEDULED
    assert deposit.appointment_id == new.id and deposit.status is PixDepositStatus.PAID
    assert deposit.reschedule_count == 0  # the clinic freed the slot: not the patient's reschedule
    assert (offer.decision, offer.new_appointment_id) == ("rebooked", new.id)
    assert offer.deposit_reused_at is not None and provider.called("get_charge") == 1
    assert items == []
    [text] = [item[2] for item in sent() if item[0] == "text"]
    assert text == f"{CONFIRMED}\n\n{release_offer.CARRIED_OVER_NOTE}"


async def test_when_asaas_cannot_confirm_the_booking_says_a_new_deposit_is_due(db, provider):  # noqa: F811
    provider.get_error = OSError("asaas down")
    world = await _rescheduling(db)

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, items = await _state(db)
    assert new is not None and deposit.appointment_id == world.appointment.id  # not carried
    assert (offer.decision, offer.carry_check, offer.refund_status) == (
        "rebooked",
        "unknown",
        "due",
    )
    (item,) = items
    assert item.status == "open"  # the clinic owes the old deposit back
    [text] = [item[2] for item in sent() if item[0] == "text"]
    assert text == f"{CONFIRMED}\n\n{release_offer.CARRY_REFUSED_NOTE}"


async def test_without_the_remarcar_tap_nothing_is_carried(db, provider):  # noqa: F811
    world = await _rescheduling(db, tap=False)

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, _items = await _state(db)
    assert new is not None and deposit.appointment_id == world.appointment.id
    assert offer.decision == "none" and provider.called("get_charge") == 0
    assert [item[2] for item in sent() if item[0] == "text"] == [CONFIRMED]


async def test_a_booking_after_b_still_carries_and_undoes_the_refund(db):  # noqa: F811
    world = await _rescheduling(db)
    async with db() as session:  # B passed meanwhile: the refund fell due, the clinic was told
        await release_offer.expire_offers(session, now=datetime.now(UTC) + timedelta(hours=7))

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, items = await _state(db)
    assert deposit.appointment_id == new.id and deposit.status is PixDepositStatus.PAID
    assert (offer.decision, offer.refund_status) == ("rebooked", "not_due")
    (item,) = items
    assert (item.status, item.resolution_action) == ("resolved", "rebooked")
    async with db() as session:
        pending = [
            row.kind
            for row in await session.scalars(
                select(DepositReleaseEmail).where(DepositReleaseEmail.claimed_at.is_(None))
            )
        ]
    assert pending == ["clinic_rebooked_deposit"]  # "REMARCADA levando o sinal", nothing else


async def test_a_booking_after_the_clinic_marked_refunded_charges_again(db, provider):  # noqa: F811
    world = await _rescheduling(db)
    async with db() as session:
        await release_offer.expire_offers(session, now=datetime.now(UTC) + timedelta(hours=7))
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
        assert (
            await release_offer.mark_refunded(
                session,
                tenant_id=world.tenant.id,
                offer_id=offer.id,
                note="Estornado no painel do Asaas",
                resolved_by_name="Ana",
            )
            == "marked"
        )

    await _confirm(world, _booking(_future(6)))

    new, deposit, offer, _items = await _state(db)
    assert new is not None and deposit.appointment_id == world.appointment.id
    assert deposit.status is PixDepositStatus.CANCELLED_REFUNDED
    assert offer.refund_status == "marked" and provider.called("get_charge") == 0
    assert [item[2] for item in sent() if item[0] == "text"] == [CONFIRMED]


async def _on_before(db, world, monkeypatch):  # noqa: F811
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant.id)
        tenant.pix_deposit_enabled = True
        tenant.deposit_timing = "before"
        tenant.appointment_types = [
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"}
        ]
        await session.commit()

    async def _entitled(tenant_id, redis):
        return entitlement_summary()

    gate = holds.BookingGate(
        tenant_id=world.tenant.id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        external_id="",
        armed=False,
    )
    return await gate.decide(
        start_at=_future(6),
        end_at=_future(6) + timedelta(minutes=30),
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
    )


async def test_the_before_gate_asks_no_new_pix_while_the_deposit_is_carried(
    db,  # noqa: F811
    monkeypatch,
    provider,  # noqa: F811
):
    world = await _rescheduling(db)

    decision = await _on_before(db, world, monkeypatch)

    assert decision.outcome == "commit"
    assert provider.called("get_charge") == 1 and provider.called("create_pix_charge") == 0


async def test_the_before_gate_charges_a_new_pix_when_asaas_says_refunded(
    db,  # noqa: F811
    monkeypatch,
    provider,  # noqa: F811
):
    provider.state = ChargeState(status=PaymentStatus.REFUNDED)
    world = await _rescheduling(db)

    decision = await _on_before(db, world, monkeypatch)

    assert decision.outcome != "commit"  # the normal `before` path: a NEW Pix is asked for
    async with db() as session:
        offer = await session.scalar(select(DepositReleaseOffer))
    assert (offer.carry_check, offer.refund_status) == ("refunded", "due")


async def test_the_portal_booking_born_at_the_code_carries_it_too(db):  # noqa: F811
    world = await seed_world(
        db, start_at=_future(2), channel=CHANNEL_BRAIN_MESSAGE, google_event_id="evt-released"
    )
    await seed_paid_deposit(db, world)
    start = _future(6)
    async with db() as session:
        released = await session.get(Appointment, world.appointment.id)
        released.status = AppointmentStatus.CANCELLED
        deposit = await session.scalar(select(PixDeposit))
        tenant = await session.get(type(world.tenant), world.tenant.id)
        offer = await release_offer.open_offer(
            session, tenant=tenant, appointment=released, deposit=deposit
        )
        await release_offer.choose_reschedule(
            session, offer=offer, conversation_id=world.conversation.id
        )
        session.add(
            BookingHold(
                tenant_id=world.tenant.id,
                conversation_id=world.conversation.id,
                patient_id=world.patient.id,
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=30),
                expires_at=datetime.now(UTC) + timedelta(minutes=10),
            )
        )
        await session.commit()
    reply = tasks._ReplyContext(
        channel=CHANNEL_BRAIN_MESSAGE,
        conversation_id=world.conversation.id,
        patient_ref=world.patient.external_id,
        inbound_body="123456",
    )

    extra = await tasks._promote_booking_hold(
        reply, tenant=world.tenant, waba_token=None, redis=None
    )

    assert extra.endswith(release_offer.CARRIED_OVER_NOTE)
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
        born = await session.scalar(
            select(Appointment).where(Appointment.id == deposit.appointment_id)
        )
    assert born.id != world.appointment.id and born.start_at is not None
