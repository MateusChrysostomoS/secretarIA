"""Decision D7 (generalised) around the flow, `before` timing: who asks for the payer's data, where
it lives, where it must never appear (spec sections 5.3 and 14; skills
`pii-field-capture-pseudonymization` and `conversation-flow-state`).

The PROVIDER decides what is needed: Asaas refuses a new customer without a CPF/CNPJ, Mercado Pago
refuses a charge without the payer's e-mail. Nothing is asked until it says so, only the missing
field is asked, a customer the provider already knows is never asked again, and the answer travels
only as an in-memory reply field into an encrypted, expiring Redis key. Every test that has a
"kind" runs TWICE: `document` (Asaas) and `email` (Mercado Pago).
The first half drives the gate and the Portal continuation; the second half drives the REAL inbound
ladder (`_persist_inbound_message`) against a real in-memory database, and every "never" is asserted
over ALL tables and over the captured log events, not over a handful of columns.
"""

import itertools
import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from secretaria.ai import graph
from secretaria.ai.formatter import TextBubble
from secretaria.config import Settings
from secretaria.core.database import Base
from secretaria.models import (
    BookingHold,
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    Patient,
    PixDeposit,
)
from secretaria.services import (
    booking_hold as holds,
    email,
    flow_router as fr,
    pii_pseudonymization as pii_store,
)
from secretaria.services.payments import (
    accounts,
    booking_charge,
    deposit_failure_notice,
    deposit_gate,
    payer_data as pdata,
    registry,
)
from secretaria.services.payments.deposit_gate import DepositQuote
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.services.pending_identity import RequestCodeOutcome, RequestCodeResult
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests.before_support import (
    SLOT_END,
    SLOT_START,
    entitlement_summary,
    make_world,
    new_charge,
    recorded_logs,
)
from tests.payments_support import FakeProvider, FakeRedis, make_sessionmaker

PROVIDER = {"document": "asaas", "email": "mercadopago"}
DOCUMENT = "52998224725"  # a well-known valid test number, never a real person's
CNPJ = "11222333000181"
EMAIL = "maria.silva@example.com"
VALUE = {"document": DOCUMENT, "email": EMAIL}
# What the patient may type, and the value the reading extracts from it.
TYPED = {
    "document": [("529.982.247-25", DOCUMENT), (CNPJ, CNPJ)],
    "email": [("maria.silva@Example.com", EMAIL), ("meu email é maria.silva@example.com", EMAIL)],
}
INVALID = {
    "document": ["52998224726", "meu cpf é 123", "111.111.111-11"],
    "email": ["maria@", "meu email é maria.silva", "maria..silva@example.com"],
}
PHONE_NUMBER_ID = "5511000000001"
KINDS = ["document", "email"]


@pytest.fixture(params=KINDS)
def kind(request) -> str:
    return request.param


def _quote(kind: str) -> DepositQuote:
    return DepositQuote(
        "required",
        "required",
        amount_cents=6000,
        provider=PROVIDER[kind],
        ttl_minutes=30,
        service_label="Consulta",
    )


class NeedyProvider(FakeProvider):
    """The two references: a NEW Asaas customer needs the payer's document; Mercado Pago needs the
    payer's e-mail on every charge (it keeps no customer)."""

    kind = "document"

    async def create_pix_charge(self, account, request):
        self.calls.append(("create_pix_charge", request))
        knows_the_payer = request.payer_document or request.provider_customer_id
        if self.kind == "document" and not knows_the_payer:
            raise ProviderError("payer_document_required", status_code=400)
        if self.kind == "email" and not request.payer_email:
            raise ProviderError("payer_email_required")
        if self.create_error:
            raise self.create_error
        return self.created_charge


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
    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.provider = NeedyProvider(name=PROVIDER[kind])
        self.provider.kind = kind
        self.provider.created_charge = new_charge()
        self.provider.state = ChargeState(PaymentStatus.PENDING)
        self.sender = Sender()
        self.emails: list[tuple[str, str]] = []
        self.code_outcome = RequestCodeOutcome.NOT_PENDING
        self.waived: list[dict] = []  # TASK-071 Plan 3: deposits dropped, clinic told


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (workers_ns, holds, booking_charge):
        monkeypatch.setattr(module, "async_session_factory", maker)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    yield maker
    await engine.dispose()


