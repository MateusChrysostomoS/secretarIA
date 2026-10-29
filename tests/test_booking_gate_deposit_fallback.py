"""A technical failure of the deposit never stops the booking (TASK-071, Plan 3, task 3).

Four places used to answer `BEFORE_BLOCKED_TEXT` ("Não consegui iniciar...") and drop the
booking when the Pix could not be taken: the router's "Confirmar" (`BookingGate`), the payer
answer that could not be kept, the Portal code verified before the Pix, and the Pix that could not
be delivered. All four now book the consultation WITHOUT the deposit and e-mail the clinic; only
`charge_pending` / `no_patient` keep blocking, and a patient who declines the CPF keeps today's
refusal path.
"""

import itertools
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.config import Settings
from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    Conversation,
    FlowState,
    HandoverState,
    Message,
    MessageDirection,
    MessageSender,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.services import booking_hold as holds, email, flow_router as fr
from secretaria.services.payments import (
    accounts,
    booking_charge,
    deposit_failure_notice,
    deposit_lifecycle,
    payer_data as pdata,
    registry,
)
from secretaria.services.payments.deposit_failure_notice import (
    CONTINUE_WITHOUT_DEPOSIT,
    continues_without_deposit,
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
)
from tests.payments_support import FakeArqPool, FakeProvider, FakeRedis, make_sessionmaker

PHONE_NUMBER_ID = "5511000000002"
WA_ID = "5511999999999"
DOCUMENT = "52998224725"  # a well-known valid test number, never a real person's
TZ = ZoneInfo("America/Sao_Paulo")
BLOCKED_WORDS = "Não consegui iniciar"
QUOTE = DepositQuote(
    "required",
    "required",
    amount_cents=6000,
    provider="asaas",
    ttl_minutes=30,
    service_label="Consulta",
)


class RefusingAsaas(FakeProvider):
    """Asaas asks for the CPF of a new customer and then refuses every charge (a technical
    failure: the account is not enabled, say)."""

    refuse = True

    async def create_pix_charge(self, account, request):
        self.calls.append(("create_pix_charge", request))
        if not (request.payer_document or request.provider_customer_id):
            raise ProviderError("payer_document_required", status_code=400)
        if self.refuse:
            raise ProviderError(
                "charge_failed", status_code=400, reason="account_not_enabled", stage="charge"
            )
        return self.created_charge


class Sender:
    persists_outbound = False

    def __init__(self, *, fail: bool = False) -> None:
        self.sent: list[tuple] = []
        self.fail = fail

    async def send_text_message(self, to, body):
        if self.fail:
            raise RuntimeError("meta down")
        self.sent.append(("text", body))
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}

    async def send_buttons(self, to, body, buttons):
        if self.fail:
            raise RuntimeError("meta down")
        self.sent.append(("buttons", body, [label for _id, label in buttons]))
        return {"messages": [{"id": f"wamid.{len(self.sent)}"}]}

    def bodies(self) -> list[str]:
        return [entry[1] for entry in self.sent]


class Calendar:
    tzinfo = TZ

    def __init__(self, conflicts: list[bool] | None = None) -> None:
        self.created: list = []
        self.conflicts = list(conflicts or [])

    async def has_conflict(self, start, end, ignore_event_id=None):
        return self.conflicts.pop(0) if self.conflicts else False

    async def create_event(self, start, end, summary, description="", **kwargs):
        self.created.append(start)
        return {"id": f"evt-{len(self.created)}", "htmlLink": "https://calendar/evt"}

    async def list_free_slots(self, day, slot_minutes=None, max_slots=None, **kwargs):
        later = datetime(2030, 3, 4, 15, 0, tzinfo=TZ)
        return [
            {
                "start": later.isoformat(),
                "end": (later + timedelta(minutes=30)).isoformat(),
                "label": "15:00",
            }
        ]

    async def list_available_days(self, start_day, days, slot_minutes=None, **kwargs):
        return [datetime(2030, 3, 4, tzinfo=TZ)]


