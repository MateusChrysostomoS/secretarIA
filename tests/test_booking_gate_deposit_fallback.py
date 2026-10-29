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
from uuid import uuid4
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
    deposit_gate,
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
from secretaria.workers.shared.deposit_fallback import DepositWaived, _promote_waived
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
        self.cancelled: list = []
        self.cancel_error: Exception | None = None
        self.conflicts = list(conflicts or [])

    async def cancel_event(self, event_id):
        if self.cancel_error is not None:
            raise self.cancel_error
        self.cancelled.append(event_id)
        return True

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

    assert env.sender.bodies() == [
        pdata.optout_text("before", "document"),
        fr.HUMAN_OFFER_BOOKING_BODY,
    ]
    assert await _rows(db, Appointment) == [] and env.calendar.created == []
    assert env.alerts == [] and env.provider.called("create_pix_charge") == 0
    conversation = await _conversation(db, world)
    # Owner, 2026-10-29: the patient chooses (Sim / Não); nobody takes over on their own.
    assert (conversation.flow_state, conversation.flow_step) == (FlowState.LLM, fr.STEP_HUMAN_OFFER)
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE


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

    # The caller promotes it like any booking with no deposit due (task 6: with the reason).
    assert result == DepositWaived("account_not_enabled")
    (hold,) = await _rows(db, BookingHold)
    assert (hold.phase, hold.charge_id) == ("code", None)
    assert await _rows(db, PixDeposit) == []
    # Review fix: the "booked without deposit" e-mail waits for the booking to exist.
    assert env.alerts == []
    text = await _promote_waived(
        reply, tenant=tenant, waba_token=None, redis=FakeRedis(), waived=result
    )
    assert "está confirmado" in text
    assert len(env.alerts) == 1


async def test_payment_after_code_promotion_confirms_and_never_says_blocked(db, env):
    world, tenant, reply = await _portal_code_hold(db)
    assert await tasks._payment_after_code(
        reply, tenant=tenant, redis=await _remembered(world)
    ) == DepositWaived("account_not_enabled")

    text = await tasks._promote_booking_hold(reply, tenant=tenant, waba_token=None, redis=None)

    assert "está confirmado" in text and BLOCKED_WORDS not in text
    (appointment,) = await _rows(db, Appointment)
    assert appointment.status == AppointmentStatus.SCHEDULED


async def test_payment_after_code_quote_failure_promotes_without_deposit(db, env):
    world, tenant, reply = await _portal_code_hold(
        db, with_account=False, contact_email="clinic@example.com"
    )
    waived = await tasks._payment_after_code(reply, tenant=tenant, redis=FakeRedis())
    assert waived == DepositWaived("no_account")
    (hold,) = await _rows(db, BookingHold)
    assert hold.phase == "code"
    assert env.alerts == []
    await _promote_waived(reply, tenant=tenant, waba_token=None, redis=FakeRedis(), waived=waived)
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
    payable, so nothing is booked: the patient is offered a person (owner, 2026-10-29)."""
    world, abort = await _undeliverable_pix(db, env)
    if broken == "cancel_charge":
        env.provider.cancel_error = RuntimeError("asaas down")
    else:
        env.provider.get_error = RuntimeError("asaas down")

    result = await abort()

    _assert_human_offer(result)
    assert await _rows(db, Appointment) == []  # never a booking next to a payable Pix
    assert env.calendar.created == []
    # The row is still waiting (the expiry job voids it) - and nothing confirmed stands beside it.
    assert len(await _awaiting_deposits(db)) == 1
    assert env.alerts == []


def _assert_human_offer(result) -> None:
    """The booking could not be written: say so, then the fixed Sim / Não card (owner,
    2026-10-29) - never an automatic hand-over."""
    assert result is not None and result.action == "reply"
    assert [b.body for b in result.bubbles] == [
        deposit_lifecycle.BEFORE_CONFIRM_FAILED_TEXT,
        fr.HUMAN_OFFER_BOOKING_BODY,
    ]
    assert result.bubbles[1].labels == [fr.LABEL_YES, fr.LABEL_NO]
    assert (result.flow_state, result.flow_step) == (FlowState.LLM, fr.STEP_HUMAN_OFFER)
    assert "equipe" not in deposit_lifecycle.BEFORE_CONFIRM_FAILED_TEXT


async def test_abort_deposit_request_offers_a_human_when_the_booking_cannot_be_written(
    db, env, monkeypatch
):
    """The Pix was voided but the agenda cannot be reached: nothing is booked, the clinic is not
    told a booking exists, and the chat is NOT handed over before the patient chooses."""
    from secretaria.workers.shared import booking_hold as worker_holds

    world, abort = await _undeliverable_pix(db, env)

    async def no_calendar(*args, **kwargs):
        return None

    monkeypatch.setattr(worker_holds, "_appointment_calendar", no_calendar)

    result = await abort()

    _assert_human_offer(result)
    assert await _rows(db, Appointment) == []
    conversation = await _conversation(db, world)
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE


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


# ---- task 6: the agenda mark of a booking made WITHOUT the deposit (rulings R5, R13) ----------
#
# Every path above that books without the deposit stores the closed `FailureReason` on the
# appointment (`deposit_waived_reason`); the agenda shows "Sem sinal (falha técnica)" from it.
# Paid and ordinary bookings never carry one.


async def test_fallback_booking_records_waived_reason(db, env):
    """Router path: the gate's fallback after `begin_payment` failed, persisted by the runner."""
    await _asked_for_the_cpf(db)

    await _turn("529.982.247-25", redis=FakeArqPool())

    (appointment,) = await _rows(db, Appointment)
    assert appointment.deposit_waived_reason == "account_not_enabled"


