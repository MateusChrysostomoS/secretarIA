"""The promotion of a paid reservation (services/payments/fulfillment.py, booking_promotion.py,
deposit_wait.py; spec sections 4.2 item 2, 8.5, 9, 10, 10A).

Every scenario starts from a REAL `before` charge (`begin_payment`: hold + deposit + provider
call) and marks it paid, then runs `fulfill_paid_charge`. What is pinned: the consultation is
created exactly once however often the step runs; a slot that is gone (late payment, a doctor's
manual Google event, another reservation) is never double-booked and never refunded - it becomes
an attention item and the patient is told the clinic will get in touch; a promotion that keeps
failing gives up into an item instead of looping forever.
"""

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from secretaria.models import (
    Appointment,
    BookingHold,
    Conversation,
    FlowState,
    PaymentAttentionItem,
    PixDeposit,
)
from secretaria.models.pix_deposit import (
    FULFILLMENT_CONFIRMED,
    FULFILLMENT_NEEDS_ATTENTION,
    FULFILLMENT_PROMOTING,
    PixDepositStatus,
)
from secretaria.services import booking_hold as holds, booking_promotion, deposit_wait
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.payments import booking_charge, fulfillment, jobs, notify, registry
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from tests.before_support import SLOT_END, SLOT_START, make_world, new_charge
from tests.payments_support import FakeProvider, make_sessionmaker, recording_enqueue

QUOTE = DepositQuote(
    "required", "required", amount_cents=6000, provider="asaas", ttl_minutes=30,
    service_label="Consulta",
)
NOW = datetime.now(UTC)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (holds, booking_charge, notify):
        monkeypatch.setattr(module, "async_session_factory", maker)
    yield maker
    await engine.dispose()


class FakeCalendar:
    def __init__(self) -> None:
        self.events: list[dict] = []  # what Google already holds on the slot
        self.created: list[tuple] = []
        self.unavailable = False
        self.known_ids: set[str] = set()

    async def check_availability(self, start, end, max_results=50):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        return list(self.events)

    async def create_event(self, start, end, summary, description="", *, event_id=None):
        if self.unavailable:
            raise CalendarUnavailableError("down")
        self.created.append((start, end, summary, event_id))
        self.known_ids.add(event_id or "")
        return {"id": event_id or "evt", "htmlLink": "https://calendar.google.com/x"}


class Env:
    def __init__(self) -> None:
        self.calendar = FakeCalendar()
        self.provider = FakeProvider()
        self.provider.created_charge = new_charge()
        self.provider.state = ChargeState(PaymentStatus.PENDING)
        self.notices: list[tuple[str, str]] = []  # (patient channel, text)
        self.notice_error: notify.DeliveryError | None = None
        self.hooks: list[tuple] = []


@pytest.fixture
def env(monkeypatch) -> Env:
    e = Env()

    async def calendar_for_booking(session, tenant, professional_id):
        return None if e.calendar is None else e.calendar

    async def deliver(session, tenant, patient, messages, **kwargs):
        if e.notice_error is not None:
            raise e.notice_error
        for message in messages:
            e.notices.append((patient.channel, message.body))

    async def enqueue_hooks(redis, tenant_id, appointment_id, source):
        e.hooks.append((str(appointment_id), source))

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", calendar_for_booking)
    monkeypatch.setattr(notify, "deliver", deliver)
    monkeypatch.setattr("secretaria.plugins.post_booking.enqueue_post_booking_hooks", enqueue_hooks)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", e.provider)
    return e


async def _paid_charge(db, world, *, late=False, paid_ago=timedelta(seconds=5)):
    """A real `before` charge, then marked paid the way the event core does it."""
    started = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
    )
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.AWAITING_DEPOSIT
        conversation.flow_selected_type = "Consulta"
        values = {
            "status": PixDepositStatus.PAID,
            "paid_at": datetime.now(UTC) - paid_ago,
            "paid_amount_cents": 6000,
            "fulfillment_status": FULFILLMENT_PROMOTING,
        }
        if late:
            values["resolved_at"] = datetime.now(UTC) - timedelta(minutes=5)
        await session.execute(
            update(PixDeposit).where(PixDeposit.id == started.deposit_id).values(**values)
        )
        if late:  # the expiry already released the hold
            await booking_charge.release_hold_of_charge(
                session, await session.get(PixDeposit, started.deposit_id)
            )
        await session.commit()
    return started.deposit_id


