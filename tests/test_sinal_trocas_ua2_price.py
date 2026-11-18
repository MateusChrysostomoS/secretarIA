"""TASK-085 R / UA2 - `service_change.price_requote` (D-R14): the CLINIC changed the price of a
booked consultation whose sinal is PAID; the patient opened the e-mail's link.

Contract: tasks/TASK-085/sinal-trocas/mapa-u.md §2.1, §2.3, §4.4, §7 items 12/20 (+ UB1's notes:
never abate / refund the same difference twice; a difference the clinic collects on the day is
refused). The seeded clinic of tests/test_service_change.py: Consulta at R$ 200,00, sinal 30 %
paid = R$ 60,00, the visit in 5 days (refund question from 2 days). The clinic then changes the
price of Consulta:

* R$ 300 -> sinal R$ 90: a Pix of R$ 30 only, `mode="applied"` in BOTH timings, nothing of the
  consultation written; unpaid -> `collect_due` + `service_diff_unpaid` + `price_unpaid_text`;
* R$ 100 -> sinal R$ 30: "quer a diferença de volta?" (far) or abated (close);
* R$ 220 -> +R$ 6 (`increase_free`) / the same price -> nothing;
* a decrease already settled (`clinic_kept`, `kept_credit`, `refund_requested`) is never told
  again; a further drop only adds what is new.
"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from secretaria.models import (
    Appointment,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    KIND_DECREASE,
    KIND_INCREASE,
    MODE_APPLIED,
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CLINIC_KEPT,
    STATUS_COLLECT_DUE,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import REASON_SERVICE_DIFF_UNPAID
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from secretaria.services import booking_hold
from secretaria.services.payments import notify, service_change
from secretaria.services.payments.provider import ProviderError
from tests import test_service_change as base

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(booking_hold, "_now", lambda: NOW)
    monkeypatch.setattr(service_change, "_now", lambda: NOW)


@pytest.fixture
def sent(monkeypatch):
    out: list = []

    async def fake_deliver(session, tenant, patient, messages, **kw):
        out.extend(messages)

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    return out


async def _seed(db, *, timing="after", days_ahead: float = 5, **kw):
    async with db() as session:
        seeded = await base._seed(session, days_ahead=days_ahead, **kw)
        tenant = await session.get(Tenant, seeded[0].id)
        tenant.deposit_timing = timing
        await session.commit()
    return seeded


async def _price(db, seeded, price: str | None):
    """The clinic edits the price of Consulta (None = no price)."""
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        catalog = []
        for entry in base.CATALOG:
            entry = dict(entry)
            if entry["name"] == "Consulta":
                if price is None:
                    entry.pop("price", None)
                else:
                    entry["price"] = price
            catalog.append(entry)
        tenant.appointment_types = catalog
        await session.commit()


async def _requote(db, seeded, *, now=NOW):
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        result = await service_change.price_requote(
            session, tenant=tenant, appointment=row, deposit=deposit, now=now
        )
        await session.commit()
    return result


async def _adjustments(db) -> list[DepositAdjustment]:
    async with db() as session:
        query = select(DepositAdjustment).order_by(DepositAdjustment.created_at)
        return list((await session.scalars(query)).all())


async def _row(db, seeded) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, seeded[2].id)


async def _answer(db, seeded, adjustment_id, accept):
    async with db() as session:
        outcome = await service_change.answer_refund_question(
            session, tenant_id=seeded[0].id, adjustment_id=adjustment_id, accept=accept, now=NOW
        )
        await session.commit()
    return outcome


# --------------------------------------------------------------------------
# a rise
# --------------------------------------------------------------------------


@pytest.mark.parametrize("timing", ["after", "before"])
async def test_a_rise_sends_a_pix_of_the_difference_and_keeps_the_consultation(
    db, provider, timing
):
    seeded = await _seed(db, timing=timing)
    before = await _row(db, seeded)
    await _price(db, seeded, "R$ 300,00")

    result = await _requote(db, seeded)

    assert (result.kind, result.decision, result.diff_cents) == (
        "applied_awaiting_payment", "increase_charge", 3000,
    )
    assert result.pix_copy_paste == "000201pix"
    assert result.expires_at == NOW + timedelta(minutes=30)
    [adjustment] = await _adjustments(db)
    assert (adjustment.kind, adjustment.mode, adjustment.status) == (
        KIND_INCREASE, MODE_APPLIED, STATUS_AWAITING_PAYMENT,
    )
    assert adjustment.id == result.adjustment_id
    assert (adjustment.old_service, adjustment.new_service) == ("Consulta", "Consulta")
    data = adjustment.pending_edit
    assert (data["source"], data["old_sinal_cents"], data["new_sinal_cents"]) == (
        "price_change", 6000, 9000,
    )
    assert service_change.is_price_change(data)
    [(_n, request)] = [call for call in provider.calls if call[0] == "create_pix_charge"]
    assert (request.amount_cents, request.external_reference) == (3000, str(adjustment.id))
    # Nothing of the consultation was written - no edit, no doctor notice, no hold.
    after = await _row(db, seeded)
    assert (after.appointment_type, after.start_at, after.end_at, after.status) == (
        before.appointment_type, before.start_at, before.end_at, before.status,
    )
    async with db() as session:
        notices = await session.scalar(
            select(func.count()).select_from(ProfessionalEditNotice)
        )
    assert notices == 0


async def test_an_unpaid_rise_leaves_the_consultation_and_tells_the_clinic(db, provider, sent):
    seeded = await _seed(db)
    await _price(db, seeded, "R$ 300,00")
    created = await _requote(db, seeded)

    async with db() as session:
        assert await service_change.expire_adjustments(
            session, now=NOW + timedelta(minutes=31)
        ) == 1

    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_COLLECT_DUE and adjustment.id == created.adjustment_id
    async with db() as session:
        [item] = (await session.scalars(select(PaymentAttentionItem))).all()
    assert (item.reason, item.amount_cents) == (REASON_SERVICE_DIFF_UNPAID, 3000)
    [message] = sent
    assert message.body == service_change.price_unpaid_text(3000)
    assert (await _row(db, seeded)).appointment_type == "Consulta"


async def test_the_clinic_reads_what_the_price_change_does(db, provider):
    seeded = await _seed(db)
    await _price(db, seeded, "R$ 300,00")
    await _requote(db, seeded)
    [adjustment] = await _adjustments(db)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        summaries = await service_change.pending_change_summaries(
            session, tenant, {adjustment.id: adjustment.pending_edit}
        )
    assert summaries[adjustment.id] == "Novo preço: sinal R$ 60,00 → R$ 90,00"


@pytest.mark.parametrize(
    ("failure", "reason"),
    [
        (ProviderError("down", status_code=503, retryable=True), "charge_failed"),
        (ProviderError("bad", status_code=400, retryable=False), "charge_failed"),
    ],
)
async def test_a_rise_the_provider_refuses_records_nothing(db, provider, failure, reason):
    seeded = await _seed(db)
    await _price(db, seeded, "R$ 300,00")
    provider.create_error = failure
    result = await _requote(db, seeded)
    assert (result.kind, result.reason) == ("blocked", reason)
    assert await _adjustments(db) == []


async def test_a_rise_without_a_payment_account_is_refused(db, provider, monkeypatch):
    from secretaria.services.payments import accounts

    async def no_account(*args, **kwargs):
        return None

    monkeypatch.setattr(accounts, "load_charge_account", no_account)
    seeded = await _seed(db)
    await _price(db, seeded, "R$ 300,00")
    result = await _requote(db, seeded)
    assert (result.kind, result.reason) == ("blocked", "no_payment_account")
    assert await _adjustments(db) == []


# --------------------------------------------------------------------------
# a drop
# --------------------------------------------------------------------------


async def test_a_drop_far_ahead_asks_for_the_difference(db, provider):
    seeded = await _seed(db)
    await _price(db, seeded, "R$ 100,00")
    result = await _requote(db, seeded)
    assert (result.kind, result.decision, result.diff_cents) == ("asked", "decrease_ask", 3000)
    [adjustment] = await _adjustments(db)
    assert (adjustment.kind, adjustment.status, adjustment.mode) == (
        KIND_DECREASE, STATUS_ASKED, None,
    )
    assert adjustment.pending_edit["new_sinal_cents"] == 3000
    assert (await _row(db, seeded)).appointment_type == "Consulta"


async def test_a_drop_close_to_the_visit_is_abated(db, provider):
    seeded = await _seed(db, days_ahead=1)
    await _price(db, seeded, "R$ 100,00")
    result = await _requote(db, seeded)
    assert (result.kind, result.decision, result.diff_cents) == (
        "applied_now", "decrease_keep", 3000,
    )
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_CLINIC_KEPT and adjustment.id == result.adjustment_id


@pytest.mark.parametrize("accept", [True, False])
async def test_a_settled_drop_is_never_told_twice(db, provider, accept):
    """UB1 note: the notice may be re-armed by a later edit - the same difference is neither
    refunded nor abated again (`refund_requested` is out of what is held; `kept_credit` is
    netted)."""
    seeded = await _seed(db)
    await _price(db, seeded, "R$ 100,00")
    asked = await _requote(db, seeded)
    await _answer(db, seeded, asked.adjustment_id, accept)

    again = await _requote(db, seeded)

    assert (again.kind, again.decision, again.adjustment_id) == ("applied_now", "no_change", None)
    assert len(await _adjustments(db)) == 1


async def test_an_abated_drop_then_a_further_drop_adds_only_the_new_part(db, provider):
    seeded = await _seed(db, days_ahead=1)
    await _price(db, seeded, "R$ 100,00")
    first = await _requote(db, seeded)
    assert (first.decision, first.diff_cents) == ("decrease_keep", 3000)
    assert (await _requote(db, seeded)).decision == "no_change"

    await _price(db, seeded, "R$ 50,00")  # sinal R$ 15: R$ 15 more than already abated
    second = await _requote(db, seeded)

    assert (second.decision, second.diff_cents) == ("decrease_keep", 1500)
    last = (await _adjustments(db))[-1]
    assert last.pending_edit["new_sinal_cents"] == 1500


async def test_after_an_abated_drop_a_rise_back_charges_nothing(db, provider):
    seeded = await _seed(db, days_ahead=1)
    await _price(db, seeded, "R$ 100,00")
    await _requote(db, seeded)
    await _price(db, seeded, "R$ 200,00")  # back to what was paid
    result = await _requote(db, seeded)
    assert (result.kind, result.decision) == ("applied_now", "no_change")
    assert provider.called("create_pix_charge") == 0


# --------------------------------------------------------------------------
# nothing to do / refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("price", "decision"), [("R$ 220,00", "increase_free"), ("R$ 200,00", "no_change")]
)
async def test_a_small_or_no_change_records_nothing(db, provider, price, decision):
    seeded = await _seed(db)
    await _price(db, seeded, price)
    result = await _requote(db, seeded)
    assert (result.kind, result.decision, result.adjustment_id) == ("applied_now", decision, None)
    assert await _adjustments(db) == []
    assert provider.calls == []


async def test_an_unreadable_price_is_refused(db, provider):
    seeded = await _seed(db)
    await _price(db, seeded, None)
    result = await _requote(db, seeded)
    assert (result.kind, result.reason) == ("blocked", "price_unavailable")


async def test_an_unpaid_sinal_is_not_this_path(db, provider):
    seeded = await _seed(db, deposit_status=PixDepositStatus.AWAITING)
    await _price(db, seeded, "R$ 300,00")
    result = await _requote(db, seeded)
    assert (result.kind, result.reason) == ("blocked", "deposit_not_paid")


async def test_an_open_adjustment_is_never_charged_twice(db, provider):
    seeded = await _seed(db)
    await _price(db, seeded, "R$ 300,00")
    first = await _requote(db, seeded)
    assert first.kind == "applied_awaiting_payment"
    again = await _requote(db, seeded)
    assert (again.kind, again.reason) == ("blocked", "adjustment_pending")
    assert provider.called("create_pix_charge") == 1


async def test_a_difference_the_clinic_collects_refuses_the_requote(db, provider):
    """UB1 / review: what was paid does not count a `collect_due` difference, so a new figure
    would ask it again - refused even after its item was resolved."""
    seeded = await _seed(db)
    async with db() as session:
        session.add(
            DepositAdjustment(
                id=uuid4(), tenant_id=seeded[0].id, deposit_id=seeded[3].id,
                appointment_id=seeded[2].id, kind=KIND_INCREASE, status=STATUS_COLLECT_DUE,
                old_service="Consulta", new_service="Avaliação", diff_cents=3000,
                resolved_at=NOW,
            )
        )
        await session.commit()
    await _price(db, seeded, "R$ 300,00")
    result = await _requote(db, seeded)
    assert (result.kind, result.reason) == ("blocked", "collect_pending")
    assert provider.calls == []


async def test_the_preview_writes_nothing(db, provider):
    seeded = await _seed(db)
    await _price(db, seeded, "R$ 100,00")
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        decision = await service_change.preview_price_requote(
            session, tenant=tenant, appointment=row, deposit=deposit, now=NOW
        )
        assert not session.new and not session.dirty
    assert (decision.action, decision.diff_cents) == ("decrease_ask", 3000)
    assert await _adjustments(db) == []


async def test_rows_of_another_clinic_are_a_programming_error(db, provider):
    seeded = await _seed(db)
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        with pytest.raises(ValueError):
            await service_change.price_requote(
                session, tenant=SimpleNamespace(id=uuid4()), appointment=row, deposit=deposit,
                now=NOW,
            )
    assert await _adjustments(db) == []
