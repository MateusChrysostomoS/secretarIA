"""Fixes from the whole-branch review of TASK-052 (money first).

Each test is the reviewer's failure scenario: a stale "reusable" answer, a Remarcar after E, a
sweep or a refund marking racing a confirmed carry, a second release of a carried deposit, a keep
accepted after the refund fell due, and a refused carry that later taps call "valid".
"""

from datetime import timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import (
    booking_promotion,
    release_carry_check as rcc,
    release_keep as rk,
    release_offer as ro,
)
from secretaria.services.payments import registry
from secretaria.services.payments.provider import (
    ChargeState,
    PaymentAccount,
    PaymentStatus,
    ProviderError,
)
from secretaria.workers.shared import release_offer_actions as roa
from tests.payments_support import FakeProvider, make_sessionmaker
from tests.release_offer_support import T, make_released
from tests.test_release_carry_check import PAID, _book, _rescheduling, _row
from tests.test_release_keep import _Calendar

E = T + timedelta(hours=24)
REFUNDED = ChargeState(status=PaymentStatus.REFUNDED)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture(autouse=True)
def _wire(monkeypatch, db):
    monkeypatch.setattr(rcc, "async_session_factory", db)

    async def _account(session, tenant_id, provider):  # a placeholder, never a real key
        return PaymentAccount(tenant_id=tenant_id, provider=provider, api_key=f"key-{tenant_id}")

    monkeypatch.setattr(rcc, "_account", _account)


def _provider(monkeypatch, **kw) -> FakeProvider:
    fake = FakeProvider(**kw)
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _open(db, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
        )
        await session.commit()
    return released, offer


async def _sweep(db, at) -> int:
    async with db() as session:
        return await ro.expire_offers(session, now=at)


# --- C1: a stored "reusable" does not outlive the minutes it was read in --------------------------


async def test_a_stale_reusable_answer_is_asked_again_before_the_next_booking(db, monkeypatch):
    provider = _provider(monkeypatch, state=PAID)
    released, offer = await _rescheduling(db)
    conversation = released.conversation.id
    assert await rcc.ensure_checked(conversation, now=T) == "reusable"
    # Two confirm clicks inside the window still get the one answer.
    assert await rcc.ensure_checked(conversation, now=T + timedelta(minutes=1)) == "reusable"
    assert provider.called("get_charge") == 1

    # The booking never completed; the clinic refunded in Asaas meanwhile; the patient comes back.
    provider.state = REFUNDED
    assert await rcc.ensure_checked(conversation, now=T + timedelta(hours=3)) == "refunded"

    assert provider.called("get_charge") == 2
    assert await _book(db, released) is None  # nothing is carried over a refunded deposit
    assert (await _row(db, offer.id)).deposit_reused_at is None


# --- I1: a Remarcar after E is the patient's answer, not silence ----------------------------------


async def test_a_remarcar_tapped_after_e_is_not_closed_as_no_answer(db, monkeypatch):
    _provider(monkeypatch, state=PAID)
    released, offer = await _open(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await ro.choose_cancel(session, offer=row, now=T + timedelta(hours=1))
        await session.commit()
    late = E + timedelta(hours=1)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        outcome = await ro.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=late
        )
        await session.commit()
    assert outcome == ro.RESCHEDULE_CARRYING

    assert await _sweep(db, late + timedelta(hours=1)) == 0

    assert (await _row(db, offer.id)).decision == "rescheduling"
    assert await rcc.ensure_checked(released.conversation.id, now=late + timedelta(hours=2))
    assert await _book(db, released) is not None
    assert (await _row(db, offer.id)).decision == "rebooked"


# --- I2: a confirmed carry holds off the sweep and the refund marking -----------------------------


async def test_a_fresh_reusable_answer_holds_off_the_sweep_and_the_refund_marking(db, monkeypatch):
    _provider(monkeypatch, state=PAID)
    released, offer = await _rescheduling(db)
    at = E + timedelta(hours=1)  # B and E have both passed
    assert await rcc.ensure_checked(released.conversation.id, now=at) == "reusable"

    assert await _sweep(db, at + timedelta(minutes=1)) == 0
    assert (await _row(db, offer.id)).decision == "rescheduling"
    async with db() as session:
        outcome = await ro.mark_refunded(
            session,
            tenant_id=released.tenant_id,
            offer_id=offer.id,
            note="Estornado no painel do Asaas",
            resolved_by_name="Ana",
            now=at + timedelta(minutes=1),
        )
    assert outcome == "deposit_reused"
    assert await _book(db, released) is not None  # the booking can still carry it

    # Without a booking the answer expires and the deadlines apply again.
    other, second = await _rescheduling(db)
    assert await rcc.ensure_checked(other.conversation.id, now=at) == "reusable"
    assert await _sweep(db, at + timedelta(minutes=20)) >= 1
    assert (await _row(db, second.id)).decision == "no_answer_cancelled"