async def _fulfill(db, deposit_id):
    async with db() as session:
        return await fulfillment.fulfill_paid_charge(session, deposit_id=deposit_id, redis=object())


async def _state(db, deposit_id):
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
        appointments = list(await session.scalars(select(Appointment)))
        items = list(await session.scalars(select(PaymentAttentionItem)))
        holds_left = list(await session.scalars(select(BookingHold)))
        conversation = await session.get(Conversation, deposit.conversation_id) if (
            deposit.conversation_id
        ) else None
    return deposit, appointments, items, holds_left, conversation


async def test_a_paid_charge_becomes_one_appointment_one_event_one_confirmation(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)

    assert await _fulfill(db, deposit_id) == "promoted"

    deposit, appointments, items, holds_left, conversation = await _state(db, deposit_id)
    (appointment,) = appointments
    assert deposit.appointment_id == appointment.id
    assert deposit.fulfillment_status == FULFILLMENT_CONFIRMED
    assert deposit.calendar_event_id == deposit_id.hex
    assert (appointment.appointment_type, appointment.phone) == ("Consulta", "5511999999999")
    assert appointment.start_at.replace(tzinfo=UTC) == SLOT_START
    assert env.calendar.created == [
        (SLOT_START, SLOT_END, "Consulta - Maria", deposit_id.hex)
    ]  # the event id is DETERMINISTIC
    assert items == [] and holds_left == []
    assert env.hooks == [(str(appointment.id), "flow")]
    (channel, text) = env.notices[0]
    assert channel == "whatsapp" and "Pagamento recebido" in text
    assert "04/03/2030 às 10:00" in text and "calendar.google.com" not in text.split("\n")[0]
    assert deposit.paid_notice_sent_at is not None
    assert conversation.flow_state == FlowState.IDLE  # out of AWAITING_DEPOSIT, selection cleared
    assert conversation.flow_selected_type is None


async def test_running_the_step_again_creates_nothing_and_sends_nothing(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    assert await _fulfill(db, deposit_id) == "promoted"
    assert await _fulfill(db, deposit_id) == "noop"
    assert await _fulfill(db, deposit_id) == "noop"
    _, appointments, _, _, _ = await _state(db, deposit_id)
    assert len(appointments) == 1
    assert len(env.calendar.created) == 1 and len(env.notices) == 1 and len(env.hooks) == 1


async def test_a_retry_after_a_failed_notice_finds_the_appointment_and_only_resends(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    env.notice_error = notify.DeliveryError("send_failed")

    with pytest.raises(fulfillment.PromotionRetry) as raised:
        await _fulfill(db, deposit_id)
    assert raised.value.code == "notice_undeliverable"
    deposit, appointments, _, _, _ = await _state(db, deposit_id)
    assert len(appointments) == 1 and deposit.fulfillment_status == FULFILLMENT_PROMOTING

    env.notice_error = None
    assert await _fulfill(db, deposit_id) == "promoted"
    _, appointments, _, _, _ = await _state(db, deposit_id)
    assert len(appointments) == 1
    assert len(env.calendar.created) == 1  # the event was NOT created again
    assert len(env.hooks) == 1  # and the hooks were NOT fired again
    assert len(env.notices) == 1


async def test_a_retry_after_the_event_but_before_the_row_reuses_the_deterministic_event(db, env):
    """Google holds OUR event (id = deposit hex) but the appointment row was never written."""
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    env.calendar.events = [{"id": deposit_id.hex, "summary": "Consulta - Maria"}]
    assert await _fulfill(db, deposit_id) == "promoted"  # our own event is not a conflict
    _, appointments, items, _, _ = await _state(db, deposit_id)
    assert len(appointments) == 1 and items == []


@pytest.mark.parametrize("late", [False, True])
async def test_a_taken_slot_is_never_double_booked_and_asks_the_clinic(db, env, late):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world, late=late)
    env.calendar.events = [{"id": "someone-elses-event", "summary": "Reunião"}]

    assert await _fulfill(db, deposit_id) == "slot_taken"

    deposit, appointments, items, _, _ = await _state(db, deposit_id)
    assert appointments == [] and env.calendar.created == []
    (item,) = items
    assert (item.reason, item.status) == ("late_slot_taken", "open")
    assert item.amount_cents == 6000 and item.paid_amount_cents == 6000
    assert item.slot_start.replace(tzinfo=UTC) == SLOT_START and item.service_label == "Consulta"
    assert item.patient_id == world.patient.id
    assert deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION
    assert deposit.status == PixDepositStatus.PAID  # the money is untouched: nobody refunds
    (_, text) = env.notices[0]
    assert text.startswith("Recebemos o seu pagamento, mas o horário")
    assert "A clínica vai falar com você" in text
    assert await _fulfill(db, deposit_id) == "noop"  # and never a second item or notice
    assert len(env.notices) == 1


async def test_a_slot_held_by_another_reservation_is_a_conflict_too(db, env):
    from uuid import uuid4

    from secretaria.models import Patient

    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world, late=True)
    async with db() as session:
        rival = Patient(id=uuid4(), tenant_id=world.tenant_id, wa_id="5511777777777")
        session.add(rival)
        await session.flush()
        conversation = Conversation(id=uuid4(), tenant_id=world.tenant_id, patient_id=rival.id)
        session.add(conversation)
        await session.commit()
    await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=conversation.id,
        patient_id=rival.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
    )
    assert await _fulfill(db, deposit_id) == "slot_taken"