class Env:
    def __init__(self) -> None:
        self.provider = RefusingAsaas()
        self.provider.created_charge = new_charge()
        self.provider.state = ChargeState(PaymentStatus.PENDING)
        self.sender = Sender()
        self.calendar = Calendar()
        self.alerts: list[dict] = []
        self.code_outcome = RequestCodeOutcome.NOT_PENDING


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    for module in (workers_ns, holds, booking_charge, deposit_failure_notice):
        monkeypatch.setattr(module, "async_session_factory", maker)
    monkeypatch.setattr(workers_ns, "get_settings", lambda: Settings(BOT_ALLOWLIST_WA_IDS=""))
    yield maker
    await engine.dispose()


@pytest.fixture
def env(monkeypatch) -> Env:
    e = Env()

    async def waba_token(session, tenant_id):
        return "wt"

    async def llm(*args, **kwargs):
        raise AssertionError("a Pix turn must never reach the LLM")

    async def alert(to_email, clinic_name, **kwargs):
        e.alerts.append({"to": to_email, **kwargs})
        return True

    async def request_code(tenant_id, external_id):
        return RequestCodeResult(e.code_outcome, email_masked="m***a@gmail.com")

    async def no_emails(tenant_id):
        return {}

    async def entitlements(tenant_id, redis):
        return entitlement_summary()

    monkeypatch.setattr(workers_ns, "get_entitlements", entitlements)
    monkeypatch.setattr(workers_ns, "get_waba_token", waba_token)
    monkeypatch.setattr(workers_ns, "run_agent", llm)
    monkeypatch.setattr(email, "send_deposit_unavailable_alert", alert)
    monkeypatch.setattr(deposit_failure_notice, "fetch_professional_emails", no_emails)
    monkeypatch.setattr(holds, "request_code", request_code)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", e.provider)
    monkeypatch.setattr(workers_ns, "_reply_sender", lambda reply, tenant, token: e.sender)
    monkeypatch.setattr(workers_ns, "_flow_turn_calendar", lambda *args: e.calendar)

    async def appointment_calendar(*args, **kwargs):
        return e.calendar

    monkeypatch.setattr(workers_ns, "_appointment_calendar", appointment_calendar)
    return e


_WAM = itertools.count(1)


async def _turn(body: str, *, redis, reply_id: str | None = None):
    reply = await tasks._persist_inbound_message(
        phone_number_id=PHONE_NUMBER_ID,
        wa_id=WA_ID,
        patient_name="Maria",
        wam_id=f"wamid.fallback.{next(_WAM)}",
        body=body,
        interactive_reply_id=reply_id,
    )
    assert reply is not None
    await tasks._send_bot_reply_inner(reply, redis=redis)
    return reply


async def _asked_for_the_cpf(db, **world_kw):
    async with db() as session:
        world = await make_world(
            session, phone_number_id=PHONE_NUMBER_ID, is_active=True, **world_kw
        )
        world.patient.lgpd_accepted_at = datetime.now(UTC)
        session.add(
            Message(
                conversation_id=world.conversation.id,
                direction=MessageDirection.OUTBOUND,
                sender=MessageSender.BOT,
                wam_id="wamid.seed",
                body=pdata.ask_text("document", "asaas"),
            )
        )
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.AWAITING_PAYER_DATA
        conversation.flow_step = pdata.step_for("before", "document", 0)
        conversation.flow_selected_type = "Consulta"
        conversation.flow_selected_day = "2030-03-04"
        conversation.flow_selected_slot = "2030-03-04T10:00"
        await session.commit()
    return world


async def _rows(db, model):
    async with db() as session:
        return list(await session.scalars(select(model)))


async def _conversation(db, world) -> Conversation:
    async with db() as session:
        return await session.get(Conversation, world.conversation.id)


async def _account(db, world):
    async with db() as session:
        return await accounts.get_account_row(session, tenant_id=world.tenant_id, provider="asaas")


async def _awaiting_deposits(db):
    return [d for d in await _rows(db, PixDeposit) if d.status == PixDepositStatus.AWAITING]


def _no_blocked_bubble(env) -> bool:
    return not any(BLOCKED_WORDS in body for body in env.sender.bodies())


# ---- the router's "Confirmar" (flow_router + BookingGate._decide_required) ---------------------