@pytest.fixture
def env(monkeypatch, kind) -> Env:
    e = Env(kind)

    async def request_code(tenant_id, external_id):
        return RequestCodeResult(e.code_outcome, email_masked="m***a@gmail.com")

    async def entitlements(tenant_id, redis):
        return entitlement_summary()

    async def send_email(to_email, clinic_name, code):
        e.emails.append((to_email, code))

    monkeypatch.setattr(holds, "request_code", request_code)
    monkeypatch.setattr(email, "send_payment_alert", send_email)
    monkeypatch.setitem(registry._PROVIDERS, PROVIDER[kind], e.provider)
    async def notify(**kwargs):
        e.waived.append(kwargs)

    monkeypatch.setattr(workers_ns, "_reply_sender", lambda reply, tenant, token: e.sender)
    monkeypatch.setattr(deposit_failure_notice, "notify_deposit_failure", notify)
    return e


async def _world(session, kind: str, **kw):
    return await make_world(session, provider=PROVIDER[kind], **kw)


def _gate(world, *, armed: bool, redis) -> holds.BookingGate:
    return holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,
        external_id=world.patient.external_id or "",
        armed=armed,
        redis=redis,
    )


async def _decide(gate):
    return await gate.decide(
        start_at=SLOT_START,
        end_at=SLOT_END,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
    )


async def _remember(world, redis, kind) -> None:
    kept = await pdata.remember(
        redis,
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        kind=kind,
        value=VALUE[kind],
    )
    assert kept is True


async def _recall(world, redis, kind):
    return await pdata.recall(
        redis, tenant_id=world.tenant_id, conversation_id=world.conversation.id, kind=kind
    )


async def _holds(db):
    async with db() as session:
        return list(await session.scalars(select(BookingHold)))


async def _deposits(db):
    async with db() as session:
        return list(await session.scalars(select(PixDeposit)))


async def _everything_persisted(db) -> str:
    """Every row of EVERY table as text: what "never written to the database" is asserted on."""
    parts: list[str] = []
    async with db() as session:
        for table in Base.metadata.sorted_tables:
            parts.append(repr((await session.execute(table.select())).all()))
    return "\n".join(parts)


def _free(text: str, kind: str) -> bool:
    """The value, in any of the spellings the patient may type it, is nowhere in `text`."""
    haystack = text.casefold()
    forms = [VALUE[kind], *(typed for typed, _digits in TYPED[kind] if len(typed) < 40)]
    if kind == "document":
        forms.append("529.982.247-25")
    return not any(form.casefold() in haystack for form in forms)


def _charge_requests(env):
    return [call[1] for call in env.provider.calls if call[0] == "create_pix_charge"]


# ---- the gate: ask only when the provider says so, only what is missing, only once -------------


async def test_a_provider_that_needs_a_field_is_asked_and_nothing_is_held_or_charged(db, env, kind):
    async with db() as session:
        world = await _world(session, kind)
    decision = await _decide(_gate(world, armed=False, redis=FakeRedis()))
    assert (decision.outcome, decision.payer_field, decision.payer_provider) == (
        "needs_payer_data",
        kind,
        PROVIDER[kind],
    )
    assert await _holds(db) == [] and await _deposits(db) == []  # no reservation while asking
    assert env.emails == []  # a question is not an operational failure: the clinic is not alerted


async def test_with_the_value_remembered_the_pix_is_created_and_the_value_forgotten(db, env, kind):
    redis = FakeRedis()
    async with db() as session:
        world = await _world(session, kind)
    await _remember(world, redis, kind)

    decision = await _decide(_gate(world, armed=False, redis=redis))

    assert decision.outcome == "awaiting_payment"
    (request,) = _charge_requests(env)
    sent = request.payer_document if kind == "document" else request.payer_email
    assert sent == VALUE[kind]  # the provider is the ONLY place it goes
    other = request.payer_email if kind == "document" else request.payer_document
    assert other is None  # only what the provider asked for was asked, and only that was sent
    assert redis.store == {} and redis.ttls == {}  # gone the moment the charge exists
    assert _free(await _everything_persisted(db), kind)
    assert VALUE[kind] not in repr(request)  # `repr=False` on the transient fields