async def test_a_late_payment_on_a_slot_that_is_still_free_reopens_the_reservation(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world, late=True)

    assert await _fulfill(db, deposit_id) == "promoted_late"

    _, appointments, items, _, _ = await _state(db, deposit_id)
    assert len(appointments) == 1 and items == []  # reopened: NO attention item, no refund
    (_, text) = env.notices[0]
    assert "reabri a sua reserva" in text and "Pagamento recebido" in text


async def test_google_down_retries_while_the_payment_is_recent_then_gives_up_to_the_clinic(
    db, env
):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    env.calendar.unavailable = True

    with pytest.raises(fulfillment.PromotionRetry):
        await _fulfill(db, deposit_id)
    deposit, appointments, items, _, _ = await _state(db, deposit_id)
    assert (appointments, items) == ([], [])
    assert deposit.fulfillment_status == FULFILLMENT_PROMOTING  # the sweep will pick it up

    async with db() as session:  # the payment is now old enough
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == deposit_id)
            .values(paid_at=datetime.now(UTC) - timedelta(minutes=20))
        )
        await session.commit()
    assert await _fulfill(db, deposit_id) == "promotion_failed"
    deposit, _, items, _, _ = await _state(db, deposit_id)
    assert [i.reason for i in items] == ["promotion_failed"]
    assert deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION
    assert "não conseguimos confirmar a consulta" in env.notices[0][1]


async def test_a_clinic_without_google_connected_is_treated_like_an_outage(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    env.calendar = None
    with pytest.raises(fulfillment.PromotionRetry):
        await _fulfill(db, deposit_id)


async def test_an_unreadable_snapshot_is_an_orphan_payment_for_the_clinic(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    async with db() as session:
        await session.execute(
            update(PixDeposit).where(PixDeposit.id == deposit_id).values(booking_snapshot=None)
        )
        await session.commit()
    assert await _fulfill(db, deposit_id) == "orphan_payment"
    _, appointments, items, _, _ = await _state(db, deposit_id)
    assert appointments == [] and [i.reason for i in items] == ["orphan_payment"]


async def test_a_portal_patient_is_told_on_the_portal_and_the_row_has_no_phone(db, env):
    async with db() as session:
        world = await make_world(session, channel="brain_message")
    deposit_id = await _paid_charge(db, world)
    assert await _fulfill(db, deposit_id) == "promoted"
    _, (appointment,), _, _, _ = await _state(db, deposit_id)
    assert appointment.phone is None  # never the UUID `external_id`
    assert env.notices[0][0] == "brain_message"


# ---- the expiry hook (jobs.EXPIRY_HOOKS) ------------------------------------------------------


async def _expiring_charge(db, world, *, conversation_state=FlowState.AWAITING_DEPOSIT):
    started = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
    )
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = conversation_state
        conversation.flow_selected_type = "Consulta"
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == started.deposit_id)
            .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
        )
        await session.commit()
    return started.deposit_id


def test_the_expiry_hook_is_registered_with_the_payment_jobs():
    assert deposit_wait.on_before_charge_expired in jobs.EXPIRY_HOOKS


