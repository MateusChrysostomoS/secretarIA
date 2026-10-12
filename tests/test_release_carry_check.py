"""The provider check at the "Remarcar" confirmation (TASK-052, owner 2026-10-12).

Read only - the platform never refunds (D15). Paid and not refunded: the deposit is reused.
Refunded / being refunded, or the clinic already marked it refunded: a NEW Pix. The provider
unreachable, slow, failing or answering something unexpected: FAIL-SAFE = a NEW Pix, the old
deposit becomes a refund the clinic owes ("clinic_carry_refused"). One clinic's key never reads
another clinic's charge.
"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import (
    email,
    release_carry_check as rcc,
    release_offer as ro,
    release_offer_dispatch,
)
from secretaria.services.payments import asaas_provider, registry
from secretaria.services.payments.provider import (
    ChargeState,
    PaymentAccount,
    PaymentStatus,
    ProviderError,
)
from tests.payments_support import FakeProvider, make_sessionmaker
from tests.release_offer_support import RELEASED_START, T, emails, make_released, new_appointment

PAID = ChargeState(status=PaymentStatus.PAID, paid_amount_cents=10000)


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


async def _rescheduling(db, *, at=T):
    async with db() as session:
        released = await make_released(session)
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=at,
        )
        await ro.choose_reschedule(
            session, offer=offer, conversation_id=released.conversation.id, now=at
        )
        await session.commit()
    return released, offer


async def _row(db, offer_id) -> DepositReleaseOffer:
    async with db() as session:
        return await session.get(DepositReleaseOffer, offer_id)


async def _book(db, released, *, days=4):
    async with db() as session:
        fresh = await new_appointment(
            session, released, start=RELEASED_START + timedelta(days=days)
        )
        carried = await ro.claim_carry_over(
            session,
            tenant_id=released.tenant_id,
            conversation_id=released.conversation.id,
            new_appointment=fresh,
        )
        await session.commit()
    return carried


async def _kinds(db, offer_id) -> list[str]:
    async with db() as session:
        return [e.kind for e in await emails(session, offer_id)]


async def test_asaas_says_paid_reuses_the_deposit(db, monkeypatch):
    provider = _provider(monkeypatch, state=PAID)
    released, offer = await _rescheduling(db)

    assert await rcc.ensure_checked(released.conversation.id) == "reusable"
    carried = await _book(db, released)

    assert carried is not None and provider.called("get_charge") == 1
    row = await _row(db, offer.id)
    assert (row.decision, row.carry_check) == ("rebooked", "reusable")
    assert row.deposit_reused_at is not None
    assert "clinic_rebooked_deposit" in await _kinds(db, offer.id)


@pytest.mark.parametrize(
    "state",
    [
        ChargeState(status=PaymentStatus.REFUNDED),  # REFUNDED / REFUND_REQUESTED / IN_PROGRESS
        ChargeState(status=PaymentStatus.PAID, refunds="some"),  # a (partial) refund listed
    ],
    ids=["asaas_says_refunded_or_in_progress", "asaas_lists_a_refund"],
)
async def test_asaas_says_refunded_charges_a_new_pix(db, monkeypatch, state):
    _provider(monkeypatch, state=state)
    released, offer = await _rescheduling(db)

    assert await rcc.ensure_checked(released.conversation.id) == "refunded"

    assert await _book(db, released) is None  # nothing carried: the booking charges a new Pix
    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status, row.refund_source) == (
        "rebooked", "due", "carry_refunded",
    )  # fmt: skip
    assert row.deposit_reused_at is None
    assert "clinic_carry_refused" in await _kinds(db, offer.id)


def test_the_asaas_client_maps_refund_states_and_the_refund_list(monkeypatch):
    answers = {
        "p1": {"id": "p1", "status": "REFUNDED"},
        "p2": {"id": "p2", "status": "REFUND_REQUESTED"},
        "p3": {"id": "p3", "status": "REFUND_IN_PROGRESS"},
        "p4": {"id": "p4", "status": "RECEIVED", "refunds": [{"status": "DONE"}]},
        "p5": {"id": "p5", "status": "RECEIVED", "refunds": []},
        "p6": {"id": "p6", "status": "RECEIVED"},
        "p7": {"id": "p7", "status": "RECEIVED", "refunds": "?"},
    }

    class _Client:
        async def get_payment(self, payment_id):
            return answers[payment_id]

    monkeypatch.setattr(asaas_provider, "client_for", lambda key: _Client())
    account = PaymentAccount(tenant_id=uuid4(), provider="asaas", api_key="placeholder")

    async def state(pid):
        return await asaas_provider.AsaasProvider().get_charge(account, pid)

    got = {pid: asyncio.run(state(pid)) for pid in answers}
    assert {pid: rcc.verdict_from(s) for pid, s in got.items()} == {
        "p1": "refunded", "p2": "refunded", "p3": "refunded", "p4": "refunded",
        "p5": "reusable", "p6": "reusable", "p7": "unknown",
    }  # fmt: skip


async def test_the_clinic_marked_refunded_without_asaas_refunding_charges_again(db, monkeypatch):
    provider = _provider(monkeypatch, state=PAID)  # Asaas still says PAID
    async with db() as session:
        released = await make_released(session)
        offer = await ro.open_offer(
            session, tenant=released.tenant, appointment=released.appointment,
            deposit=released.deposit, now=T,
        )  # fmt: skip
        await session.commit()
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        await ro.choose_cancel(session, offer=row, now=T + timedelta(hours=1))
        await session.commit()
    async with db() as session:
        assert (
            await ro.mark_refunded(
                session, tenant_id=released.tenant_id, offer_id=offer.id,
                note="Estornado no painel do Asaas", resolved_by_name="Ana",
                now=T + timedelta(hours=2),
            )
            == "marked"
        )  # fmt: skip
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        outcome = await ro.choose_reschedule(
            session, offer=row, conversation_id=released.conversation.id, now=T + timedelta(hours=3)
        )
        await session.commit()

    assert outcome == ro.RESCHEDULE_NEW_CHARGE
    assert await rcc.ensure_checked(released.conversation.id) is None  # nothing to carry
    assert provider.called("get_charge") == 0  # the clinic's word is enough: no provider call
    assert await _book(db, released) is None


@pytest.mark.parametrize(
    "setup",
    ["provider_error", "network_error", "timeout", "unexpected_status", "unreadable_refunds",
     "unexpected_answer", "no_account", "no_charge_id"],
)  # fmt: skip
async def test_asaas_down_or_unreadable_charges_again_and_the_old_deposit_is_owed(
    db, monkeypatch, setup
):
    kw = {"state": PAID}
    if setup == "provider_error":
        kw = {"get_error": ProviderError("get_charge_failed", status_code=500, retryable=True)}
    elif setup == "network_error":
        kw = {"get_error": OSError("connection reset")}
    elif setup == "unexpected_status":
        kw = {"state": ChargeState(status=PaymentStatus.OTHER)}
    elif setup == "unreadable_refunds":
        kw = {"state": ChargeState(status=PaymentStatus.PAID, refunds="unreadable")}
    provider = _provider(monkeypatch, **kw)
    if setup == "timeout":
        monkeypatch.setattr(rcc, "CHECK_TIMEOUT_SECONDS", 0.05)

        async def _slow(account, charge_id):
            await asyncio.sleep(1)
            return PAID

        monkeypatch.setattr(provider, "get_charge", _slow)
    if setup == "no_account":

        async def _none(session, tenant_id, provider_name):
            return None

        monkeypatch.setattr(rcc, "_account", _none)
    released, offer = await _rescheduling(db)
    if setup == "no_charge_id":
        async with db() as session:
            deposit = await session.get(type(released.deposit), released.deposit.id)
            deposit.provider_charge_id = None
            deposit.asaas_payment_id = None
            await session.commit()
    if setup == "unexpected_answer":  # not a ChargeState at all
        monkeypatch.setattr(provider, "state", {released.deposit.provider_charge_id: {"x": 1}})

    assert await rcc.ensure_checked(released.conversation.id) == "unknown"

    assert await _book(db, released) is None  # FAIL-SAFE: never reuse a deposit we cannot verify
    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status, row.refund_source) == (
        "rebooked", "due", "carry_unknown",
    )  # fmt: skip
    assert await _kinds(db, offer.id) == ["clinic_carry_refused"]


async def test_two_confirm_clicks_reuse_the_deposit_once(db, monkeypatch):
    provider = _provider(monkeypatch, state=PAID)
    released, offer = await _rescheduling(db)

    first = await rcc.ensure_checked(released.conversation.id)
    provider.state = ChargeState(status=PaymentStatus.REFUNDED)  # the 2nd click's answer never wins
    second = await rcc.ensure_checked(released.conversation.id)
    carried = [await _book(db, released, days=4), await _book(db, released, days=5)]

    assert (first, second) == ("reusable", "reusable") and provider.called("get_charge") == 1
    # ONE booking carries (guarded UPDATE); the truly concurrent version runs on Postgres (Task 13).
    assert sum(1 for c in carried if c is not None) == 1
    row = await _row(db, offer.id)
    assert (row.decision, row.carry_check) == ("rebooked", "reusable")
    assert (await _kinds(db, offer.id)).count("clinic_rebooked_deposit") == 1


async def test_marking_refunded_after_the_reuse_is_refused(db, monkeypatch):
    _provider(monkeypatch, state=PAID)
    released, offer = await _rescheduling(db)
    await rcc.ensure_checked(released.conversation.id)
    assert await _book(db, released) is not None

    async with db() as session:
        outcome = await ro.mark_refunded(
            session, tenant_id=released.tenant_id, offer_id=offer.id, note="Estornado",
            resolved_by_name="Ana",
        )  # fmt: skip

    assert outcome == "deposit_reused"  # the hub answers 409 `deposit_reused` (Task 11)
    assert (await _row(db, offer.id)).refund_status == "not_due"


async def test_the_asaas_check_uses_only_this_clinics_key_and_charge(db, monkeypatch):
    released_a, offer_a = await _rescheduling(db)
    released_b, _offer_b = await _rescheduling(db)
    provider = _provider(
        monkeypatch,
        state={
            released_a.deposit.provider_charge_id: PAID,
            released_b.deposit.provider_charge_id: ChargeState(status=PaymentStatus.REFUNDED),
        },
    )
    keys: list = []

    async def _account(session, tenant_id, provider_name):
        keys.append(tenant_id)
        return PaymentAccount(
            tenant_id=tenant_id, provider=provider_name, api_key=f"key-{tenant_id}"
        )

    monkeypatch.setattr(rcc, "_account", _account)

    assert await rcc.ensure_checked(released_a.conversation.id) == "reusable"

    assert keys == [released_a.tenant_id] and provider.tenants_seen == [released_a.tenant_id]
    assert provider.calls == [("get_charge", released_a.deposit.provider_charge_id)]
    async with db() as session:  # clinic B's offer was never touched
        others = list(
            await session.scalars(
                select(DepositReleaseOffer).where(DepositReleaseOffer.id != offer_a.id)
            )
        )
    assert [o.carry_check for o in others] == [None]


async def test_a_deposit_of_another_clinic_is_never_read_with_this_clinics_key(db, monkeypatch):
    provider = _provider(monkeypatch, state=PAID)
    released, offer = await _rescheduling(db)
    other_tenant = (await _rescheduling(db))[0].tenant_id
    async with db() as session:  # a corrupted row: the deposit now belongs to another clinic
        deposit = await session.get(type(released.deposit), released.deposit.id)
        deposit.tenant_id = other_tenant
        await session.commit()

    assert await rcc.ensure_checked(released.conversation.id) == "unknown"
    assert provider.called("get_charge") == 0


def test_the_carry_refused_email_says_refund_the_old_deposit_and_why():
    subject, unknown = email.release_carry_refused_clinic_email(
        clinic_name="Clínica X", check="unknown", amount_text="R$ 60,00",
        slot_text="08/03 às 10:00", conversation_link="https://portal.example/c/1",
    )  # fmt: skip
    _subject, refunded = email.release_carry_refused_clinic_email(
        clinic_name="Clínica X", check="refunded", amount_text="R$ 60,00",
        slot_text="08/03 às 10:00", conversation_link=None,
    )  # fmt: skip

    assert "DEVOLVA o sinal anterior" in subject and "R$ 60,00" in subject
    assert "pode ter pago duas vezes" in unknown and "Estornar" in unknown
    assert "já foi estornado" in refunded and "marque como devolvido" in refunded
    for body in (unknown, refunded):
        assert "banco" not in body.lower()


def test_the_carry_refused_notice_is_relevant_until_the_clinic_marks_it():
    offer = DepositReleaseOffer(deposit_paid=True, decision="rebooked", refund_status="due")
    assert release_offer_dispatch.still_relevant("clinic_carry_refused", offer)
    offer.refund_status = "marked"
    assert not release_offer_dispatch.still_relevant("clinic_carry_refused", offer)