async def test_charge_failure_after_cpf_books_the_appointment_without_deposit(db, env):
    world = await _asked_for_the_cpf(db)

    await _turn("529.982.247-25", redis=FakeArqPool())

    (appointment,) = await _rows(db, Appointment)
    assert appointment.status == AppointmentStatus.SCHEDULED
    assert appointment.patient_id == world.patient.id
    assert await _rows(db, PixDeposit) == [] and await _awaiting_deposits(db) == []
    assert await _rows(db, BookingHold) == []
    assert len(env.calendar.created) == 1
    assert _no_blocked_bubble(env)
    assert any("está confirmado" in body for body in env.sender.bodies())
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE


async def test_failure_notifies_clinic_once_and_marks_account_failing(db, env):
    world = await _asked_for_the_cpf(db, contact_email="clinic@example.com")

    await _turn("529.982.247-25", redis=FakeArqPool())

    (alert,) = env.alerts
    assert alert["to"] == "clinic@example.com"
    assert alert["patient_name"] == "Maria" and alert["service"] == "Consulta"
    assert DOCUMENT not in repr(alert)  # never the CPF
    account = await _account(db, world)
    assert accounts.charge_health(account) == "failing"
    assert account.last_charge_failure_reason == "account_not_enabled"


async def _confirm(world, env, *, armed=False, redis=None):
    gate = holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,
        external_id=world.patient.external_id or "",
        armed=armed,
        redis=redis or FakeRedis(),
    )
    snapshot = type(
        "Snap",
        (),
        {
            "id": world.conversation.id,
            "flow_state": FlowState.SERVICE_CATALOG,
            "flow_step": fr.STEP_AWAITING_CONFIRMATION,
            "flow_selected_type": "Consulta",
            "flow_selected_day": "2030-03-04",
            "flow_selected_slot": "2030-03-04T10:00",
            "flow_selected_professional_id": None,
            "flow_selected_insurance": None,
            "flow_managing_appointment_id": None,
            "flow_attendee_name": None,
        },
    )()
    return await fr._handle_confirmation(
        snapshot, world.tenant, env.calendar, fr.LABEL_CONFIRM, "Maria", gate=gate
    )


async def _remembered(world) -> FakeRedis:
    redis = FakeRedis()
    assert await pdata.remember(
        redis,
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        kind="document",
        value=DOCUMENT,
    )
    return redis


async def test_charge_pending_still_blocks(db, env, monkeypatch):
    async def pending(**kwargs):
        raise booking_charge.ChargeStartError("charge_pending")

    monkeypatch.setattr(booking_charge, "begin_payment", pending)
    async with db() as session:
        world = await make_world(session, contact_email="clinic@example.com")

    result = await _confirm(world, env)

    assert result.action == "handover"
    assert [b.body for b in result.bubbles] == [deposit_lifecycle.BEFORE_BLOCKED_TEXT]
    assert result.appointment is None and env.calendar.created == []
    assert env.alerts == []  # not a technical failure: nothing to tell the clinic


async def test_a_waived_commit_carries_the_reason_and_creates_the_event(db, env):
    async with db() as session:
        world = await make_world(session)
    result = await _confirm(world, env, redis=await _remembered(world))
    assert result.appointment is not None and result.action == "reply"
    assert len(env.calendar.created) == 1
    assert "está confirmado" in result.bubbles[0].body


async def test_the_gate_decision_names_the_waived_reason(db, env):
    async with db() as session:
        world = await make_world(session)
    gate = holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,
        external_id="",
        armed=False,
        redis=await _remembered(world),
    )
    decision = await gate.decide(
        start_at=SLOT_START,
        end_at=SLOT_END,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
    )
    assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "account_not_enabled")


async def test_slot_taken_during_fallback_offers_other_times(db, env):
    # Free at "Confirmar"; taken by the time the failed charge came back.
    env.calendar.conflicts = [False, True]
    async with db() as session:
        world = await make_world(session)

    result = await _confirm(world, env, redis=await _remembered(world))

    assert env.calendar.created == [] and result.appointment is None
    assert result.bubbles[0].body == fr.SLOT_TAKEN_AT_CONFIRM_MESSAGE
    assert BLOCKED_WORDS not in " ".join(getattr(b, "body", "") for b in result.bubbles)