async def test_quote_level_waiver_records_waived_reason(db, env):
    """Router path, ruling R6: the deposit could not even be quoted (no usable account)."""
    async with db() as session:
        world = await make_world(session, with_account=False)

    result = await _confirm(world, env)

    assert result.appointment is not None
    assert result.appointment["deposit_waived_reason"] == "no_account"


@pytest.mark.parametrize("armed", [False, True])
async def test_quote_level_waiver_never_books_next_to_a_live_charge(db, env, armed):
    """Ruling R13: like the breaker, the R6 waiver never commits beside a payable Pix of the same
    conversation - `charge_pending` keeps blocking (Portal and WhatsApp)."""
    from uuid import uuid4

    env.code_outcome = RequestCodeOutcome.SENT
    async with db() as session:
        world = await make_world(
            session,
            channel="brain_message" if armed else "whatsapp",
            with_account=False,
            contact_email="clinic@example.com",
        )
    assert await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=world.patient.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START + timedelta(hours=2),
        end_at=SLOT_END + timedelta(hours=2),
        phase="payment",
        charge_id=uuid4(),
    )

    result = await _confirm(world, env, armed=armed)

    assert result.action == "handover"
    assert [b.body for b in result.bubbles] == [deposit_lifecycle.BEFORE_BLOCKED_TEXT]
    assert result.appointment is None and env.calendar.created == []
    assert env.alerts == []  # not a technical failure: nothing to tell the clinic


async def test_payer_data_unavailable_records_waived_reason(db, env):
    """Worker path 1: the payer answer could not be kept, the turn books without the deposit."""

    class Down(FakeArqPool):
        async def set(self, *args, **kwargs):
            raise ConnectionError("redis down")

    await _asked_for_the_cpf(db)

    await _turn("529.982.247-25", redis=Down())

    (appointment,) = await _rows(db, Appointment)
    assert appointment.deposit_waived_reason == "unknown"


async def test_payment_after_code_fallback_records_waived_reason(db, env):
    """Worker path 2: the Portal code was verified and the Pix could not be started. The result
    carries the reason to the promotion, which stores it on the appointment."""
    world, tenant, reply = await _portal_code_hold(db)

    result = await tasks._payment_after_code(reply, tenant=tenant, redis=await _remembered(world))

    assert result == DepositWaived("account_not_enabled")
    text = await tasks._promote_booking_hold(
        reply,
        tenant=tenant,
        waba_token=None,
        redis=None,
        deposit_waived_reason=result.reason,
    )
    assert "está confirmado" in text
    (appointment,) = await _rows(db, Appointment)
    assert appointment.deposit_waived_reason == "account_not_enabled"