async def test_a_customer_asaas_already_knows_is_never_asked_again(db, env, monkeypatch):
    env.provider.kind = "document"
    # `env` registers the provider of the parametrized kind; this case is Asaas-only.
    monkeypatch.setitem(registry._PROVIDERS, PROVIDER["document"], env.provider)
    async with db() as session:
        world = await _world(session, "document")
        await accounts.remember_customer_id(
            session,
            tenant_id=world.tenant_id,
            patient_id=world.patient.id,
            provider="asaas",
            provider_customer_id="cus_known",
        )
        await session.commit()
    decision = await _decide(_gate(world, armed=False, redis=FakeRedis()))
    assert decision.outcome == "awaiting_payment"
    (request,) = _charge_requests(env)
    assert request.payer_document is None and request.provider_customer_id == "cus_known"


async def test_a_provider_that_still_wants_it_after_it_was_given_books_without_it_not_looping(
    db, env, kind
):
    redis = FakeRedis()
    async with db() as session:
        world = await _world(session, kind)
    await _remember(world, redis, kind)
    env.provider.create_error = ProviderError(
        "payer_document_required" if kind == "document" else "payer_email_required"
    )
    decision = await _decide(_gate(world, armed=False, redis=redis))
    # Asking again would be a loop the patient cannot win; TASK-071 Plan 3: a provider that still
    # refuses what it was given is a technical failure, so the booking commits WITHOUT the deposit.
    assert decision.outcome == "commit" and decision.deposit_waived_reason is not None
    assert len(env.waived) == 1
    assert await _recall(world, redis, kind) is None  # the provider saw it: forgotten
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_a_taken_slot_keeps_the_value_for_the_next_try_but_a_failed_charge_forgets_it(
    db, env, kind
):
    redis = FakeRedis()
    async with db() as session:
        world = await _world(session, kind)
        rival = Patient(id=uuid4(), tenant_id=world.tenant_id, wa_id="5511888888888")
        session.add(rival)
        await session.flush()
        rival_conversation = Conversation(
            id=uuid4(), tenant_id=world.tenant_id, patient_id=rival.id
        )
        session.add(rival_conversation)
        await session.commit()
    rival_hold = await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=rival_conversation.id,
        patient_id=rival.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
    )
    await _remember(world, redis, kind)
    assert (await _decide(_gate(world, armed=False, redis=redis))).outcome == "slot_taken"
    assert await _recall(world, redis, kind) == VALUE[kind]  # no provider call was made

    await holds.release(rival_hold.id)  # the rival gives the slot up; the provider now refuses
    env.provider.create_error = ProviderError("customer_failed", status_code=400)
    decision = await _decide(_gate(world, armed=False, redis=redis))
    # TASK-071 Plan 3 (was `blocked`): technical, so it books without the deposit.
    assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "unknown")
    assert await _recall(world, redis, kind) is None  # the provider saw it (or failed): forgotten
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_the_portal_is_asked_only_after_the_code_and_the_hold_is_not_kept_while_asking(
    db, env, kind
):
    """Also the rule for the Portal e-mail: the address the patient verified at login is NOT known
    to this service (brain-api hands over a masked copy only), so a provider that needs an e-mail
    asks the Portal patient like anyone else."""
    async with db() as session:
        world = await _world(session, kind, channel="brain_message")
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
    assert held is not None
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref=world.patient.external_id,
        inbound_body="123456",
        channel="brain_message",
        tenant_id=world.tenant_id,
    )

    result = await tasks._payment_after_code(reply, tenant=tenant, redis=FakeRedis())

    assert result.flow_state is FlowState.AWAITING_PAYER_DATA
    assert result.flow_step == pdata.step_for("before", kind, 0)
    (bubble,) = result.bubbles
    assert isinstance(bubble, TextBubble) and not hasattr(bubble, "labels")
    assert bubble.body == pdata.ask_text(kind, PROVIDER[kind])
    assert result.flow_selected_type == "Consulta"
    assert result.flow_selected_slot == "2030-03-04T10:00"  # the clinic's local 10:00, replayable
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_a_portal_visitor_who_gave_the_value_goes_straight_to_the_pix_after_the_code(
    db, env, kind
):
    redis = FakeRedis()
    async with db() as session:
        world = await _world(session, kind, channel="brain_message")
    await _remember(world, redis, kind)
    decision = await _decide(_gate(world, armed=True, redis=redis))  # already verified: no code
    assert decision.outcome == "awaiting_payment"
    (hold,) = await _holds(db)
    assert (hold.phase, hold.charge_id) == ("payment", decision.payment.deposit_id)
    assert redis.store == {}


