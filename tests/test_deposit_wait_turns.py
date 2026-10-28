"""`FlowState.AWAITING_DEPOSIT` at the turn level (workers/tasks.py, services/deposit_wait.py;
spec sections 4.2 items 1 and 3, decision D12, skill `conversation-flow-state`).

The bug in a state machine lives in the guard, not in the helper, so the first half drives the
REAL inbound ladder (`_persist_inbound_message`) against a real in-memory database: while a Pix is
open the whole turn belongs to the wait; every way OUT of the state is time-bounded and does not
depend on the patient. The second half runs the handler with a scripted sender and provider.
"""

import itertools
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from secretaria.config import Settings
from secretaria.models import (
    BookingHold,
    Conversation,
    FlowState,
    Message,
    MessageDirection,
    MessageSender,
    PixDeposit,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import booking_hold as holds, email, flow_router as fr
from secretaria.services.payments import booking_charge, deposit_lifecycle, registry
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests.before_support import (
    SLOT_END,
    SLOT_START,
    entitlement_summary,
    make_world,
    new_charge,
)
from tests.payments_support import FakeArqPool, FakeProvider, make_sessionmaker

PHONE_NUMBER_ID = "5511000000001"
QUOTE = DepositQuote(
    "required", "required", amount_cents=6000, provider="asaas", ttl_minutes=30,
    service_label="Consulta",
)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (workers_ns, holds, booking_charge):
        monkeypatch.setattr(module, "async_session_factory", maker)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    yield maker
    await engine.dispose()


class Sender:
    persists_outbound = False

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_text_message(self, to, body):
        self.sent.append(("text", body))
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", body, [label for _id, label in buttons]))
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}


class Env:
    def __init__(self) -> None:
        self.provider = FakeProvider()
        self.provider.created_charge = new_charge()
        self.provider.state = ChargeState(PaymentStatus.PENDING)
        self.sender = Sender()
        self.emails: list[tuple[str, str]] = []


@pytest.fixture
def env(monkeypatch) -> Env:
    e = Env()

    async def entitlements(tenant_id, redis):
        return entitlement_summary()

    async def send_email(to_email, clinic_name, kind):
        e.emails.append((to_email, kind))

    monkeypatch.setattr(email, "send_payment_alert", send_email)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", e.provider)
    monkeypatch.setattr(workers_ns, "_reply_sender", lambda reply, tenant, token: e.sender)
    return e


async def _waiting_world(db, *, expired=False, **kw):
    """A `before` clinic whose patient waits for a live Pix in AWAITING_DEPOSIT."""
    async with db() as session:
        world = await make_world(
            session, phone_number_id=PHONE_NUMBER_ID, is_active=True, **kw
        )
        world.patient.lgpd_accepted_at = datetime.now(UTC)
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.INBOUND,
                sender=MessageSender.PATIENT,
                wam_id="wamid.seed",
                body="oi",
            )
        )
        await session.commit()
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
        conversation.flow_selected_day = "2030-03-04"
        conversation.flow_selected_slot = "2030-03-04T10:00"
        if expired:
            await session.execute(
                update(PixDeposit)
                .where(PixDeposit.id == started.deposit_id)
                .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
            )
        await session.commit()
    return world, started.deposit_id


_WAM = itertools.count(1)


async def _inbound(body="oi", *, reply_id=None):
    return await tasks._persist_inbound_message(
        phone_number_id=PHONE_NUMBER_ID,
        wa_id="5511999999999",
        patient_name="Maria",
        wam_id=f"wamid.new.{next(_WAM)}",
        body=body,
        interactive_reply_id=reply_id,
    )


async def _conversation(db, world) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, world.conversation.id)


# ---- the inbound ladder ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "reply_id", "action"),
    [
        ("Já paguei", None, "paid"),
        ("Trocar horário", None, "change"),
        ("Cancelar reserva", None, "cancel"),
        ("já paguei", None, "paid"),
        ("qualquer coisa", "deposit_change", "change"),
        ("quanto custa a consulta?", None, "other"),
    ],
)
async def test_while_a_pix_is_open_the_whole_turn_belongs_to_the_wait(
    db, env, body, reply_id, action
):
    world, deposit_id = await _waiting_world(db)
    reply = await _inbound(body, reply_id=reply_id)
    assert reply is not None
    assert reply.deposit_wait_action == action
    assert reply.deposit_wait_id == deposit_id
    # Not the router, not the LLM, not a greeting: the state did not move.
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_DEPOSIT
    assert reply.greeting_override is None and reply.menu_requested is False


