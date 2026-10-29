"""Decision D7 (generalised), `after` timing: the consultation is already booked and confirmed; the
provider needs a payer field before the Pix can exist, so the patient is asked RIGHT BEFORE the Pix
is sent (owner answers 8 and 9 of 2026-09-29: Asaas asks CPF/CNPJ, Mercado Pago asks the e-mail,
in both timings, only when the provider requires it and the patient has none on file).

Every test with a "kind" runs twice: `document` (Asaas) and `email` (Mercado Pago). The consultation
must stand through every path; the Pix is the only thing that may be skipped, and never in silence
for a patient who was asked.
"""

import itertools
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from secretaria.ai import graph
from secretaria.config import Settings
from secretaria.core.database import Base
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    PixDeposit,
)
from secretaria.plugins import pix_deposit
from secretaria.plugins.base import PostBookingContext
from secretaria.services import pii_pseudonymization as pii_store
from secretaria.services.payments import (
    deposit_lifecycle,
    notify,
    payer_data as pdata,
    payer_data_after,
    registry,
)
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.workers import tasks
from tests._patching import workers_ns
from tests.before_support import SLOT_END, SLOT_START, make_world, new_charge, recorded_logs
from tests.payments_support import FakeProvider, FakeRedis, make_sessionmaker

PROVIDER = {"document": "asaas", "email": "mercadopago"}
DOCUMENT = "52998224725"  # a well-known valid test number, never a real person's
EMAIL = "maria.silva@example.com"
VALUE = {"document": DOCUMENT, "email": EMAIL}
TYPED = {"document": "529.982.247-25", "email": "maria.silva@Example.com"}
PHONE_NUMBER_ID = "5511000000001"
KINDS = ["document", "email"]


@pytest.fixture(params=KINDS)
def kind(request) -> str:
    return request.param


class NeedyProvider(FakeProvider):
    """`needs` lists the fields the provider insists on: Asaas wants the document of a NEW
    customer, Mercado Pago the e-mail of every charge; `both` is the worst case of a provider that
    wants the two."""

    needs: tuple[str, ...] = ("document",)

    async def create_pix_charge(self, account, request):
        self.calls.append(("create_pix_charge", request))
        if "document" in self.needs and not request.payer_document:
            raise ProviderError("payer_document_required", status_code=400)
        if "email" in self.needs and not request.payer_email:
            raise ProviderError("payer_email_required")
        if self.create_error:
            raise self.create_error
        return self.created_charge


class FakeWhatsApp:
    """Mirrors `WhatsAppClient` for the two seams that send: `notify.deliver` (the question) and
    the Pix request of `deposit_lifecycle`. Every send lands in the class-level `log`."""

    persists_outbound = False
    log: list[tuple] = []
    fail_on: str | None = None

    def __init__(self, *args, **kwargs):
        pass

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        if FakeWhatsApp.fail_on == "text":
            raise RuntimeError("meta down")
        FakeWhatsApp.log.append(("text", body))
        return {"messages": [{"id": f"wamid.{len(FakeWhatsApp.log)}"}]}

    async def send_buttons(self, to, body, buttons):
        if FakeWhatsApp.fail_on == "buttons":
            raise RuntimeError("meta down")
        FakeWhatsApp.log.append(("buttons", body))
        return {"messages": [{"id": f"wamid.{len(FakeWhatsApp.log)}"}]}


class Sender:
    """The turn's own sender (`tasks._reply_sender`)."""

    persists_outbound = False

    def __init__(self) -> None:
        self.sent: list[tuple] = []

    async def send_text_message(self, to, body):
        self.sent.append(("text", body))
        return {"messages": [{"id": f"wamid.t{len(self.sent)}"}]}

    async def send_buttons(self, to, body, buttons):
        self.sent.append(("buttons", body, [label for _id, label in buttons]))
        return {"messages": [{"id": f"wamid.t{len(self.sent)}"}]}


class Env:
    def __init__(self, kind: str) -> None:
        self.provider = NeedyProvider(name=PROVIDER[kind])
        self.provider.needs = ("document",) if kind == "document" else ("email",)
        self.provider.created_charge = new_charge()
        self.provider.state = ChargeState(PaymentStatus.PENDING)
        self.sender = Sender()


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (workers_ns, notify, pix_deposit):
        monkeypatch.setattr(module, "async_session_factory", maker)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", FakeWhatsApp)
    FakeWhatsApp.log, FakeWhatsApp.fail_on = [], None
    yield maker
    await engine.dispose()


@pytest.fixture
def env(monkeypatch, kind) -> Env:
    e = Env(kind)
    monkeypatch.setitem(registry._PROVIDERS, PROVIDER[kind], e.provider)
    monkeypatch.setattr(workers_ns, "_reply_sender", lambda reply, tenant, token: e.sender)
    return e