async def test_the_confirmation_that_needs_the_value_parks_the_chat_and_creates_nothing(
    db, env, kind
):
    async with db() as session:
        world = await _world(session, kind)
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.SERVICE_CATALOG
        conversation.flow_step = fr.STEP_AWAITING_CONFIRMATION
        conversation.flow_selected_type = "Consulta"
        conversation.flow_selected_day = "2030-03-04"
        conversation.flow_selected_slot = "2030-03-04T10:00"
        await session.commit()
        tenant = await session.get(type(world.tenant), world.tenant_id)

    class Calendar:
        tzinfo = ZoneInfo("America/Sao_Paulo")

        def __init__(self) -> None:
            self.created: list = []

        async def create_event(self, start, end, summary, description=""):
            self.created.append(start)
            return {"id": "evt-1"}

    calendar = Calendar()
    result = await fr._handle_confirmation(
        conversation,
        tenant,
        calendar,
        fr.LABEL_CONFIRM,
        "Maria",
        professionals=[],
        gate=_gate(world, armed=False, redis=FakeRedis()),
    )

    assert calendar.created == []  # nothing on Google
    assert (result.flow_state, result.flow_step) == (
        FlowState.AWAITING_PAYER_DATA,
        pdata.step_for("before", kind, 0),
    )
    assert result.flow_selected_slot == "2030-03-04T10:00"
    (bubble,) = result.bubbles
    assert isinstance(bubble, TextBubble) and not hasattr(bubble, "labels")
    assert bubble.body == pdata.ask_text(kind, PROVIDER[kind])
    assert await _holds(db) == [] and await _deposits(db) == []


# ---- the inbound ladder: the answer is redacted at the boundary --------------------------------


_WAM = itertools.count(1)


async def _waiting_for_data(db, kind, **kw):
    async with db() as session:
        world = await _world(
            session, kind, phone_number_id=PHONE_NUMBER_ID, is_active=True, **kw
        )
        world.patient.lgpd_accepted_at = datetime.now(UTC)
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.AWAITING_PAYER_DATA
        conversation.flow_step = pdata.step_for("before", kind, 0)
        conversation.flow_selected_type = "Consulta"
        conversation.flow_selected_day = "2030-03-04"
        conversation.flow_selected_slot = "2030-03-04T10:00"
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                wam_id="wamid.ask",
                body=pdata.ask_text(kind, PROVIDER[kind]),
            )
        )
        await session.commit()
    return world


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


async def _inbound_bodies(db) -> list[str]:
    async with db() as session:
        rows = await session.scalars(
            select(Message.body).where(Message.direction == MessageDirection.INBOUND)
        )
        return list(rows)


@pytest.mark.parametrize("index", [0, 1])
async def test_the_answer_reaches_only_the_reply_context_and_the_history_row_is_redacted(
    db, env, kind, index
):
    typed, value = TYPED[kind][index]
    world = await _waiting_for_data(db, kind)
    reply = await _inbound(typed)

    assert reply.payer_data_answer == value
    assert (reply.payer_data_action, reply.payer_data_kind) == (None, kind)
    assert reply.inbound_body == fr.LABEL_CONFIRM  # the turn goes on as the confirmation
    assert value not in repr(reply) and typed not in repr(reply)
    conversation = await _conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (
        FlowState.SERVICE_CATALOG,
        fr.STEP_AWAITING_CONFIRMATION,
    )
    assert conversation.flow_selected_slot == "2030-03-04T10:00"
    assert await _inbound_bodies(db) == [pdata.REDACTED_BODY]
    persisted = (await _everything_persisted(db)).casefold()
    assert value.casefold() not in persisted and typed.casefold() not in persisted


async def test_three_invalid_answers_hand_the_chat_to_a_human_and_none_is_stored(db, env, kind):
    world = await _waiting_for_data(db, kind)
    first = await _inbound(INVALID[kind][0])
    assert first.payer_data_action == "invalid"
    assert (await _conversation(db, world)).flow_step == pdata.step_for("before", kind, 1)
    second = await _inbound(INVALID[kind][1])
    assert second.payer_data_action == "invalid"
    assert (await _conversation(db, world)).flow_step == pdata.step_for("before", kind, 2)
    third = await _inbound(INVALID[kind][2])
    assert third.payer_data_action == "exhausted"

    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE and conversation.flow_selected_type is None
    assert await _inbound_bodies(db) == [pdata.REDACTED_BODY] * 3
    persisted = await _everything_persisted(db)
    assert all(typed.casefold() not in persisted.casefold() for typed in INVALID[kind])