async def test_a_quote_that_cannot_reach_the_provider_books_and_tells_the_clinic(db, env):
    """Ruling R6: no usable credential is technical too; `no_patient` keeps blocking."""
    async with db() as session:
        world = await make_world(session, with_account=False, contact_email="c@example.com")
    result = await _confirm(world, env)
    assert result.appointment is not None and len(env.calendar.created) == 1
    (alert,) = env.alerts
    assert alert["reason_text"]  # a closed-vocabulary sentence, never provider text


@pytest.mark.parametrize("code", sorted(CONTINUE_WITHOUT_DEPOSIT))
def test_every_technical_code_continues(code):
    assert continues_without_deposit(code) is True


@pytest.mark.parametrize("code", ["charge_pending", "no_patient"])
def test_the_non_technical_codes_keep_blocking(code):
    assert continues_without_deposit(code, "unknown") is False


# ---- worker path 1: the payer answer that could not be kept (`_payer_data_unavailable`) -------


async def test_payer_data_unavailable_books_without_deposit_instead_of_blocking(db, env):
    class Down(FakeArqPool):
        async def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    world = await _asked_for_the_cpf(db, contact_email="clinic@example.com")

    await _turn("529.982.247-25", redis=Down())

    assert _no_blocked_bubble(env)
    (appointment,) = await _rows(db, Appointment)
    assert appointment.status == AppointmentStatus.SCHEDULED
    assert env.provider.called("create_pix_charge") == 0  # the deposit step was dropped
    # Fix round 1: Redis, not the charge, failed - no e-mail, account health untouched.
    assert env.alerts == []
    assert accounts.charge_health(await _account(db, world)) != "failing"
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE


async def test_patient_declining_cpf_does_not_trigger_fallback(db, env):
    world = await _asked_for_the_cpf(db, contact_email="clinic@example.com")

    await _turn(pdata.LABEL_OPTOUT, redis=FakeArqPool(), reply_id=pdata.OPTOUT_REPLY_ID)

    assert env.sender.bodies() == [pdata.optout_text("before", "document")]
    assert await _rows(db, Appointment) == [] and env.calendar.created == []
    assert env.alerts == [] and env.provider.called("create_pix_charge") == 0
    conversation = await _conversation(db, world)
    assert conversation.flow_state == FlowState.IDLE
    assert conversation.handover_state == HandoverState.HUMAN_ACTIVE


# ---- worker path 2: the Portal code verified before the Pix (`_payment_after_code`) ----------


async def _portal_code_hold(db, **world_kw):
    async with db() as session:
        world = await make_world(session, channel="brain_message", **world_kw)
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
    return world, tenant, reply


async def test_payment_after_code_failure_keeps_a_promotable_hold_and_tells_the_clinic(db, env):
    world, tenant, reply = await _portal_code_hold(db, contact_email="clinic@example.com")

    result = await tasks._payment_after_code(reply, tenant=tenant, redis=await _remembered(world))

    assert result is None  # the caller promotes it like any booking with no deposit due
    (hold,) = await _rows(db, BookingHold)
    assert (hold.phase, hold.charge_id) == ("code", None)
    assert await _rows(db, PixDeposit) == []
    assert len(env.alerts) == 1


async def test_payment_after_code_promotion_confirms_and_never_says_blocked(db, env):
    world, tenant, reply = await _portal_code_hold(db)
    assert (
        await tasks._payment_after_code(reply, tenant=tenant, redis=await _remembered(world))
        is None
    )

    text = await tasks._promote_booking_hold(reply, tenant=tenant, waba_token=None, redis=None)

    assert "está confirmado" in text and BLOCKED_WORDS not in text
    (appointment,) = await _rows(db, Appointment)
    assert appointment.status == AppointmentStatus.SCHEDULED


async def test_payment_after_code_quote_failure_promotes_without_deposit(db, env):
    world, tenant, reply = await _portal_code_hold(
        db, with_account=False, contact_email="clinic@example.com"
    )
    assert await tasks._payment_after_code(reply, tenant=tenant, redis=FakeRedis()) is None
    (hold,) = await _rows(db, BookingHold)
    assert hold.phase == "code"
    assert len(env.alerts) == 1