async def test_an_expired_pix_frees_the_slot_parks_the_chat_on_the_retry_card_and_tells_once(
    db, env
):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _expiring_charge(db, world)
    sent, enqueue = recording_enqueue()

    async with db() as session:
        report = await jobs.expire_due_charges(session, now=datetime.now(UTC), enqueue=enqueue)
    assert report.expired == 1

    deposit, _, _, holds_left, _ = await _state(db, deposit_id)
    assert deposit.status == PixDepositStatus.EXPIRED and holds_left == []
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
    from secretaria.services.flow_router import STEP_AWAITING_RETRY

    assert (conversation.flow_state, conversation.flow_step) == (
        FlowState.SERVICE_CATALOG,
        STEP_AWAITING_RETRY,
    )
    assert conversation.flow_selected_type == "Consulta"  # one tap to pick the same slot again
    assert len(env.notices) == 1 and "O prazo do Pix acabou" in env.notices[0][1]
    assert deposit.expired_notice_sent_at is not None and deposit.conversation_id is None
    async with db() as session:  # a second tick finds nothing left to expire
        again = await jobs.expire_due_charges(session, now=datetime.now(UTC), enqueue=enqueue)
    assert again.expired == 0 and len(env.notices) == 1


async def test_a_patient_who_moved_on_gets_no_expiry_notice(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _expiring_charge(db, world, conversation_state=FlowState.IDLE)
    sent, enqueue = recording_enqueue()
    async with db() as session:
        await jobs.expire_due_charges(session, now=datetime.now(UTC), enqueue=enqueue)
    deposit, _, _, holds_left, _ = await _state(db, deposit_id)
    assert deposit.status == PixDepositStatus.EXPIRED and holds_left == []
    assert env.notices == []


def test_the_wait_verdict_separates_waiting_paid_and_over():
    now = datetime.now(UTC)
    open_deposit = PixDeposit(
        status=PixDepositStatus.AWAITING, expires_at=now + timedelta(minutes=5)
    )
    assert deposit_wait.wait_verdict(None) == "over"
    assert deposit_wait.wait_verdict(open_deposit) == "waiting"
    assert deposit_wait.wait_verdict(open_deposit, now=now + timedelta(minutes=6)) == "over"
    paid = PixDeposit(status=PixDepositStatus.PAID, expires_at=now - timedelta(minutes=5))
    assert deposit_wait.wait_verdict(paid) == "paid"  # never told "your Pix expired"
    expired = PixDeposit(status=PixDepositStatus.EXPIRED, expires_at=now + timedelta(minutes=5))
    assert deposit_wait.wait_verdict(expired) == "over"


async def test_the_patient_notice_is_claimed_atomically_so_a_second_run_never_repeats_it(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
        deposit = await session.get(PixDeposit, deposit_id)
        first = await fulfillment._send_once(session, tenant, deposit, "texto")
        second = await fulfillment._send_once(session, tenant, deposit, "texto")
    assert (first, second) == ("sent", "already")
    assert len(env.notices) == 1


async def test_a_failed_send_releases_the_claim_so_the_next_run_can_send(db, env):
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    env.notice_error = notify.DeliveryError("send_failed")
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
        deposit = await session.get(PixDeposit, deposit_id)
        assert await fulfillment._send_once(session, tenant, deposit, "texto") == "failed"
    assert (await _state(db, deposit_id))[0].paid_notice_sent_at is None
    env.notice_error = None
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
        deposit = await session.get(PixDeposit, deposit_id)
        assert await fulfillment._send_once(session, tenant, deposit, "texto") == "sent"


# ---- review fixes (TASK-017 final review, I1/I2/I3) ----------------------------------------------


async def _start_charge(world):
    return await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
    )


async def _apply_paid_event(db, env, world, deposit_id, *, paid_at=None):
    """The paid event reaching the event core for a charge that is still AWAITING."""
    import dataclasses

    from secretaria.services import tenant_config as cfg
    from secretaria.services.payments import event_processor
    from tests.payments_support import make_event

    env.provider.state = ChargeState(
        PaymentStatus.PAID, paid_amount_cents=6000, external_reference=str(deposit_id)
    )
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
        tenant = await session.get(type(world.tenant), world.tenant_id)
        account = await cfg.get_payment_account(session, world.tenant_id, "asaas")
        event = make_event(
            charge_id=deposit.provider_charge_id, reference=str(deposit_id), amount=6000
        )
        if paid_at is not None:
            event = dataclasses.replace(event, paid_at=paid_at)
        result = await event_processor.apply_normalized_event(
            session,
            tenant=tenant,
            account=account,
            provider=env.provider,
            event=event,
        )
        await session.commit()
    return result


async def test_a_paid_charge_keeps_its_slot_held_while_the_promotion_is_pending(db, env):
    """I1: the hold must outlive the Pix window once the money is in, or a patient who has not
    paid can take the slot of one who has."""
    async with db() as session:
        world = await make_world(session)
    started = await _start_charge(world)
    async with db() as session:  # the Pix window ends an instant before the webhook arrives
        await session.execute(
            update(holds.BookingHold).values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()

    result = await _apply_paid_event(db, env, world, started.deposit_id)

    assert result.outcome == "paid_before"
    async with db() as session:
        hold = (await session.scalars(select(holds.BookingHold))).one()
        assert hold.expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + fulfillment.GIVE_UP_AFTER
        assert await holds.live_hold_overlaps(
            session, world.tenant_id, SLOT_START, SLOT_END, professional_id=None
        )


async def test_the_retry_window_starts_when_we_learn_of_the_payment_not_at_the_providers_date(
    db, env
):
    """I2: Asaas reports a bare date (00:00 local); measuring the give-up age from it would make
    the first transient Google error final for every payment after 00:15."""
    async with db() as session:
        world = await make_world(session)
    started = await _start_charge(world)
    midnight = datetime.now(UTC).astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    await _apply_paid_event(db, env, world, started.deposit_id, paid_at=midnight)
    async with db() as session:
        deposit = await session.get(PixDeposit, started.deposit_id)
        assert deposit.last_payment_check_at is not None  # our own "learned at" stamp
        assert datetime.now(UTC) - deposit.last_payment_check_at.replace(tzinfo=UTC) < timedelta(
            minutes=1
        )
    env.calendar.unavailable = True

    with pytest.raises(fulfillment.PromotionRetry):  # NOT "promotion_failed"
        await _fulfill(db, started.deposit_id)
    _, _, items, _, _ = await _state(db, started.deposit_id)
    assert items == []


async def test_a_payment_never_confirms_a_slot_that_has_already_started(db, env):
    """I3: a late payment (or a Pix that outlived the slot start) must not create a consultation
    in the past; it is the `late_slot_taken` case - the clinic will call."""
    async with db() as session:
        world = await make_world(session)
    deposit_id = await _paid_charge(db, world)
    started = datetime.now(UTC) - timedelta(minutes=10)
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
        snapshot = dict(deposit.booking_snapshot)
        snapshot["start_at"] = started.isoformat()
        snapshot["end_at"] = (started + timedelta(minutes=30)).isoformat()
        deposit.booking_snapshot = snapshot
        await session.commit()

    assert await _fulfill(db, deposit_id) == "slot_taken"
    _, appointments, items, _, _ = await _state(db, deposit_id)
    assert appointments == [] and env.calendar.created == []
    assert [i.reason for i in items] == ["late_slot_taken"]
    assert "A clínica vai falar com você" in env.notices[0][1]


async def test_the_hold_and_the_pix_window_end_at_the_slot_start(db, env):
    """I3: the window is the clinic's 30 minutes, but never longer than the time left."""
    async with db() as session:
        world = await make_world(session)
    soon = datetime.now(UTC) + timedelta(minutes=12)
    started = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=soon,
        end_at=soon + timedelta(minutes=30),
    )
    assert started is not None
    assert started.minutes <= 12
    expires = started.expires_at
    expires = expires if expires.tzinfo else expires.replace(tzinfo=UTC)
    assert expires <= soon


async def test_a_slot_that_starts_within_the_minute_is_not_reserved_for_a_pix(db, env):
    async with db() as session:
        world = await make_world(session)
    imminent = datetime.now(UTC) + timedelta(seconds=30)
    result = await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=QUOTE,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=imminent,
        end_at=imminent + timedelta(minutes=30),
    )
    assert result is None  # the router sends the patient back to pick another time
    async with db() as session:
        assert list(await session.scalars(select(holds.BookingHold))) == []
        assert list(await session.scalars(select(PixDeposit))) == []