async def test_an_inbound_after_the_window_exits_the_wait_without_the_cron(db, env):
    world, _ = await _waiting_world(db, expired=True)
    reply = await _inbound("oi de novo")
    assert reply.deposit_wait_action == "expired"
    conversation = await _conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (
        FlowState.SERVICE_CATALOG,
        fr.STEP_AWAITING_RETRY,
    )
    assert conversation.flow_selected_type == "Consulta"  # one tap to choose the slot again


async def test_a_paid_charge_is_never_told_that_the_pix_expired(db, env):
    world, deposit_id = await _waiting_world(db, expired=True)
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == deposit_id)
            .values(status=PixDepositStatus.PAID, fulfillment_status="promoting")
        )
        await session.commit()
    reply = await _inbound("cadê minha consulta?")
    assert reply.deposit_wait_action == "paid_pending"
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_DEPOSIT


async def test_a_state_left_behind_with_no_open_deposit_still_leaves_on_its_own(db, env):
    """The cron already expired the charge but its hook failed: the conversation must not stay
    parked forever (the invariant of the skill: an exit that does not need the patient)."""
    world, deposit_id = await _waiting_world(db)
    async with db() as session:
        await session.execute(
            update(PixDeposit)
            .where(PixDeposit.id == deposit_id)
            .values(status=PixDepositStatus.EXPIRED)
        )
        await session.commit()
    reply = await _inbound("alô")
    assert reply.deposit_wait_action == "expired"
    assert (await _conversation(db, world)).flow_state == FlowState.SERVICE_CATALOG


async def test_a_state_with_no_deposit_row_at_all_also_leaves(db, env):
    world, deposit_id = await _waiting_world(db)
    async with db() as session:
        await session.execute(
            update(PixDeposit).where(PixDeposit.id == deposit_id).values(conversation_id=None)
        )
        await session.commit()
    assert (await _inbound("oi")).deposit_wait_action == "expired"


async def test_a_human_who_took_over_silences_the_bot_even_while_a_pix_is_open(db, env):
    world, _ = await _waiting_world(db)
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        from secretaria.models import HandoverState

        conversation.handover_state = HandoverState.HUMAN_ACTIVE
        await session.commit()
    assert await _inbound("já paguei") is None


async def test_menu_is_still_the_patients_way_out_and_it_wins_over_the_wait(db, env):
    world, _ = await _waiting_world(db)
    reply = await _inbound("/menu")
    assert reply.menu_requested is True and reply.deposit_wait_action is None


async def test_another_clinics_deposit_never_holds_this_conversation(db, env):
    world, deposit_id = await _waiting_world(db)
    async with db() as session:
        other = await make_world(session)
        await session.execute(
            update(PixDeposit).where(PixDeposit.id == deposit_id).values(tenant_id=other.tenant_id)
        )
        await session.commit()
    assert (await _inbound("oi")).deposit_wait_action == "expired"


# ---- the handler ------------------------------------------------------------------------------


def _reply(world, action, deposit_id=None):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref="5511999999999",
        inbound_body="x",
        tenant_id=world.tenant_id,
        deposit_wait_action=action,
        deposit_wait_id=deposit_id,
    )


async def _handle(world, env, db, action, deposit_id, *, redis=None):
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    await tasks._handle_deposit_wait(
        _reply(world, action, deposit_id),
        tenant=tenant,
        waba_token="wt",
        redis=redis if redis is not None else FakeArqPool(),
        patient_wa="5511999999999",
    )


async def _deposit(db, deposit_id) -> PixDeposit:
    async with db() as session:
        return await session.get(PixDeposit, deposit_id)


async def test_ja_paguei_with_a_paid_charge_runs_the_same_promotion_the_webhook_would(db, env):
    world, deposit_id = await _waiting_world(db)
    env.provider.state = ChargeState(PaymentStatus.PAID, paid_amount_cents=6000)
    pool = FakeArqPool()
    await _handle(world, env, db, "paid", deposit_id, redis=pool)
    assert env.sender.sent == [("text", deposit_lifecycle.BEFORE_PAYMENT_FOUND_TEXT)]
    # A synthetic event went through the SAME queue a webhook uses; nothing was marked paid here.
    assert [name for name, _a, _k in pool.jobs] == ["process_payment_event"]
    assert (await _deposit(db, deposit_id)).status == PixDepositStatus.AWAITING