async def test_payment_after_code_slot_taken_meanwhile_offers_the_retry(db, env, monkeypatch):
    world, tenant, reply = await _portal_code_hold(db)

    async def taken(held):
        return None

    from secretaria.workers.shared import deposit_fallback

    monkeypatch.setattr(deposit_fallback, "_hold_again", taken)
    result = await tasks._payment_after_code(reply, tenant=tenant, redis=await _remembered(world))
    assert result.flow_state is FlowState.SERVICE_CATALOG
    assert result.flow_step == fr.STEP_AWAITING_RETRY
    assert BLOCKED_WORDS not in result.bubbles[0].body
    assert env.alerts == []  # nothing was booked


# ---- worker path 3: the Pix that could not be delivered (`_abort_deposit_request`) -----------


async def _undeliverable_pix(db, env):
    """A Pix created fine (the account is marked healthy) whose message could not be delivered."""
    env.provider.refuse = False
    async with db() as session:
        world = await make_world(session, contact_email="clinic@example.com")
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
        await session.commit()
        tenant = await session.get(type(world.tenant), world.tenant_id)
    reply = tasks._ReplyContext(
        conversation_id=world.conversation.id,
        patient_ref=world.patient.wa_id,
        inbound_body=fr.LABEL_CONFIRM,
        channel="whatsapp",
        tenant_id=world.tenant_id,
    )

    async def abort():
        return await tasks._abort_deposit_request(
            reply,
            {"deposit_id": started.deposit_id},
            tenant=tenant,
            waba_token="wt",
            redis=FakeArqPool(),
        )

    return world, abort


async def test_abort_deposit_request_books_without_deposit_and_voids_the_pix(db, env):
    world, abort = await _undeliverable_pix(db, env)

    result = await abort()

    assert result is not None and result.action == "reply"
    assert "está confirmado" in result.bubbles[0].body
    assert all(BLOCKED_WORDS not in getattr(b, "body", "") for b in result.bubbles)
    (appointment,) = await _rows(db, Appointment)
    assert appointment.status == AppointmentStatus.SCHEDULED
    assert appointment.phone == world.patient.wa_id  # a WhatsApp patient keeps their number
    assert await _awaiting_deposits(db) == []  # the Pix is voided: no deadline can cancel it
    assert env.provider.called("cancel_charge") == 1
    assert await _rows(db, BookingHold) == []
    # Fix round 1: the charge worked, only the delivery failed - no "Sinal do Pix" e-mail and
    # the account stays healthy (`record_charge_success` from the charge is not overwritten).
    assert env.alerts == []
    assert accounts.charge_health(await _account(db, world)) == "ok"


@pytest.mark.parametrize("broken", ["cancel_charge", "get_charge"])
async def test_abort_deposit_request_never_books_over_a_pix_whose_void_was_not_confirmed(
    db, env, broken
):
    """Fix round 1: `abandon_charge` answers "voided" even when the provider could not be asked
    (`get_charge` raised) or refused the void (`cancel_charge` raised). The Pix may still be
    payable, so nothing is booked: a person takes over."""
    world, abort = await _undeliverable_pix(db, env)
    if broken == "cancel_charge":
        env.provider.cancel_error = RuntimeError("asaas down")
    else:
        env.provider.get_error = RuntimeError("asaas down")

    result = await abort()

    assert result is not None and result.action == "handover"
    assert [b.body for b in result.bubbles] == [deposit_lifecycle.BEFORE_CONFIRM_FAILED_TEXT]
    assert await _rows(db, Appointment) == []  # never a booking next to a payable Pix
    assert env.calendar.created == []
    # The row is still waiting (the expiry job voids it) - and nothing confirmed stands beside it.
    assert len(await _awaiting_deposits(db)) == 1
    assert env.alerts == []


async def test_next_successful_charge_clears_failing_health(db, env):
    env.provider.refuse = False
    async with db() as session:
        world = await make_world(session)
        await accounts.record_charge_failure(
            session,
            tenant_id=world.tenant_id,
            provider="asaas",
            reason="provider_unavailable",
            stage="charge",
        )
        await session.commit()
    assert accounts.charge_health(await _account(db, world)) == "failing"

    await booking_charge.begin_payment(
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

    assert accounts.charge_health(await _account(db, world)) == "ok"