async def test_the_opt_out_ends_the_wait_without_holding_or_charging_anything(db, env, kind):
    world = await _waiting_for_data(db, kind)
    reply = await _inbound(pdata.LABEL_OPTOUT, reply_id=pdata.OPTOUT_REPLY_ID)
    assert reply.payer_data_action == "optout" and reply.payer_data_answer is None
    assert (await _conversation(db, world)).flow_state == FlowState.IDLE
    assert await _holds(db) == [] and await _deposits(db) == []
    assert env.provider.called("create_pix_charge") == 0


async def test_old_optout_button_tap_still_handled(db, env, kind):
    """A chat that was sent the old card can still tap its button: the reply id and the label keep
    meaning "no", even though no new request carries them."""
    world = await _waiting_for_data(db, kind)
    # A real tap arrives as the button's title in the body plus its id (the label is what is read).
    reply = await _inbound(pdata.LABEL_OPTOUT, reply_id=pdata.OPTOUT_REPLY_ID)
    assert reply.payer_data_action == "optout" and reply.payer_data_answer is None
    assert (await _conversation(db, world)).flow_state == FlowState.IDLE
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_silence_past_the_window_leaves_the_state_on_its_own_without_any_cron(db, env, kind):
    world = await _waiting_for_data(db, kind)
    async with db() as session:
        await session.execute(
            update(Message).values(created_at=datetime.now(UTC) - timedelta(minutes=20))
        )
        await session.commit()
    reply = await _inbound(TYPED[kind][0][0])  # a late answer, even a valid one, is too late
    assert reply.payer_data_action == "expired" and reply.payer_data_answer is None
    conversation = await _conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (
        FlowState.SERVICE_CATALOG,
        fr.STEP_AWAITING_RETRY,
    )
    assert conversation.flow_selected_type == "Consulta"  # one tap to pick the slot again
    assert await _inbound_bodies(db) == [pdata.REDACTED_BODY]


async def test_a_human_who_took_over_silences_the_bot_but_the_answer_is_still_redacted(
    db, env, kind
):
    world = await _waiting_for_data(db, kind)
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.handover_state = HandoverState.HUMAN_ACTIVE
        await session.commit()
    assert await _inbound(TYPED[kind][0][0]) is None
    assert await _inbound_bodies(db) == [pdata.REDACTED_BODY]


async def test_menu_is_still_the_way_out_and_it_wins_over_the_question(db, env, kind):
    await _waiting_for_data(db, kind)
    reply = await _inbound("/menu")
    assert reply.menu_requested is True and reply.payer_data_action is None


async def test_the_same_value_sent_again_while_the_pix_is_open_is_never_stored(db, env, kind):
    async with db() as session:
        world = await _world(session, kind, phone_number_id=PHONE_NUMBER_ID, is_active=True)
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
    await booking_charge.begin_payment(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        quote=_quote(kind),
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        attendee_name=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
        payer_document=DOCUMENT if kind == "document" else None,
        payer_email=EMAIL if kind == "email" else None,
    )
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.AWAITING_DEPOSIT
        await session.commit()

    reply = await _inbound(TYPED[kind][0][0])  # a double send: another wamid, the same content

    assert reply.deposit_wait_action == "other"  # the wait repeats its card, nothing else
    assert (await _conversation(db, world)).flow_state == FlowState.AWAITING_DEPOSIT
    assert _free(await _everything_persisted(db), kind)


def test_the_redaction_steps_are_the_routers_own_spellings():
    assert pdata._SHAPE_STEPS == {fr.STEP_AWAITING_CONFIRMATION, fr.STEP_AWAITING_RETRY}


def test_the_data_never_outlives_the_shortest_reservation_a_clinic_can_configure():
    assert pdata.DATA_TTL_SECONDS <= deposit_gate.HOLD_TTL_MIN_MINUTES * 60


# ---- the worker handlers: store, answer, forget ------------------------------------------------


def _reply(world, kind, **fields):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref="5511999999999",
        inbound_body="",
        tenant_id=world.tenant_id,
        payer_data_kind=kind,
        **fields,
    )