@pytest.mark.parametrize("scripted", ["pending", "error"])
async def test_ja_paguei_without_a_payment_says_so_honestly_and_touches_nothing(
    db, env, scripted
):
    world, deposit_id = await _waiting_world(db)
    if scripted == "error":
        env.provider.get_error = ProviderError("http_503", status_code=503, retryable=True)
    pool = FakeArqPool()
    await _handle(world, env, db, "paid", deposit_id, redis=pool)
    assert env.sender.sent == [("text", deposit_lifecycle.BEFORE_STILL_WAITING_TEXT)]
    assert pool.jobs == []
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_DEPOSIT


async def test_trocar_horario_voids_the_pix_frees_the_slot_and_reopens_the_picker(db, env):
    world, deposit_id = await _waiting_world(db)
    await _handle(world, env, db, "change", deposit_id)

    assert env.provider.called("cancel_charge") == 1
    deposit = await _deposit(db, deposit_id)
    assert deposit.status == PixDepositStatus.EXPIRED
    async with db() as session:
        assert list(await session.scalars(select(BookingHold))) == []
    conversation = await _conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (
        FlowState.SERVICE_CATALOG,
        fr.STEP_AWAITING_RETRY,
    )
    assert conversation.flow_selected_type == "Consulta"
    assert env.sender.sent[0][0] == "buttons"
    assert deposit_lifecycle.BEFORE_CHANGE_TEXT in env.sender.sent[0][1]


async def test_cancelar_reserva_voids_the_pix_and_returns_to_idle(db, env):
    world, deposit_id = await _waiting_world(db)
    await _handle(world, env, db, "cancel", deposit_id)
    assert env.provider.called("cancel_charge") == 1
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE and conversation.flow_selected_type is None
    assert deposit_lifecycle.BEFORE_RELEASED_TEXT in env.sender.sent[0][1]


@pytest.mark.parametrize("action", ["change", "cancel"])
async def test_a_charge_that_turns_out_paid_is_never_voided_by_leaving(db, env, action):
    world, deposit_id = await _waiting_world(db)
    env.provider.state = ChargeState(PaymentStatus.PAID, paid_amount_cents=6000)
    await _handle(world, env, db, action, deposit_id)
    assert env.provider.called("cancel_charge") == 0
    assert (await _deposit(db, deposit_id)).status == PixDepositStatus.AWAITING
    async with db() as session:
        assert len(list(await session.scalars(select(BookingHold)))) == 1  # the slot stays held
    assert env.sender.sent == [("text", deposit_lifecycle.BEFORE_PAYMENT_FOUND_TEXT)]


async def test_any_other_text_repeats_the_card_and_moves_nothing(db, env):
    world, deposit_id = await _waiting_world(db)
    await _handle(world, env, db, "other", deposit_id)
    (kind, body, labels) = env.sender.sent[0]
    assert kind == "buttons" and "aguardando o pagamento" in body
    assert labels == ["Já paguei", "Trocar horário", "Cancelar reserva"]
    assert env.provider.called("cancel_charge") == 0
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_DEPOSIT


async def test_menu_voids_the_open_pix_before_the_slot_is_released(db, env):
    world, deposit_id = await _waiting_world(db)
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref="5511999999999",
        inbound_body="/menu",
        tenant_id=world.tenant_id,
        menu_requested=True,
    )
    await tasks._handle_show_main_menu(reply, tenant, [], "5511999999999", waba_token="wt")
    assert env.provider.called("cancel_charge") == 1
    assert (await _deposit(db, deposit_id)).status == PixDepositStatus.EXPIRED
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.MENU
    async with db() as session:
        assert list(await session.scalars(select(BookingHold))) == []


async def test_menu_with_a_paid_charge_shows_the_menu_but_never_deletes_the_payment(db, env):
    world, deposit_id = await _waiting_world(db)
    env.provider.state = ChargeState(PaymentStatus.PAID, paid_amount_cents=6000)
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref="5511999999999",
        inbound_body="/menu",
        tenant_id=world.tenant_id,
        menu_requested=True,
    )
    await tasks._handle_show_main_menu(reply, tenant, [], "5511999999999", waba_token="wt")
    assert env.provider.called("cancel_charge") == 0
    assert (await _deposit(db, deposit_id)).status == PixDepositStatus.AWAITING


# ---- the Portal: the code first, then the Pix on the SAME hold (decision D12) ----------------


async def _code_held_portal(db, **kw):
    async with db() as session:
        world = await make_world(session, channel="brain_message", **kw)
    held = await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
    )
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref=world.patient.external_id,
        inbound_body="123456",
        channel="brain_message",
        tenant_id=world.tenant_id,
    )
    return world, held, tenant, reply