async def _booked(db, kind, *, state=FlowState.IDLE, step=None, **kw):
    """A clinic on `after` with a confirmed consultation and its conversation in `state`."""
    async with db() as session:
        world = await make_world(
            session,
            provider=PROVIDER[kind],
            timing="after",
            phone_number_id=PHONE_NUMBER_ID,
            is_active=True,
            **kw,
        )
        world.patient.lgpd_accepted_at = datetime.now(UTC)
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = state
        conversation.flow_step = step
        appointment = Appointment(
            id=uuid4(),
            tenant_id=world.tenant_id,
            patient_id=world.patient.id,
            google_event_id="evt-1",
            appointment_type="Consulta",
            phone=world.patient.wa_id,
            status=AppointmentStatus.SCHEDULED,
            start_at=SLOT_START,
            end_at=SLOT_END,
        )
        session.add(appointment)
        await session.commit()
    return world, appointment


def _key(world):
    return {"tenant_id": world.tenant_id, "conversation_id": world.conversation.id}


async def _ask(db, world, appointment, kind, redis):
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
        patient = await session.get(type(world.patient), world.patient.id)
        return await payer_data_after.ask_after_booking(
            session,
            tenant=tenant,
            patient=patient,
            appointment=appointment,
            kind=kind,
            provider=PROVIDER[kind],
            waba_token="wt",
            redis=redis,
            conversation_id=world.conversation.id,
            whatsapp_cls=FakeWhatsApp,
        )


async def _conversation(db, world) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, world.conversation.id)


async def _still_booked(db, appointment) -> bool:
    async with db() as session:
        row = await session.get(Appointment, appointment.id)
    return row is not None and row.status == AppointmentStatus.SCHEDULED


async def _deposits(db):
    async with db() as session:
        return list(await session.scalars(select(PixDeposit)))


async def _everything_persisted(db) -> str:
    parts: list[str] = []
    async with db() as session:
        for table in Base.metadata.sorted_tables:
            parts.append(repr((await session.execute(table.select())).all()))
    return "\n".join(parts).casefold()


def _free(text: str, kind: str) -> bool:
    haystack = text.casefold()
    return VALUE[kind].casefold() not in haystack and TYPED[kind].casefold() not in haystack


# ---- asking: the hook, the busy chat, the failures ---------------------------------------------


async def test_the_hook_asks_when_the_provider_needs_a_field_and_the_consultation_stands(
    db, env, kind, monkeypatch
):
    world, appointment = await _booked(db, kind)
    redis = FakeRedis()
    asked: list[dict] = []

    async def spy(session, **kwargs):
        asked.append(kwargs)
        return True

    monkeypatch.setattr(payer_data_after, "ask_after_booking", spy)
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
        patient = await session.get(type(world.patient), world.patient.id)
    await pix_deposit._post_booking(
        PostBookingContext(
            tenant=tenant,
            patient=patient,
            appointment=appointment,
            waba_token="wt",
            source="flow",
            redis=redis,
        )
    )

    (call,) = asked
    assert (call["kind"], call["provider"]) == (kind, PROVIDER[kind])
    assert call["conversation_id"] == world.conversation.id and call["redis"] is redis
    assert env.provider.called("create_pix_charge") == 1  # one refused attempt, nothing created
    assert await _deposits(db) == []
    assert await _still_booked(db, appointment)


async def test_after_timing_request_has_no_button(db, env, kind):
    world, appointment = await _booked(db, kind)
    redis = FakeRedis()

    assert await _ask(db, world, appointment, kind, redis) is True

    conversation = await _conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (
        FlowState.AWAITING_PAYER_DATA,
        pdata.step_for("after", kind, 0),
    )
    assert await pdata.recall_pending(redis, **_key(world)) == appointment.id
    assert FakeWhatsApp.log == [("text", pdata.ask_text(kind, PROVIDER[kind]))]
    assert redis.ttls[f"payer_pending:{world.tenant_id}:{world.conversation.id}"] == (
        pdata.DATA_TTL_SECONDS
    )


@pytest.mark.parametrize(
    "busy", [FlowState.SERVICE_CATALOG, FlowState.AWAITING_DEPOSIT, FlowState.AWAITING_EMAIL_CODE]
)
async def test_a_chat_that_moved_on_is_not_hijacked(db, env, kind, busy):
    world, appointment = await _booked(db, kind, state=busy)
    redis = FakeRedis()
    assert await _ask(db, world, appointment, kind, redis) is False
    assert (await _conversation(db, world)).flow_state == busy
    assert redis.store == {} and FakeWhatsApp.log == []


async def test_without_redis_nothing_is_asked_and_the_consultation_stands(db, env, kind):
    world, appointment = await _booked(db, kind)
    assert await _ask(db, world, appointment, kind, None) is False
    assert (await _conversation(db, world)).flow_state == FlowState.IDLE
    assert FakeWhatsApp.log == [] and await _deposits(db) == []