# --- I3: a deposit released a second time opens its own refund item -------------------------------


async def test_a_second_release_of_a_carried_deposit_opens_its_own_refund_item(db, monkeypatch):
    _provider(monkeypatch, state=PAID)
    released, offer = await _open(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await ro.choose_cancel(session, offer=row, now=T + timedelta(hours=1))
        row = await session.get(DepositReleaseOffer, offer.id)
        await ro.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=T + timedelta(hours=2)
        )
        await session.commit()
    assert await rcc.ensure_checked(released.conversation.id, now=T + timedelta(hours=2))
    assert await _book(db, released) is not None  # the first refund item is closed

    later = T + timedelta(days=2)
    async with db() as session:
        deposit = await session.get(PixDeposit, released.deposit.id)
        fresh = await session.get(Appointment, deposit.appointment_id)
        fresh.status = AppointmentStatus.CANCELLED
        tenant = await session.get(Tenant, released.tenant_id)
        second = await ro.open_offer(
            session, tenant=tenant, appointment=fresh, deposit=deposit, now=later
        )
        await ro.choose_cancel(session, offer=second, now=later + timedelta(hours=1))
        await session.commit()

    async with db() as session:
        items = list(await session.scalars(select(PaymentAttentionItem)))
    assert sorted(item.status for item in items) == ["open", "resolved"]


# --- I4: accepting "Manter" after the refund fell due reads Asaas first ---------------------------


@pytest.fixture
def calendar(monkeypatch) -> _Calendar:
    owner = _Calendar()

    async def _for_booking(session, tenant, professional_id):
        return owner

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", _for_booking)
    return owner


async def _keep_requested_after_b(db):
    released, offer = await _open(db)
    assert await _sweep(db, T + timedelta(hours=7)) == 1  # B: the refund is due
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await ro.request_keep(session, offer=row, now=T + timedelta(hours=8))
        await session.commit()
    return released, offer


async def _accept(db, released, offer_id):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        tenant = await session.get(Tenant, released.tenant_id)
        return await rk.accept_keep(
            session, tenant=tenant, offer=row, resolved_by="Ana", now=T + timedelta(hours=9)
        )


@pytest.mark.parametrize(
    "provider_kw",
    [
        {"state": REFUNDED},
        {"state": PAID, "get_error": ProviderError("boom")},
    ],
)
async def test_keep_is_refused_when_asaas_does_not_confirm_the_deposit_is_still_there(
    db, monkeypatch, calendar, provider_kw
):
    _provider(monkeypatch, **provider_kw)
    released, offer = await _keep_requested_after_b(db)

    result = await _accept(db, released, offer.id)

    assert result.outcome == rk.ACCEPT_DEPOSIT_NOT_REUSABLE and result.appointment is None
    assert calendar.created == []
    assert (await _row(db, offer.id)).decision == "keep_requested"


async def test_keep_is_accepted_when_asaas_confirms_the_paid_deposit(db, monkeypatch, calendar):
    provider = _provider(monkeypatch, state=PAID)
    released, offer = await _keep_requested_after_b(db)

    result = await _accept(db, released, offer.id)

    assert result.outcome == rk.ACCEPT_KEPT and provider.called("get_charge") == 1


# --- I5: a refused carry is not "your deposit is valid" ---------------------------------------


async def test_a_tap_on_an_offer_closed_by_a_refused_carry_does_not_call_the_deposit_valid(
    db, monkeypatch
):
    _provider(monkeypatch, state=REFUNDED)
    released, offer = await _rescheduling(db)
    assert await rcc.ensure_checked(released.conversation.id) == "refunded"
    row = await _row(db, offer.id)

    for action in (ro.ACTION_RESCHEDULE, ro.ACTION_KEEP):
        answer = roa._closed_answer(row, action)
        assert answer.text == ro.TAP_REBOOKED_NEW_CHARGE_TEXT
    assert ro.TAP_ALREADY_REBOOKED_TEXT not in ro.TAP_REBOOKED_NEW_CHARGE_TEXT
    assert "devol" in ro.TAP_REBOOKED_NEW_CHARGE_TEXT