async def _tenant(db, world):
    async with db() as session:
        return await session.get(type(world.tenant), world.tenant_id)


async def test_keeping_the_answer_stores_it_encrypted_and_a_redis_outage_books_without_deposit(
    db, env, kind
):
    world = await _waiting_for_data(db, kind)
    tenant = await _tenant(db, world)
    reply = _reply(world, kind, payer_data_answer=VALUE[kind])

    redis = FakeRedis()
    assert await tasks._keep_payer_data(reply, tenant=tenant, redis=redis) is True
    assert await _recall(world, redis, kind) == VALUE[kind]
    assert VALUE[kind] not in "".join(str(v) for v in redis.store.values())

    class Down(FakeRedis):
        async def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    down = Down()
    assert await tasks._keep_payer_data(reply, tenant=tenant, redis=down) is False
    # TASK-071 Plan 3 (was: IDLE + human + BEFORE_BLOCKED_TEXT): the turn goes on as the
    # "Confirmar" with the deposit step waived; nothing is said here, the router books.
    waived = await tasks._payer_data_unavailable(reply, tenant=tenant, waba_token="wt", redis=down)
    assert waived is not None
    assert (waived.deposit_waived_reason, waived.payer_data_answer) == ("unknown", None)
    assert env.sender.sent == []
    conversation = await _conversation(db, world)
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE


@pytest.mark.parametrize("action", ["optout", "exhausted"])
async def test_a_refusal_frees_everything_says_so_and_offers_a_human(db, env, kind, action):
    """Owner, 2026-10-29: the patient chooses. What happened, then the fixed "quer que eu
    chame nosso atendente humano?" card (Sim / Não); nobody takes over on their own."""
    world = await _waiting_for_data(db, kind)
    tenant = await _tenant(db, world)
    redis = FakeRedis()
    await _remember(world, redis, kind)
    expected = (
        pdata.optout_text("before", kind) if action == "optout" else pdata.exhausted_text("before")
    )

    await tasks._handle_payer_data(
        _reply(world, kind, payer_data_action=action, payer_data_timing="before"),
        tenant=tenant,
        waba_token="wt",
        redis=redis,
    )

    assert await _recall(world, redis, kind) is None
    assert env.sender.sent == [
        ("text", expected),
        ("buttons", fr.HUMAN_OFFER_BODY, [fr.LABEL_YES, fr.LABEL_NO]),
    ]
    assert "equipe" not in expected
    conversation = await _conversation(db, world)
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE
    assert (conversation.flow_state, conversation.flow_step) == (
        FlowState.LLM,
        fr.STEP_HUMAN_OFFER,
    )
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_invalid_document_retry_has_no_button(db, env, kind):
    world = await _waiting_for_data(db, kind)
    tenant = await _tenant(db, world)
    await tasks._handle_payer_data(
        _reply(world, kind, payer_data_action="invalid"),
        tenant=tenant,
        waba_token="wt",
        redis=FakeRedis(),
    )
    assert env.sender.sent == [("text", pdata.invalid_text(kind))]
    assert (await _conversation(db, world)).handover_state != HandoverState.HUMAN_ACTIVE


async def test_an_expired_question_forgets_the_value_and_offers_the_retry_card(db, env, kind):
    world = await _waiting_for_data(db, kind)
    tenant = await _tenant(db, world)
    redis = FakeRedis()
    await _remember(world, redis, kind)
    await tasks._handle_payer_data(
        _reply(world, kind, payer_data_action="expired", payer_data_timing="before"),
        tenant=tenant,
        waba_token="wt",
        redis=redis,
    )
    assert await _recall(world, redis, kind) is None
    assert env.sender.sent[0][:2] == ("buttons", pdata.expired_text("before"))


async def test_menu_forgets_a_value_that_was_already_given(db, env, kind):
    world = await _waiting_for_data(db, kind)
    tenant = await _tenant(db, world)
    redis = FakeRedis()
    await _remember(world, redis, kind)
    reply = _reply(world, kind, menu_requested=True)
    await tasks._handle_show_main_menu(
        reply, tenant, [], "5511999999999", redis=redis, waba_token="wt", source="command"
    )
    assert await _recall(world, redis, kind) is None


# ---- the proof the owner asked for: not in any log, not in what the LLM is given ---------------