async def test_payment_after_code_quote_failure_names_the_reason(db, env):
    world, tenant, reply = await _portal_code_hold(db, with_account=False)

    result = await tasks._payment_after_code(reply, tenant=tenant, redis=FakeRedis())

    assert result == DepositWaived("no_account")


async def test_verified_code_turn_promotes_with_the_waived_reason(db, env, monkeypatch):
    """The orchestrator hands the reason `_payment_after_code` returned to the promotion."""
    from secretaria.services.pending_identity import VerifyOutcome, VerifyResult
    world, tenant, reply = await _portal_code_hold(db)
    seen: dict = {}

    async def waived(reply, *, tenant, redis):
        return DepositWaived("pix_key_missing")

    async def promote(reply, *, tenant, waba_token, redis, deposit_waived_reason=None, **kw):
        seen["reason"] = deposit_waived_reason
        seen["hand_over"] = kw.get("hand_over", True)
        return None

    async def verified(tenant_id, external_id, code):
        return VerifyResult(VerifyOutcome.VERIFIED)

    monkeypatch.setattr(workers_ns, "_payment_after_code", waived)
    monkeypatch.setattr(workers_ns, "_promote_booking_hold", promote)
    monkeypatch.setattr(workers_ns, "verify_code", verified)
    async with db() as session:
        conversation = await session.get(Conversation, world.conversation.id)
        conversation.flow_state = FlowState.AWAITING_EMAIL_CODE
        patient = await session.get(type(world.patient), world.patient.id)
        patient.lgpd_accepted_at = datetime.now(UTC)
        await session.commit()

    inbound = await tasks._persist_brain_message_inbound(
        tenant_id=world.tenant_id,
        external_id=world.patient.external_id,
        text="123456",
        patient_name="Maria",
    )
    await tasks._send_bot_reply(inbound, redis=None)

    assert seen == {"reason": "pix_key_missing", "hand_over": False}


async def test_abort_deposit_request_records_waived_reason(db, env):
    """Worker path 3: the Pix was created but could not be delivered."""
    world, abort = await _undeliverable_pix(db, env)

    await abort()

    (appointment,) = await _rows(db, Appointment)
    assert appointment.deposit_waived_reason == "unknown"


async def test_paid_and_normal_bookings_have_no_waived_reason(db, env):
    from secretaria.services import booking_promotion

    # Normal: a clinic that charges no deposit up front books exactly as before.
    async with db() as session:
        world = await make_world(session, timing="after")
    result = await _confirm(world, env)
    assert result.appointment is not None
    assert "deposit_waived_reason" not in result.appointment

    # Normal Portal booking: the code hold promoted with no deposit due.
    world, tenant, reply = await _portal_code_hold(db, timing="after")
    assert "está confirmado" in await tasks._promote_booking_hold(
        reply, tenant=tenant, waba_token=None, redis=None
    )
    (appointment,) = await _rows(db, Appointment)
    assert appointment.deposit_waived_reason is None

    # Paid: the promotion of a paid deposit never marks the booking.
    env.provider.refuse = False
    async with db() as session:
        world = await make_world(session)
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
        deposit = await session.get(PixDeposit, started.deposit_id)
        tenant = await session.get(type(world.tenant), world.tenant_id)
        snapshot = booking_promotion.BookingSnapshot.from_deposit(deposit)
        promoted = await booking_promotion.promote(session, tenant, deposit, snapshot, env.calendar)
    assert promoted.appointment.deposit_waived_reason is None


# ---- final review, finding 2: the quote itself raising after the Portal code ------------------


