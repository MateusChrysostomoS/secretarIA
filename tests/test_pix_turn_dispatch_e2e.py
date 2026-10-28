"""The Pix turns end to end: inbound ladder -> `_send_bot_reply_inner` (TASK-050 review, I-2).

When the Pix work was moved onto the split worker package, the three seams that connect the
inbound ladder to the Pix handlers moved with it into `workers/orchestrator.py` and
`workers/shared/draft_resolution.py`. Every other Pix test calls the handlers directly, so those
seams could be switched off with the suite still green. These tests drive the REAL path - the
real `_persist_inbound_message` against an in-memory database, then the real
`_send_bot_reply_inner` - and assert only what the patient sees and what the conversation
becomes:

  * a tap on the waiting card ("Já paguei" / "Trocar horário" / "Cancelar reserva") is answered
    by the wait, never by the router or the LLM;
  * the answer to the payer-data question (CPF / e-mail) is handled by the wait: an invalid one
    is asked again, the opt-out hands the chat to a human;
  * a valid answer completes the "Confirmar": the booking gate reads it back from Redis and the
    provider receives it - so the turn's Redis connection reaches the gate.
"""

import itertools
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.config import Settings
from secretaria.models import (
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    PixDeposit,
)
from secretaria.services import booking_hold as holds, email
from secretaria.services.payments import (
    booking_charge,
    deposit_lifecycle,
    payer_data as pdata,
    registry,
)
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests.before_support import SLOT_END, SLOT_START, entitlement_summary, make_world, new_charge
from tests.payments_support import FakeArqPool, FakeProvider, make_sessionmaker

PHONE_NUMBER_ID = "5511000000001"
WA_ID = "5511999999999"
DOCUMENT = "52998224725"  # a well-known valid test number, never a real person's
QUOTE = DepositQuote(
    "required",
    "required",
    amount_cents=6000,
    provider="asaas",
    ttl_minutes=30,
    service_label="Consulta",
)
LLM_TEXT = "resposta do LLM"


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

    def bodies(self) -> list[str]:
        return [entry[1] for entry in self.sent]


class NeedyAsaas(FakeProvider):
    """Asaas refuses a NEW customer without the payer's CPF (decision D7)."""

    async def create_pix_charge(self, account, request):
        self.calls.append(("create_pix_charge", request))
        if not (request.payer_document or request.provider_customer_id):
            raise ProviderError("payer_document_required", status_code=400)
        return self.created_charge


class Calendar:
    """The flow's agenda for the "Confirmar" turn; a gated booking never creates an event."""

    tzinfo = ZoneInfo("America/Sao_Paulo")

    def __init__(self) -> None:
        self.created: list = []

    async def create_event(self, start, end, summary, description="", **kwargs):
        self.created.append(start)
        return {"id": "evt-1"}


class Env:
    def __init__(self) -> None:
        self.provider = NeedyAsaas()
        self.provider.created_charge = new_charge()
        self.provider.state = ChargeState(PaymentStatus.PENDING)
        self.sender = Sender()
        self.calendar = Calendar()
        self.llm_calls: list[str] = []


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (workers_ns, holds, booking_charge):
        monkeypatch.setattr(module, "async_session_factory", maker)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    yield maker
    await engine.dispose()


@pytest.fixture
def env(monkeypatch) -> Env:
    e = Env()

    async def entitlements(tenant_id, redis):
        return entitlement_summary()

    async def send_email(*args, **kwargs):
        return None

    async def waba_token(session, tenant_id):
        return "wt"

    async def llm(*args, **kwargs):
        # Any turn that leaks past the Pix dispatch lands here: recorded, never a real model.
        e.llm_calls.append(args[0] if args else "")
        return LLM_TEXT

    monkeypatch.setattr(workers_ns, "get_entitlements", entitlements)
    monkeypatch.setattr(workers_ns, "get_waba_token", waba_token)
    monkeypatch.setattr(workers_ns, "run_agent", llm)
    monkeypatch.setattr(email, "send_payment_alert", send_email)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", e.provider)
    monkeypatch.setattr(workers_ns, "_reply_sender", lambda reply, tenant, token: e.sender)
    monkeypatch.setattr(workers_ns, "_flow_turn_calendar", lambda *args: e.calendar)
    return e


_WAM = itertools.count(1)


async def _turn(body: str, *, redis, reply_id: str | None = None):
    """One real turn: the inbound ladder, then the reply the worker sends for it."""
    reply = await tasks._persist_inbound_message(
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id=f"wamid.e2e.{next(_WAM)}",
        body=body,
        interactive_reply_id=reply_id,
    )
    assert reply is not None
    await tasks._send_bot_reply_inner(reply, redis=redis)
    return reply


async def _seed_world(db, *, last_bot_text: str | None = None):
    async with db() as session:
        world = await make_world(session, phone_number_id=PHONE_NUMBER_ID, is_active=True)
        world.patient.lgpd_accepted_at = datetime.now(UTC)
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.OUTBOUND if last_bot_text else MessageDirection.INBOUND,
                sender=MessageSender.BOT if last_bot_text else MessageSender.PATIENT,
                wam_id="wamid.seed",
                body=last_bot_text or "oi",
            )
        )
        await session.commit()
    return world


