"""`BookingGate.decide` with a deposit (services/booking_hold.py, spec sections 4.2, 5.2, 7, 11).

WhatsApp goes straight to the Pix; the Portal proves the address FIRST (decision D12) and the
payment starts on the same hold once the code is verified; a clinic on `after` (every clinic
today) leaves the gate exactly as it was; a deposit that cannot be asked for BLOCKS the booking,
tells the clinic and leaves nothing held.
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import BookingHold, PixDeposit
from secretaria.services import booking_hold as holds, email
from secretaria.services.payments import booking_charge, deposit_gate, registry
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

    monkeypatch.setattr(holds, "request_code", request_code)
    monkeypatch.setattr(deposit_gate, "get_entitlements", entitlements)
    monkeypatch.setattr(email, "send_payment_alert", send_email)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", e.provider)
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


async def test_a_cancelled_addon_commits_and_never_stops_the_agenda(db, env):
    env.entitlement = entitlement_summary(addon=False)
    async with db() as session:
        world = await make_world(session)
    assert (await _decide(_gate(world, armed=False))).outcome == "commit"


async def test_a_missing_credential_blocks_the_booking_and_holds_nothing(
    db, env
):
    async with db() as session:
        world = await make_world(session, with_account=False, contact_email="clinic@example.com")
    decision = await _decide(_gate(world, armed=False, redis=FakeRedis()))
    assert (decision.outcome, decision.blocked_reason) == ("blocked", "no_credential")
    assert await _holds(db) == [] and await _deposits(db) == []
    assert env.emails == []  # owner answer 13: no operational e-mail


async def test_a_provider_failure_blocks_and_leaves_no_hold(db, env):
    from secretaria.services.payments.provider import ProviderError

    env.provider.create_error = ProviderError("http_500", status_code=500, retryable=True)
    async with db() as session:
        world = await make_world(session, contact_email="clinic@example.com")
    decision = await _decide(_gate(world, armed=False, redis=FakeRedis()))
    assert (decision.outcome, decision.blocked_reason) == ("blocked", "http_500")
    assert await _holds(db) == []
    assert env.emails == []


async def test_a_blocked_booking_never_e_mails_the_clinic_however_often_it_repeats(db, env):
    async with db() as session:
        world = await make_world(session, with_account=False, contact_email="c@example.com")
    redis = FakeRedis()
    await _decide(_gate(world, armed=False, redis=redis))
    await _decide(_gate(world, armed=False, redis=redis))
    assert env.emails == []


async def test_an_unreadable_entitlement_blocks_instead_of_booking_for_free(db, env):
    env.entitlement = None
    async with db() as session:
        world = await make_world(session)
    decision = await _decide(_gate(world, armed=False))
    assert (decision.outcome, decision.blocked_reason) == (
        "blocked",
        "entitlement_unresolvable",
    )


async def test_a_failure_deciding_after_the_clinic_is_known_to_be_on_before_blocks(
    db, env, monkeypatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("database went away")

    monkeypatch.setattr(deposit_gate, "quote_deposit", boom)
    async with db() as session:
        world = await make_world(session)
    decision = await _decide(_gate(world, armed=False))
    assert (decision.outcome, decision.blocked_reason) == ("blocked", "quote_failed")


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