async def test_no_log_event_of_the_whole_path_carries_the_value(db, env, kind):
    world = await _waiting_for_data(db, kind)
    tenant = await _tenant(db, world)
    redis = FakeRedis()
    value = VALUE[kind]

    class Down(FakeRedis):
        async def set(self, *args, **kwargs):
            raise ConnectionError(f"redis down while storing {value}")

    with recorded_logs() as logs:
        reply = await _inbound(TYPED[kind][0][0])
        # a store that fails with the value IN its exception text: only the type is logged
        assert await tasks._keep_payer_data(reply, tenant=tenant, redis=Down()) is False
        assert await tasks._keep_payer_data(reply, tenant=tenant, redis=redis) is True
        decision = await _decide(_gate(world, armed=False, redis=redis))
        assert decision.outcome == "awaiting_payment"
    assert "payer_data_store_failed" in [entry["event"] for entry in logs]  # non-vacuous
    assert _free(json.dumps(logs, default=str), kind)


async def test_the_answer_never_reaches_the_llm_history(db, env, kind, monkeypatch):
    """Drive the real capture, then the real `run_agent`, and spy only the call that becomes the
    OpenAI request (recipe of `pii-field-capture-pseudonymization`)."""
    world = await _waiting_for_data(db, kind)
    await _inbound(TYPED[kind][0][0])  # captured through the real ladder, not written by hand
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.LLM
        conversation.flow_step = None
        await session.commit()
    await _inbound("obrigada")

    seen: list = []

    async def _spy(messages):
        seen.append(messages)
        return "ok"

    monkeypatch.setattr(graph, "invoke_agent", _spy)
    monkeypatch.setattr(graph, "async_session_factory", db)
    monkeypatch.setattr(pii_store, "async_session_factory", db)
    await graph.run_agent("obrigada", context={"conversation_id": str(world.conversation.id)})

    # the EXACT message list handed to the LLM stub, every part of every message
    history = "\n".join(str(m.content) for m in seen[0])
    assert pdata.REDACTED_BODY in history  # non-vacuous: the answer's row is in the history
    assert _free(history, kind)
    assert _free(repr(seen[0]), kind)


# ---- the placeholder of the redacted answer is storage-only, never a bubble --------------------


def test_is_redacted_placeholder_is_the_one_stored_marker_and_nothing_else():
    assert pdata.is_redacted_placeholder(pdata.REDACTED_BODY) is True
    assert pdata.is_redacted_placeholder("[dado oculto] ") is False
    assert pdata.is_redacted_placeholder("") is False
    assert pdata.is_redacted_placeholder(None) is False
    assert pdata.is_redacted_placeholder("oi") is False


async def _outbound_bodies(db) -> list[str | None]:
    async with db() as session:
        rows = await session.scalars(
            select(Message.body).where(Message.direction == MessageDirection.OUTBOUND)
        )
        return list(rows)


async def test_no_outbound_bubble_contains_redaction_marker(db, env, kind):
    """A whole turn around the answer (invalid x3, each re-asked, then a person offered): the
    marker is what is STORED for the answer; it is not what the bot says, in any bubble or row."""
    world = await _waiting_for_data(db, kind)
    tenant = await _tenant(db, world)
    for answer in INVALID[kind]:
        reply = await _inbound(answer)
        await tasks._handle_payer_data(reply, tenant=tenant, waba_token="wt", redis=FakeRedis())

    assert len(env.sender.sent) == 4  # two re-asks, what happened and the offer: non-vacuous
    assert await _inbound_bodies(db) == [pdata.REDACTED_BODY] * 3  # stored, as the history needs
    assert pdata.REDACTED_BODY not in repr(env.sender.sent)
    assert all(pdata.REDACTED_BODY not in (body or "") for body in await _outbound_bodies(db))


async def test_no_outbound_bubble_contains_redaction_marker_when_the_answer_is_taken(db, env, kind):
    """The success turn: the answer continues as the confirmation, never as the marker."""
    await _waiting_for_data(db, kind)
    reply = await _inbound(TYPED[kind][0][0])
    assert reply is not None and reply.payer_data_answer is not None
    assert reply.inbound_body == fr.LABEL_CONFIRM
    assert pdata.REDACTED_BODY not in repr(reply)
    assert await _inbound_bodies(db) == [pdata.REDACTED_BODY]
    assert env.sender.sent == []
    assert all(pdata.REDACTED_BODY not in (body or "") for body in await _outbound_bodies(db))