async def test_payment_after_code_quote_error_promotes_without_deposit(db, env, monkeypatch):
    """A DB error while quoting is `quote_failed` (as in the gate): book without the deposit,
    reason `unknown`, the clinic is told; never a crashed turn."""
    world, tenant, reply = await _portal_code_hold(db, contact_email="clinic@example.com")

    async def broken_quote(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(deposit_gate, "quote_deposit", broken_quote)
    monkeypatch.setattr(deposit_gate, "QUOTE_RETRY_DELAYS", (0.0, 0.0))

    result = await tasks._payment_after_code(reply, tenant=tenant, redis=FakeRedis())

    assert result == DepositWaived("unknown")
    (hold,) = await _rows(db, BookingHold)
    assert (hold.phase, hold.charge_id) == ("code", None)
    assert env.alerts == []  # sent once the booking exists
    await _promote_waived(reply, tenant=tenant, waba_token=None, redis=FakeRedis(), waived=result)
    assert len(env.alerts) == 1
    assert env.provider.called("create_pix_charge") == 0


async def test_payment_after_code_retries_a_transient_quote_error(db, env, monkeypatch):
    """Owner, 2026-10-29: the calculation is retried (fresh session each time) before it falls
    back to `quote_failed`; a transient error followed by success yields the real quote."""
    world, tenant, reply = await _portal_code_hold(
        db, with_account=False, contact_email="clinic@example.com"
    )
    real = deposit_gate.quote_deposit
    sessions: list = []

    async def flaky(session, *args, **kwargs):
        sessions.append(session)
        if len(sessions) == 1:
            raise RuntimeError("database went away")
        return await real(session, *args, **kwargs)

    async def no_sleep(seconds):
        return None

    monkeypatch.setattr(deposit_gate, "quote_deposit", flaky)
    monkeypatch.setattr(deposit_gate, "_retry_sleep", no_sleep)

    result = await tasks._payment_after_code(reply, tenant=tenant, redis=FakeRedis())

    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    # The REAL quote (no account -> `no_account`), not the `quote_failed` -> `unknown` fallback.
    assert result == DepositWaived("no_account")


async def test_payment_after_code_quote_error_beside_a_live_charge_hands_over(
    db, env, monkeypatch
):
    """The same live-charge safety as the gate (ruling R13): a dropped deposit never books
    beside a payable Pix of this conversation."""
    world, tenant, reply = await _portal_code_hold(db, contact_email="clinic@example.com")
    async with db() as session:
        session.add(
            BookingHold(
                tenant_id=world.tenant_id,
                conversation_id=world.conversation.id,
                start_at=SLOT_START + timedelta(days=1),
                end_at=SLOT_END + timedelta(days=1),
                expires_at=datetime.now(UTC) + timedelta(minutes=20),
                phase="payment",
                charge_id=uuid4(),
                created_at=datetime.now(UTC) - timedelta(hours=1),  # not the latest hold
            )
        )
        await session.commit()

    async def broken_quote(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(deposit_gate, "quote_deposit", broken_quote)
    monkeypatch.setattr(deposit_gate, "QUOTE_RETRY_DELAYS", (0.0, 0.0))

    result = await tasks._payment_after_code(reply, tenant=tenant, redis=FakeRedis())

    assert isinstance(result, fr.FlowRouterResult) and result.action == "handover"
    assert env.alerts == []
    assert await _rows(db, Appointment) == []


# ---- final review, finding 3: the phone read never orphans a calendar event -------------------


async def test_promotion_survives_a_failed_phone_read(db, env, monkeypatch):
    """`appointments.phone` is read BEFORE the event is written, and a failed read is None: an
    event never exists without its appointment because of it."""
    world, tenant, reply = await _portal_code_hold(db)

    async def broken_phone(patient_id):
        raise RuntimeError("database went away")

    from secretaria.workers.shared import booking_hold as worker_holds

    monkeypatch.setattr(worker_holds, "_patient_phone", broken_phone)

    text = await tasks._promote_booking_hold(reply, tenant=tenant, waba_token=None, redis=None)

    assert "está confirmado" in text
    (appointment,) = await _rows(db, Appointment)
    assert appointment.phone is None
    assert len(env.calendar.created) == 1


# ---- review fix (2026-10-29): a waived booking that cannot be written never hands over ----------


async def _verified_code_turn(db, world, redis):
    from secretaria.services.pending_identity import VerifyOutcome, VerifyResult

    async def verified(tenant_id, external_id, code):
        return VerifyResult(VerifyOutcome.VERIFIED)

    import secretaria.workers.orchestrator as orchestrator

    orchestrator_verify = orchestrator.verify_code
    orchestrator.verify_code = verified
    try:
        async with db() as session:
            conversation = await session.get(Conversation, world.conversation.id)
            conversation.flow_state = FlowState.AWAITING_EMAIL_CODE
            patient = await session.get(type(world.patient), world.patient.id)
            patient.lgpd_accepted_at = datetime.now(UTC)
            await session.commit()
        inbound = await tasks._persist_brain_message_inbound(
            tenant_id=world.tenant_id,
            external_id=world.patient.external_id,
            text="123456",
            patient_name="Maria",
        )
        await tasks._send_bot_reply(inbound, redis=redis)
    finally:
        orchestrator.verify_code = orchestrator_verify


async def test_portal_waived_booking_that_cannot_be_written_offers_a_person(
    db, env, monkeypatch
):
    """Portal, `before`: valid code, the Pix fails (deposit dropped), then the agenda is gone. The
    patient reads the code line, the explanation and the Sim / Não card - no automatic hand-over,
    no "booked without deposit" e-mail, the hold released, the charge failure still recorded."""
    from secretaria.services.pending_identity import CODE_ACCEPTED_MESSAGE

    world, tenant, reply = await _portal_code_hold(db, contact_email="clinic@example.com")

    async def no_calendar(*args, **kwargs):
        return None

    monkeypatch.setattr(workers_ns, "_appointment_calendar", no_calendar)

    await _verified_code_turn(db, world, await _remembered(world))

    assert env.sender.bodies() == [
        CODE_ACCEPTED_MESSAGE,
        deposit_lifecycle.BEFORE_CONFIRM_FAILED_TEXT,
        fr.HUMAN_OFFER_BOOKING_BODY,
    ]
    conversation = await _conversation(db, world)
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE
    assert (conversation.flow_state, conversation.flow_step) == (FlowState.LLM, fr.STEP_HUMAN_OFFER)
    assert await _rows(db, Appointment) == []
    assert await _rows(db, BookingHold) == []  # the slot is free for others
    assert env.alerts == []  # no "booked without deposit" for a booking that does not exist
    assert accounts.charge_health(await _account(db, world)) == "failing"  # still recorded


async def _portal_persist_failure(db, env, monkeypatch):
    world, tenant, reply = await _portal_code_hold(db, contact_email="clinic@example.com")

    async def broken_plan_ids(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(workers_ns, "resolve_booking_plan_ids", broken_plan_ids)
    alerts: list[dict] = []

    async def handoff_alert(**kwargs):
        alerts.append(kwargs)
        return 1

    monkeypatch.setattr(workers_ns, "notify_human_handoff", handoff_alert)
    waived = await tasks._payment_after_code(reply, tenant=tenant, redis=await _remembered(world))
    assert waived == DepositWaived("account_not_enabled")
    return world, tenant, reply, waived, alerts


async def test_an_event_left_without_its_booking_is_removed_again(db, env, monkeypatch):
    world, tenant, reply, waived, alerts = await _portal_persist_failure(db, env, monkeypatch)

    result = await _promote_waived(
        reply, tenant=tenant, waba_token=None, redis=FakeRedis(), waived=waived
    )

    assert isinstance(result, fr.FlowRouterResult) and result.flow_step == fr.STEP_HUMAN_OFFER
    assert env.calendar.created and env.calendar.cancelled == ["evt-1"]
    assert alerts == []  # removed: nobody needs to clean it up
    assert await _rows(db, Appointment) == [] and await _rows(db, BookingHold) == []
    assert env.alerts == []
    conversation = await _conversation(db, world)
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE


async def test_an_event_that_cannot_be_removed_alerts_the_clinic_without_handing_over(
    db, env, monkeypatch
):
    world, tenant, reply, waived, alerts = await _portal_persist_failure(db, env, monkeypatch)
    env.calendar.cancel_error = RuntimeError("google down")

    result = await _promote_waived(
        reply, tenant=tenant, waba_token=None, redis=FakeRedis(), waived=waived
    )

    assert isinstance(result, fr.FlowRouterResult)
    (alert,) = alerts
    assert alert["conversation_id"] == world.conversation.id
    conversation = await _conversation(db, world)
    assert conversation.handover_state != HandoverState.HUMAN_ACTIVE