async def _conversation(db, world) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, world.conversation.id)


def _charge_requests(env):
    return [call[1] for call in env.provider.calls if call[0] == "create_pix_charge"]


# ---- (a) the waiting card's three taps -------------------------------------------------------


async def _waiting_for_the_pix(db):
    world = await _seed_world(db)
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
        payer_document=DOCUMENT,
    )
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.AWAITING_DEPOSIT
        conversation.flow_selected_type = "Consulta"
        conversation.flow_selected_day = "2030-03-04"
        conversation.flow_selected_slot = "2030-03-04T10:00"
        await session.commit()
    return world, started.deposit_id


async def test_ja_paguei_asks_the_provider_and_says_it_is_still_waiting(db, env):
    world, _ = await _waiting_for_the_pix(db)
    await _turn("Já paguei", redis=FakeArqPool())

    assert env.sender.bodies() == [deposit_lifecycle.BEFORE_STILL_WAITING_TEXT]
    assert env.provider.called("get_charge") == 1  # the provider is the authority on the money
    assert env.llm_calls == []
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_DEPOSIT


async def test_trocar_horario_voids_the_pix_and_offers_the_slot_list_again(db, env):
    world, deposit_id = await _waiting_for_the_pix(db)
    await _turn("Trocar horário", redis=FakeArqPool())

    assert env.sender.bodies() == [deposit_lifecycle.BEFORE_CHANGE_TEXT]
    assert env.llm_calls == []
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.SERVICE_CATALOG
    assert conversation.flow_selected_type == "Consulta"  # one tap to pick another slot
    async with db() as session:
        assert (await session.get(PixDeposit, deposit_id)).status.value != "pending"


async def test_cancelar_reserva_voids_the_pix_frees_the_slot_and_says_so(db, env):
    world, deposit_id = await _waiting_for_the_pix(db)
    await _turn("Cancelar reserva", redis=FakeArqPool())

    assert env.sender.bodies() == [deposit_lifecycle.BEFORE_RELEASED_TEXT]
    assert env.llm_calls == []
    assert (await _conversation(db, world)).flow_state == FlowState.IDLE
    async with db() as session:
        assert (await session.get(PixDeposit, deposit_id)).status.value != "pending"
        held = await holds.live_hold_overlaps(session, world.tenant_id, SLOT_START, SLOT_END)
    assert held is False  # the slot is free for anyone again


# ---- (b) the answer to the payer-data question ----------------------------------------------


async def _asked_for_the_cpf(db):
    world = await _seed_world(db, last_bot_text=pdata.ask_text("document", "asaas"))
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.AWAITING_PAYER_DATA
        conversation.flow_step = pdata.step_for("before", "document", 0)
        conversation.flow_selected_type = "Consulta"
        conversation.flow_selected_day = "2030-03-04"
        conversation.flow_selected_slot = "2030-03-04T10:00"
        await session.commit()
    return world


async def test_an_invalid_cpf_is_asked_again_with_the_way_out(db, env):
    world = await _asked_for_the_cpf(db)
    await _turn("meu cpf é 123", redis=FakeArqPool())

    assert env.sender.sent == [("buttons", pdata.invalid_text("document"), [pdata.LABEL_OPTOUT])]
    assert env.llm_calls == [] and _charge_requests(env) == []
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_PAYER_DATA


async def test_the_opt_out_hands_the_chat_to_a_human_and_says_nothing_was_reserved(db, env):
    world = await _asked_for_the_cpf(db)
    await _turn(pdata.LABEL_OPTOUT, redis=FakeArqPool(), reply_id=pdata.OPTOUT_REPLY_ID)

    assert env.sender.bodies() == [pdata.optout_text("before", "document")]
    assert env.llm_calls == [] and _charge_requests(env) == []
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.handover_state == HandoverState.HUMAN_ACTIVE


# ---- (c) a valid answer: the turn's Redis reaches the booking gate ---------------------------


async def test_a_valid_cpf_completes_the_confirmation_and_reaches_the_provider(db, env):
    """The answer is kept in Redis by the worker and read back by the booking gate of the SAME
    turn (`_turn_booking_gate(..., redis=)`). Without that connection the gate sees nothing,
    the provider refuses again and the patient would be asked for the CPF a second time."""
    world = await _asked_for_the_cpf(db)
    redis = FakeArqPool()

    await _turn("529.982.247-25", redis=redis)

    (request,) = _charge_requests(env)
    assert request.payer_document == DOCUMENT  # the value reached the provider, via the gate
    assert redis.store == {}  # and was forgotten the moment the charge existed
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.AWAITING_DEPOSIT
    assert env.calendar.created == []  # a gated booking puts nothing on Google
    assert env.llm_calls == []
    assert all(pdata.ask_text("document", "asaas") != body for body in env.sender.bodies())
    async with db() as session:
        (deposit,) = list(await session.scalars(select(PixDeposit)))
    assert deposit.conversation_id == world.conversation.id