async def test_a_verified_code_moves_the_same_hold_to_the_payment_phase(db, env):
    world, held, tenant, reply = await _code_held_portal(db)
    result = await tasks._payment_after_code(reply, tenant=tenant, redis=None)

    assert result.flow_state is FlowState.AWAITING_DEPOSIT
    assert result.deposit_request["amount_cents"] == 6000
    assert result.flow_selected_type == "Consulta"
    async with db() as session:
        (hold,) = list(await session.scalars(select(BookingHold)))
    assert hold.id == held.id and (hold.phase, hold.charge_id) == (
        "payment",
        result.deposit_request["deposit_id"],
    )
    # 10 minutes of code became the clinic's 30 minutes of Pix: no collision, no second hold.
    assert hold.expires_at.replace(tzinfo=UTC) > datetime.now(UTC) + timedelta(minutes=28)


async def test_a_clinic_on_after_promotes_exactly_as_before(db, env):
    world, held, tenant, reply = await _code_held_portal(db, timing="after")
    assert await tasks._payment_after_code(reply, tenant=tenant, redis=None) is None


async def test_a_code_hold_that_expired_before_the_code_is_not_turned_into_a_payment(db, env):
    world, held, tenant, reply = await _code_held_portal(db)
    async with db() as session:
        await session.execute(
            update(BookingHold)
            .where(BookingHold.id == held.id)
            .values(expires_at=datetime.now(UTC) - timedelta(seconds=1))
        )
        await session.commit()
    assert await tasks._payment_after_code(reply, tenant=tenant, redis=None) is None


async def test_a_payment_phase_hold_is_never_promoted_by_a_code(db, env):
    world, held, tenant, reply = await _code_held_portal(db)
    await tasks._payment_after_code(reply, tenant=tenant, redis=None)
    promoted = await tasks._promote_booking_hold(
        reply, tenant=tenant, waba_token=None, redis=None
    )
    assert promoted is None  # only the payment may create the appointment
    from secretaria.models import Appointment

    async with db() as session:
        assert list(await session.scalars(select(Appointment))) == []


async def test_when_the_deposit_cannot_be_asked_the_hold_is_released_and_a_human_is_called(
    db, env
):
    world, held, tenant, reply = await _code_held_portal(
        db, with_account=False, contact_email="c@example.com"
    )
    result = await tasks._payment_after_code(reply, tenant=tenant, redis=FakeArqPool())
    assert result.action == "handover"
    async with db() as session:
        assert list(await session.scalars(select(BookingHold))) == []
    assert env.emails == []  # owner answer 13: no operational e-mail


# ---- review fix I4: the lazy expiry asks the provider before it says "expired" ------------------


async def _lazy_expiry(world, env, db, reply, *, redis):
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    await tasks._handle_deposit_wait(
        reply, tenant=tenant, waba_token="wt", redis=redis, patient_wa="5511999999999"
    )


async def test_a_patient_who_paid_at_the_last_second_is_never_told_the_pix_expired(db, env):
    world, deposit_id = await _waiting_world(db, expired=True)
    env.provider.state = ChargeState(PaymentStatus.PAID, paid_amount_cents=6000)
    reply = await _inbound("Já paguei")  # the real ladder: the window is over, no cron ran yet
    assert reply.deposit_wait_action == "expired" and reply.deposit_wait_id == deposit_id
    pool = FakeArqPool()

    await _lazy_expiry(world, env, db, reply, redis=pool)

    assert env.sender.sent == [("text", deposit_lifecycle.BEFORE_PAYMENT_FOUND_TEXT)]
    assert [name for name, _a, _k in pool.jobs] == ["process_payment_event"]
    # the chat waits for the promotion again instead of offering a second booking
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_DEPOSIT


@pytest.mark.parametrize("scripted", ["pending", "error"])
async def test_a_window_that_really_ended_still_ends_with_the_retry_card(db, env, scripted):
    world, deposit_id = await _waiting_world(db, expired=True)
    if scripted == "error":
        env.provider.get_error = ProviderError("http_503", status_code=503, retryable=True)
    reply = await _inbound("oi")
    pool = FakeArqPool()

    await _lazy_expiry(world, env, db, reply, redis=pool)

    assert env.sender.sent[0][0] == "buttons"
    assert deposit_lifecycle.BEFORE_EXPIRED_TEXT in env.sender.sent[0][1]
    assert pool.jobs == []
    assert (await _conversation(db, world)).flow_state == FlowState.SERVICE_CATALOG