async def test_a_question_that_cannot_be_delivered_frees_the_chat_and_forgets_the_pointer(
    db, env, kind
):
    world, appointment = await _booked(db, kind)
    redis = FakeRedis()
    FakeWhatsApp.fail_on = "text"
    assert await _ask(db, world, appointment, kind, redis) is False
    conversation = await _conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (FlowState.IDLE, None)
    assert redis.store == {}


# ---- the answer: redacted at the boundary, then the Pix ----------------------------------------


async def _waiting(db, kind, **kw):
    world, appointment = await _booked(
        db, kind, state=FlowState.AWAITING_PAYER_DATA, step=pdata.step_for("after", kind, 0), **kw
    )
    return world, appointment


_WAM = itertools.count(1)


async def _inbound(body):
    return await tasks._persist_inbound_message(
        phone_number_id=PHONE_NUMBER_ID,
        wa_id="5511999999999",
        patient_name="Maria",
        wam_id=f"wamid.after.{next(_WAM)}",
        body=body,
        interactive_reply_id=None,
    )


async def test_the_answer_is_redacted_and_the_turn_resumes_the_pix_not_a_confirmation(
    db, env, kind
):
    world, _appointment = await _waiting(db, kind)
    reply = await _inbound(TYPED[kind])

    assert (reply.payer_data_action, reply.payer_data_kind, reply.payer_data_timing) == (
        "captured_after",
        kind,
        "after",
    )
    assert reply.payer_data_answer == VALUE[kind]
    assert VALUE[kind] not in repr(reply) and TYPED[kind] not in repr(reply)
    conversation = await _conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (FlowState.IDLE, None)
    async with db() as session:
        bodies = list(
            await session.scalars(
                select(Message.body).where(Message.direction == MessageDirection.INBOUND)
            )
        )
    assert bodies == [pdata.REDACTED_BODY]
    assert _free(await _everything_persisted(db), kind)


async def test_the_answer_creates_and_sends_the_pix_and_forgets_everything(db, env, kind):
    world, appointment = await _waiting(db, kind)
    redis = FakeRedis()
    assert await pdata.remember_pending(redis, appointment_id=appointment.id, **_key(world))
    reply = await _inbound(TYPED[kind])
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)

    await tasks._handle_payer_data(reply, tenant=tenant, waba_token="wt", redis=redis)

    (deposit,) = await _deposits(db)
    assert deposit.appointment_id == appointment.id and deposit.deposit_timing == "after"
    request = env.provider.calls[-1][1]
    sent = request.payer_document if kind == "document" else request.payer_email
    assert sent == VALUE[kind]  # only the provider received it
    assert any("PIXCOPIAECOLA" in entry[1] for entry in FakeWhatsApp.log)  # the Pix went out
    assert redis.store == {} and redis.ttls == {}  # value and pointer are gone
    assert _free(await _everything_persisted(db), kind)
    assert (await _conversation(db, world)).flow_state == FlowState.IDLE


async def test_a_provider_that_wants_both_fields_is_asked_for_the_second_after_the_first(
    db, monkeypatch
):
    provider = NeedyProvider(name="asaas")
    provider.needs = ("document", "email")
    provider.created_charge = new_charge()
    monkeypatch.setitem(registry._PROVIDERS, "asaas", provider)
    world, appointment = await _booked(db, "document")
    redis = FakeRedis()
    await pdata.remember_pending(redis, appointment_id=appointment.id, **_key(world))
    await pdata.remember(redis, kind="document", value=DOCUMENT, **_key(world))
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
        patient = await session.get(type(world.patient), world.patient.id)
        verdict = await payer_data_after.resume_after_booking(
            session,
            tenant=tenant,
            patient=patient,
            conversation_id=world.conversation.id,
            waba_token="wt",
            redis=redis,
            whatsapp_cls=FakeWhatsApp,
        )

    assert verdict == "asked_again"
    conversation = await _conversation(db, world)
    assert conversation.flow_step == pdata.step_for("after", "email", 0)
    assert await pdata.recall(redis, kind="document", **_key(world)) == DOCUMENT  # not asked twice
    assert await pdata.recall_pending(redis, **_key(world)) == appointment.id
    assert FakeWhatsApp.log == [("text", pdata.ask_text("email", "asaas"))]
    assert await _deposits(db) == []


async def test_a_provider_that_refuses_after_the_answer_tells_the_patient_and_calls_a_human(
    db, env, kind
):
    world, appointment = await _waiting(db, kind)
    env.provider.create_error = ProviderError("customer_failed", status_code=400)
    redis = FakeRedis()
    await pdata.remember_pending(redis, appointment_id=appointment.id, **_key(world))
    reply = await _inbound(TYPED[kind])
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)

    await tasks._handle_payer_data(reply, tenant=tenant, waba_token="wt", redis=redis)

    assert env.sender.sent == [("text", pdata.AFTER_FAILED_TEXT)]
    assert (await _conversation(db, world)).handover_state == HandoverState.HUMAN_ACTIVE
    assert await _deposits(db) == [] and redis.store == {}
    assert await _still_booked(db, appointment)


