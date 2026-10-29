"""`BookingGate.decide` with a deposit (services/booking_hold.py, spec sections 4.2, 5.2, 7, 11).

WhatsApp goes straight to the Pix; the Portal proves the address FIRST (decision D12) and the
payment starts on the same hold once the code is verified; a clinic on `after` (every clinic
today) leaves the gate exactly as it was; a deposit that cannot be taken for a TECHNICAL reason
is dropped and the booking commits (TASK-071 Plan 3); nothing is left held.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import BookingHold, PixDeposit
from secretaria.services import booking_hold as holds, email
from secretaria.services.payments import (
    booking_charge,
    deposit_failure_notice,
    deposit_gate,
    registry,
)
from secretaria.services.pending_identity import RequestCodeOutcome, RequestCodeResult
from tests.before_support import (
    SLOT_END,
    SLOT_START,
    entitlement_summary,
    make_world,
    new_charge,
)
from tests.payments_support import FakeProvider, FakeRedis, make_sessionmaker


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(holds, "async_session_factory", maker)
    monkeypatch.setattr(booking_charge, "async_session_factory", maker)
    yield maker
    await engine.dispose()


class Env:
    def __init__(self) -> None:
        self.code_requests: list[str] = []
        self.code_outcome = RequestCodeOutcome.SENT
        self.emails: list[tuple[str, str]] = []
        self.entitlement = entitlement_summary()
        self.provider = FakeProvider()
        self.provider.created_charge = new_charge()
        self.waived: list[dict] = []  # TASK-071 Plan 3: deposits dropped, clinic told


@pytest.fixture
def env(monkeypatch) -> Env:
    e = Env()

    async def request_code(tenant_id, external_id):
        e.code_requests.append(external_id)
        return RequestCodeResult(e.code_outcome, email_masked="m***a@gmail.com")

    async def entitlements(tenant_id, redis):
        return e.entitlement

    async def send_email(to_email, clinic_name, kind):
        e.emails.append((to_email, kind))

    async def notify(**kwargs):
        e.waived.append(kwargs)

    monkeypatch.setattr(holds, "request_code", request_code)
    monkeypatch.setattr(email, "send_payment_alert", send_email)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", e.provider)
    monkeypatch.setattr(deposit_failure_notice, "notify_deposit_failure", notify)
    return e


def _gate(world, *, armed: bool, redis=None) -> holds.BookingGate:
    return holds.BookingGate(
        tenant_id=world.tenant_id,
        conversation_id=world.conversation.id,
        patient_id=None,  # the worker builds the gate without it; the gate reads it itself
        external_id=world.patient.external_id or "",
        armed=armed,
        redis=redis,
    )


async def _decide(gate, *, service="Consulta"):
    return await gate.decide(
        start_at=SLOT_START,
        end_at=SLOT_END,
        professional_id=None,
        appointment_type=service,
        insurance=None,
    )


async def _holds(db):
    async with db() as session:
        return list(await session.scalars(select(BookingHold)))


async def _deposits(db):
    async with db() as session:
        return list(await session.scalars(select(PixDeposit)))


async def test_whatsapp_goes_straight_to_the_pix_without_asking_for_a_code(db, env):
    async with db() as session:
        world = await make_world(session)
    decision = await _decide(_gate(world, armed=False))
    assert decision.outcome == "awaiting_payment"
    assert decision.payment.amount_cents == 6000
    assert decision.payment.copy_paste == "PIXCOPIAECOLA"
    assert env.code_requests == []
    (hold,) = await _holds(db)
    assert hold.phase == "payment" and hold.charge_id == decision.payment.deposit_id


async def test_the_portal_asks_for_the_code_first_on_a_ten_minute_code_hold(db, env):
    async with db() as session:
        world = await make_world(session, channel="brain_message")
    decision = await _decide(_gate(world, armed=True))
    assert decision.outcome == "held"
    assert decision.email_masked == "m***a@gmail.com"
    assert env.code_requests == [world.patient.external_id]
    assert await _deposits(db) == []  # nobody is charged before their address is proven
    (hold,) = await _holds(db)
    assert hold.phase == "code" and hold.charge_id is None


@pytest.mark.parametrize(
    "outcome",
    [RequestCodeOutcome.NOT_PENDING, RequestCodeOutcome.NO_EMAIL, RequestCodeOutcome.UNAVAILABLE],
)
async def test_a_portal_visitor_who_owes_no_code_goes_to_the_pix_on_the_same_hold(
    db, env, outcome
):
    env.code_outcome = outcome
    async with db() as session:
        world = await make_world(session, channel="brain_message")
    decision = await _decide(_gate(world, armed=True))
    assert decision.outcome == "awaiting_payment"
    (hold,) = await _holds(db)  # ONE hold: extended, not replaced
    assert hold.id == decision.hold_id and hold.phase == "payment"


async def test_a_clinic_on_after_leaves_the_gate_exactly_as_it_was(db, env):
    async with db() as session:
        whatsapp = await make_world(session, timing="after")
        portal = await make_world(session, timing="after", channel="brain_message")
    assert (await _decide(_gate(whatsapp, armed=False))).outcome == "commit"
    assert (await _decide(_gate(portal, armed=True))).outcome == "held"
    assert await _deposits(db) == []
    assert env.provider.called("create_pix_charge") == 0


async def test_a_service_without_a_price_commits_even_on_before(db, env):
    async with db() as session:
        world = await make_world(session, price="")
    assert (await _decide(_gate(world, armed=False))).outcome == "commit"
    assert await _holds(db) == [] and await _deposits(db) == []


async def test_a_clinic_without_the_old_addon_still_asks_for_the_deposit(db, env):
    # Owner's decision 2026-10-28: the deposit is core SecretarIA, not an add-on.
    env.entitlement = entitlement_summary(addon=False)
    async with db() as session:
        world = await make_world(session)
    assert (await _decide(_gate(world, armed=False))).outcome == "awaiting_payment"


async def test_a_missing_credential_books_without_the_deposit_and_holds_nothing(db, env):
    """TASK-071 Plan 3, ruling R6 (was: "...blocks the booking"): a technical failure."""
    async with db() as session:
        world = await make_world(session, with_account=False, contact_email="clinic@example.com")
    decision = await _decide(_gate(world, armed=False, redis=FakeRedis()))
    assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "no_account")
    assert await _holds(db) == [] and await _deposits(db) == []
    assert env.emails == []  # the old payment alert stays unused
    assert [(w["reason"], w["stage"]) for w in env.waived] == [("no_account", "quote")]


async def test_a_provider_failure_books_without_the_deposit_and_leaves_no_hold(db, env):
    """TASK-071 Plan 3 (was: "...blocks"): the booking commits without the deposit."""
    from secretaria.services.payments.provider import ProviderError

    env.provider.create_error = ProviderError("http_500", status_code=500, retryable=True)
    async with db() as session:
        world = await make_world(session, contact_email="clinic@example.com")
    decision = await _decide(_gate(world, armed=False, redis=FakeRedis()))
    assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "unknown")
    assert await _holds(db) == []
    assert env.emails == []
    assert len(env.waived) == 1


async def test_a_blocked_booking_never_e_mails_the_clinic_however_often_it_repeats(db, env):
    async with db() as session:
        world = await make_world(session, with_account=False, contact_email="c@example.com")
    redis = FakeRedis()
    await _decide(_gate(world, armed=False, redis=redis))
    await _decide(_gate(world, armed=False, redis=redis))
    assert env.emails == []


async def test_a_failure_deciding_after_the_clinic_is_known_to_be_on_before_books_without_it(
    db, env, monkeypatch
):
    """TASK-071 Plan 3, ruling R6 (was: "...blocks"): `quote_failed` is technical."""

    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(deposit_gate, "quote_deposit", boom)
    monkeypatch.setattr(deposit_gate, "_retry_sleep", _no_sleep)
    async with db() as session:
        world = await make_world(session)
    decision = await _decide(_gate(world, armed=False))
    assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "unknown")
    assert [w["stage"] for w in env.waived] == ["quote"]


async def _no_sleep(seconds: float) -> None:
    return None


async def test_a_transient_failure_deciding_the_deposit_is_retried_on_a_fresh_session(
    db, env, monkeypatch
):
    """Owner, 2026-10-29: retried before giving up. The first attempt's session may hold a failed
    transaction, so the retry runs on a NEW one, and the real quote decides the booking."""
    real = deposit_gate.quote_deposit
    sessions: list = []

    async def flaky(session, *args, **kwargs):
        sessions.append(session)
        if len(sessions) == 1:
            raise RuntimeError("database went away")
        return await real(session, *args, **kwargs)

    monkeypatch.setattr(deposit_gate, "quote_deposit", flaky)
    monkeypatch.setattr(deposit_gate, "_retry_sleep", _no_sleep)
    async with db() as session:
        world = await make_world(session)
    decision = await _decide(_gate(world, armed=False))

    assert len(sessions) == 2 and sessions[0] is not sessions[1]
    assert decision.deposit_waived_reason is None  # the real quote, not `quote_failed`
    assert decision.outcome == "awaiting_payment"
    assert env.waived == []


async def test_on_before_learnt_by_an_earlier_attempt_survives_later_failures(
    db, env, monkeypatch
):
    """`BookingGate._deposit_quote` retries: attempt 1 learns the clinic is on `before` and then
    fails; attempts 2 and 3 fail BEFORE reading the timing. The verdict is still the `before`
    fallback (`quote_failed` -> booked without the deposit, reason `unknown`), never the
    fail-open None of a clinic not known to charge up front."""
    real_timing = deposit_gate.timing_is_before
    timing_calls: list[int] = []
    quote_calls: list[int] = []

    def flaky_timing(tenant):
        timing_calls.append(1)
        if len(timing_calls) > 1:
            raise RuntimeError("database went away")
        return real_timing(tenant)

    async def boom(*args, **kwargs):
        quote_calls.append(1)
        raise RuntimeError("database went away")

    monkeypatch.setattr(deposit_gate, "timing_is_before", flaky_timing)
    monkeypatch.setattr(deposit_gate, "quote_deposit", boom)
    monkeypatch.setattr(deposit_gate, "_retry_sleep", _no_sleep)
    async with db() as session:
        world = await make_world(session)

    decision = await _decide(_gate(world, armed=False))

    assert len(timing_calls) == 3 and len(quote_calls) == 1  # 3 attempts, 1 reached the quote
    assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "unknown")


async def test_a_window_another_conversation_holds_is_slot_taken_and_nothing_is_charged(db, env):
    from secretaria.models import Conversation, Patient

    async with db() as session:
        world = await make_world(session)
        rival = Patient(id=uuid4(), tenant_id=world.tenant_id, wa_id="5511888888888")
        session.add(rival)
        await session.flush()
        conv = Conversation(id=uuid4(), tenant_id=world.tenant_id, patient_id=rival.id)
        session.add(conv)
        await session.commit()
    await holds.place_hold(
        tenant_id=world.tenant_id,
        conversation_id=conv.id,
        patient_id=rival.id,
        professional_id=None,
        appointment_type="Consulta",
        insurance=None,
        start_at=SLOT_START,
        end_at=SLOT_END,
    )
    assert (await _decide(_gate(world, armed=False))).outcome == "slot_taken"
    assert env.provider.called("create_pix_charge") == 0


async def test_a_conversation_that_already_waits_for_a_pix_cannot_start_a_second_one(db, env):
    async with db() as session:
        world = await make_world(session)
    await _decide(_gate(world, armed=False))
    env.provider.created_charge = new_charge("pay_2")
    decision = await _decide(_gate(world, armed=False))
    assert (decision.outcome, decision.blocked_reason) == ("blocked", "charge_pending")
    assert len(await _holds(db)) == 1 and len(await _deposits(db)) == 1


async def test_a_waived_deposit_logs_the_safe_reason_stage_and_status(db, env):
    """TASK-071 Plan 3 (was `booking_gate_deposit_blocked`): a technical failure now logs the
    distinct `booking_gate_deposit_waived` event, with the same closed vocabulary."""
    from secretaria.services.payments.provider import ProviderError
    from tests.before_support import recorded_logs

    env.provider.create_error = ProviderError(
        "charge_failed", status_code=401, reason="api_key_invalid", stage="charge"
    )
    async with db() as session:
        world = await make_world(session)
    with recorded_logs() as logs:
        decision = await _decide(_gate(world, armed=False, redis=FakeRedis()))
    assert (decision.outcome, decision.deposit_waived_reason) == ("commit", "api_key_invalid")
    assert not [e for e in logs if e["event"] == "booking_gate_deposit_blocked"]
    (line,) = [e for e in logs if e["event"] == "booking_gate_deposit_waived"]
    assert (line["reason"], line["stage"], line["status"]) == ("api_key_invalid", "charge", 401)
    assert line["code"] == "charge_failed"
    assert set(line) <= {
        "event", "log_level", "code", "reason", "stage", "status", "tenant_id", "conversation_id"
    }