async def test_an_answer_whose_pointer_was_lost_tells_the_patient_and_calls_a_human(db, env, kind):
    world, _appointment = await _waiting(db, kind)
    redis = FakeRedis()  # the pointer is gone (a Redis flush): there is nothing to resume
    reply = await _inbound(TYPED[kind])
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)

    await tasks._handle_payer_data(reply, tenant=tenant, waba_token="wt", redis=redis)

    assert env.sender.sent == [("text", pdata.AFTER_FAILED_TEXT)]
    assert (await _conversation(db, world)).handover_state == HandoverState.HUMAN_ACTIVE
    assert await _deposits(db) == [] and redis.store == {}


# ---- the ways out: the same limits as `before`, and a human when the Pix is lost ---------------


@pytest.mark.parametrize("action", ["optout", "exhausted", "expired"])
async def test_a_refusal_keeps_the_consultation_forgets_everything_and_calls_a_human(
    db, env, kind, action
):
    world, appointment = await _waiting(db, kind)
    redis = FakeRedis()
    await pdata.remember(redis, kind=kind, value=VALUE[kind], **_key(world))
    await pdata.remember_pending(redis, appointment_id=appointment.id, **_key(world))
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    texts = {
        "optout": pdata.optout_text("after", kind),
        "exhausted": pdata.exhausted_text("after"),
        "expired": pdata.expired_text("after"),
    }
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref="5511999999999",
        inbound_body="",
        tenant_id=world.tenant_id,
        payer_data_action=action,
        payer_data_timing="after",
        payer_data_kind=kind,
    )

    await tasks._handle_payer_data(reply, tenant=tenant, waba_token="wt", redis=redis)

    assert env.sender.sent == [("text", texts[action])]
    assert "continua agendada" in texts[action]
    assert (await _conversation(db, world)).handover_state == HandoverState.HUMAN_ACTIVE
    assert redis.store == {} and await _deposits(db) == []
    assert await _still_booked(db, appointment)


async def test_silence_past_the_window_leaves_the_state_on_its_own_without_any_cron(db, env, kind):
    world, _appointment = await _waiting(db, kind)
    async with db() as session:
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
        await session.execute(
            update(Message).values(created_at=datetime.now(UTC) - timedelta(minutes=20))
        )
        await session.commit()
    reply = await _inbound(TYPED[kind])  # a late answer, even a valid one, is too late
    assert reply.payer_data_action == "expired" and reply.payer_data_answer is None
    conversation = await _conversation(db, world)
    assert (conversation.flow_state, conversation.flow_step) == (FlowState.IDLE, None)
    # (no slot to pick again: the consultation is booked, so no retry card either)


async def test_menu_forgets_the_value_and_the_pointer_of_an_after_wait(db, env, kind):
    world, appointment = await _waiting(db, kind)
    redis = FakeRedis()
    await pdata.remember(redis, kind=kind, value=VALUE[kind], **_key(world))
    await pdata.remember_pending(redis, appointment_id=appointment.id, **_key(world))
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref="5511999999999",
        inbound_body="",
        tenant_id=world.tenant_id,
        payer_data_kind=kind,
        menu_requested=True,
    )
    await tasks._handle_show_main_menu(
        reply, tenant, [], "5511999999999", redis=redis, waba_token="wt", source="command"
    )
    assert redis.store == {}


# ---- the proof the owner asked for: not in any log, not in what the LLM is given ---------------


async def test_no_log_event_of_the_after_path_carries_the_value(db, env, kind):
    world, appointment = await _waiting(db, kind)
    redis = FakeRedis()
    await pdata.remember_pending(redis, appointment_id=appointment.id, **_key(world))
    async with db() as session:
        tenant = await session.get(type(world.tenant), world.tenant_id)
    with recorded_logs() as logs:
        reply = await _inbound(TYPED[kind])
        await tasks._handle_payer_data(reply, tenant=tenant, waba_token="wt", redis=redis)
    assert logs  # non-vacuous
    assert _free(repr(logs), kind)


async def test_the_after_answer_never_reaches_the_llm_history(db, env, kind, monkeypatch):
    world, _appointment = await _waiting(db, kind)
    await _inbound(TYPED[kind])  # captured through the real ladder (state goes back to IDLE)
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.LLM
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

    history = "\n".join(str(m.content) for m in seen[0])
    assert pdata.REDACTED_BODY in history  # non-vacuous
    assert _free(history, kind) and _free(repr(seen[0]), kind)
