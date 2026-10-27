"""The payment provider down around a service change on a PAID sinal (follow-up B, 2026-10-27).

Owner's request: "Se o provedor de pagamento estiver fora do ar, pode marcar a consulta normalmente
sem ter que o paciente mandar novamente a diferença do sinal".

* The Pix of the difference cannot be created (Asaas down / erroring, or a charge without its
  code): the change stands NOW, like `increase_free`; the clinic gets ONE attention item to
  collect the difference (`service_diff_collect_due`, with the amount and the consultation); the
  patient is told plainly. That difference is NOT money held (never in a refund figure).
* The consultation is cancelled while the provider is down and the unpaid difference Pix could
  not be voided: the cancellation stands (as before) and the expiry sweep retries the void
  until it succeeds; a late payment of it is still an ORPHAN item.

Seed (tests/test_service_change.py): sinal R$ 60 of "Consulta"; "Avaliação" = +R$ 30.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from secretaria.models import PaymentAttentionItem, PixDeposit, Tenant
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    STATUS_CANCELLED,
    STATUS_CANCELLED_VOID_PENDING,
    STATUS_COLLECT_DUE,
)
from secretaria.models.payment_attention_item import (
    REASON_ORPHAN_PAYMENT,
    REASON_REFUND_DUE_POLICY,
    REASON_SERVICE_DIFF_COLLECT_DUE,
)
from secretaria.services.payments import adjustment_queries, attention_view, service_change
from secretaria.services.payments.provider import (
    ChargeState,
    CreatedCharge,
    PaymentStatus,
    ProviderError,
)
from secretaria.workers.shared.service_change_actions import service_change_bubbles
from tests import (
    test_service_change as base,
    test_service_change_cancel as cancel,
    test_service_change_webhook as hook,
)
from tests.payments_support import recording_enqueue

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks


async def _deposit(db, deposit_id) -> PixDeposit:
    async with db() as session:
        return await session.get(PixDeposit, deposit_id)


async def _held(db, deposit_id) -> int:
    async with db() as session:
        deposit = await session.get(PixDeposit, deposit_id)
        return await service_change.held_deposit_cents(session, deposit)


# --------------------------------------------------------------------------
# B1: the Pix of the difference cannot be created -> applied now + clinic item
# --------------------------------------------------------------------------


async def test_provider_down_applies_the_change_without_a_pix_and_opens_a_collect_item(
    db, provider
):
    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    async with db() as session:
        seeded = await base._seed(session)

    result = await base._request(db, seeded, "Avaliação", minutes=60)

    assert (result.kind, result.diff_cents) == ("applied_now", 3000)
    assert result.decision == service_change.DECISION_COLLECT
    assert result.pix_copy_paste is None and result.reason is None
    # The consultation changed now - the patient never has to ask (or pay) again here.
    row = await base._appointment(db, seeded[2].id)
    assert row.appointment_type == "Avaliação"
    assert (row.end_at - row.start_at) == timedelta(minutes=60)
    assert result.changed_fields == ("serviço",)
    assert await base._notices(db) == 1  # the doctor's ONE "Consulta alterada"
    # Recorded for audit, never as money received.
    [adjustment] = await base._adjustments(db)
    assert result.adjustment_id == adjustment.id
    assert (adjustment.kind, adjustment.status) == (KIND_INCREASE, STATUS_COLLECT_DUE)
    assert adjustment.diff_cents == 3000 and adjustment.provider_charge_id is None
    assert adjustment.resolved_at is not None
    # ONE item for the clinic: collect R$ 30 for this consultation.
    [item] = await base._items(db)
    assert item.reason == REASON_SERVICE_DIFF_COLLECT_DUE
    assert (item.amount_cents, item.paid_amount_cents, item.paid_at) == (3000, None, None)
    assert item.patient_id == seeded[1].id
    assert item.service_label == "Avaliação"
    assert item.slot_start.replace(tzinfo=None) == row.start_at.replace(tzinfo=None)
    assert item.suggested_refund_cents is None
    text = attention_view.reason_text(item.reason, item.suggested_refund_cents, item.amount_cents)
    assert "R$ 30,00" in text and "cobr" in text.lower()
    # The uncollected difference is NOT held money.
    assert await _held(db, seeded[3].id) == 6000
    assert (await _deposit(db, seeded[3].id)).amount_cents == 6000


async def test_a_network_timeout_is_an_outage_too(db, provider):
    provider.create_error = TimeoutError("read timeout")
    async with db() as session:
        seeded = await base._seed(session)

    result = await base._request(db, seeded, "Avaliação")

    assert result.kind == "applied_now" and result.decision == service_change.DECISION_COLLECT
    assert [i.reason for i in await base._items(db)] == [REASON_SERVICE_DIFF_COLLECT_DUE]


@pytest.mark.parametrize(
    "error",
    [
        ProviderError("charge_failed", status_code=401),  # revoked / expired API key
        ProviderError("charge_failed", status_code=403),
        ProviderError("payer_document_required", status_code=400),  # e.g. no CPF
        RuntimeError("unexpected"),  # an unknown failure is never waived
    ],
    ids=["401", "403", "400-missing-cpf", "unknown"],
)
async def test_a_permanent_refusal_blocks_as_before_and_never_waives_the_difference(
    db, provider, error
):
    """Follow-up review I2: only an outage (retryable error / network timeout) goes to the
    collect path - a refusal that will happen again must not hand out the change for free."""
    provider.create_error = error
    async with db() as session:
        seeded = await base._seed(session)

    result = await base._request(db, seeded, "Avaliação")

    assert (result.kind, result.reason) == ("blocked", "charge_failed")
    assert "fale com a clínica" in service_change.blocked_text(result.reason)
    assert await base._items(db) == [] and await base._adjustments(db) == []
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Consulta"
    assert await base._notices(db) == 0


async def test_a_charge_without_its_code_is_voided_and_blocks_as_before(db, provider):
    provider.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste=None)
    async with db() as session:
        seeded = await base._seed(session)

    result = await base._request(db, seeded, "Avaliação")

    assert (result.kind, result.reason) == ("blocked", "charge_failed")
    assert provider.called("cancel_charge") == 1
    assert await base._items(db) == [] and await base._adjustments(db) == []
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_a_timeout_reuses_the_reference_it_sent(db, provider):
    """A timeout may come after the provider created the charge: the collect row keeps the id
    that was sent as the reference, so a late charge is never tied to a stranger id."""
    provider.create_error = TimeoutError("read timeout")
    async with db() as session:
        seeded = await base._seed(session)

    result = await base._request(db, seeded, "Avaliação")

    [(_name, request)] = [c for c in provider.calls if c[0] == "create_pix_charge"]
    assert request.external_reference == str(result.adjustment_id)


async def test_an_unconfigured_account_is_still_refused_not_collected(db, provider, monkeypatch):
    """A disconnected account is configuration, not an outage: nothing changes (as before)."""
    from secretaria.services.payments import accounts

    async def no_account(*args, **kwargs):
        return None

    monkeypatch.setattr(accounts, "load_charge_account", no_account)
    async with db() as session:
        seeded = await base._seed(session)

    result = await base._request(db, seeded, "Avaliação")

    assert (result.kind, result.reason) == ("blocked", "no_payment_account")
    assert await base._items(db) == [] and await base._adjustments(db) == []


async def _collect(db, provider):
    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    seeded = await cancel._seed(db)
    first = await base._request(db, seeded, "Avaliação")
    assert first.decision == service_change.DECISION_COLLECT
    provider.create_error = None
    return seeded, first


async def test_cancelling_closes_the_collect_item_and_never_counts_it_as_refund(db, provider):
    """Follow-up review I3: the clinic is never told to collect for a cancelled consultation."""
    seeded, _first = await _collect(db, provider)

    outcome, _notice = await cancel._cancel(db, seeded)

    assert outcome == "refund_due"
    by_reason = cancel._by_reason(await base._items(db))
    assert by_reason[REASON_REFUND_DUE_POLICY] == [6000]  # only the R$ 60 really paid
    [collect] = [i for i in await base._items(db) if i.reason == REASON_SERVICE_DIFF_COLLECT_DUE]
    assert collect.status == "resolved" and collect.resolved_at is not None
    assert collect.resolution_action == "other"
    assert "cancelada" in collect.resolution_note and "não cobrar" in collect.resolution_note
    assert "devol" not in collect.resolution_note.lower()
    [adjustment] = await base._adjustments(db)
    assert adjustment.status == STATUS_CANCELLED


async def test_a_second_change_while_the_collect_item_is_open_is_blocked(db, provider):
    """Follow-up review I3 (scenario 2): never a second charge for a difference the clinic is
    still collecting."""
    seeded, _first = await _collect(db, provider)

    again = await base._request(db, seeded, "Consulta plus")

    assert (again.kind, again.reason) == ("blocked", "collect_pending")
    text = service_change.blocked_text(again.reason)
    assert "clínica" in text and "continua como estava" in text
    assert provider.called("create_pix_charge") == 1  # only the first (failed) attempt
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Avaliação"


async def test_switching_back_while_the_collect_item_is_open_is_blocked_too(db, provider):
    """The simplest consistent rule: ANY service change waits for the clinic's item."""
    seeded, _first = await _collect(db, provider)

    back = await base._request(db, seeded, "Consulta")

    assert (back.kind, back.reason) == ("blocked", "collect_pending")
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Avaliação"
    [item] = await base._items(db)
    assert item.status == "open"


async def test_once_the_clinic_resolves_the_item_changes_are_allowed_again(db, provider):
    from sqlalchemy import update

    seeded, _first = await _collect(db, provider)
    async with db() as session:
        await session.execute(
            update(PaymentAttentionItem).values(status="resolved", resolved_at=NOW)
        )
        await session.commit()

    result = await base._request(db, seeded, "Consulta")

    assert result.kind != "blocked"
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_a_released_slot_keeps_the_collect_item_until_the_refund_falls_due(db, provider):
    """Release (the patient may still keep the slot) does not close it; the refund-due settle
    (questions closed) does."""
    seeded, _first = await _collect(db, provider)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        await adjustment_queries.settle_open_adjustments(
            session, tenant=tenant, deposit=deposit, now=NOW, close_questions=False
        )
        await session.commit()
    [item] = await base._items(db)
    assert item.status == "open"
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        await adjustment_queries.settle_open_adjustments(
            session, tenant=tenant, deposit=deposit, now=NOW, void_payments=False
        )
        await session.commit()
    [item] = await base._items(db)
    assert item.status == "resolved"


def test_the_patient_reply_says_the_difference_is_settled_with_the_clinic():
    from secretaria.ai.formatter import TextBubble

    outcome = service_change.ServiceChangeResult(
        kind="applied_now", diff_cents=3000, decision=service_change.DECISION_COLLECT
    )
    recap = TextBubble(body="Pronto")
    menu = TextBubble(body="menu")

    bubbles = service_change_bubbles([recap, menu], outcome, new_service="Avaliação", tenant=None)

    assert bubbles[0] is recap
    assert bubbles[1].body == service_change.collect_due_text("Avaliação", 3000)
    assert bubbles[2] is menu
    text = bubbles[1].body
    assert "R$ 30,00" in text and "clínica" in text and "Avaliação" in text
    assert "devol" not in text.lower() and "estorn" not in text.lower()
    hub = service_change.collect_due_text("Avaliação", 3000, source="hub")
    assert "R$ 30,00" in hub and "clínica" in hub


# --------------------------------------------------------------------------
# B2: cancelled with the provider down -> the void is retried by the sweep
# --------------------------------------------------------------------------


async def _expire(db, now, enqueue=None) -> int:
    async with db() as session:
        return await service_change.expire_adjustments(session, now=now, enqueue=enqueue)


async def test_cancel_with_the_provider_down_marks_the_void_pending_then_the_sweep_voids(
    db, provider
):
    seeded, _created = await cancel._awaiting(db, provider)
    provider.calls.clear()
    provider.get_error = ProviderError("down", status_code=503)

    outcome, notice = await cancel._cancel(db, seeded)

    assert outcome == "refund_due"
    assert notice.endswith(cancel.VOIDED_LINE)  # the patient is told not to pay it
    [adjustment] = await base._adjustments(db)
    assert adjustment.status == STATUS_CANCELLED_VOID_PENDING
    assert provider.called("cancel_charge") == 0

    # Still down: the sweep leaves it for the next tick.
    assert await _expire(db, NOW + timedelta(minutes=12)) == 0
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED_VOID_PENDING

    # Back up: the sweep checks the charge, voids it, and stops retrying.
    provider.get_error = None
    assert await _expire(db, NOW + timedelta(minutes=14)) == 1
    [adjustment] = await base._adjustments(db)
    assert adjustment.status == STATUS_CANCELLED
    assert provider.called("cancel_charge") == 1
    assert await _expire(db, NOW + timedelta(minutes=16)) == 0
    assert provider.called("cancel_charge") == 1
    # Never money held, never a second refund item.
    assert cancel._by_reason(await base._items(db)) == {REASON_REFUND_DUE_POLICY: [6000]}
    assert await _held(db, seeded[3].id) == 6000


async def test_a_failed_void_at_cancel_is_retried_until_it_succeeds(db, provider):
    seeded, _created = await cancel._awaiting(db, provider)
    provider.cancel_error = ProviderError("boom", status_code=500, retryable=True)

    await cancel._cancel(db, seeded)
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED_VOID_PENDING

    assert await _expire(db, NOW + timedelta(minutes=12)) == 0
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED_VOID_PENDING
    provider.cancel_error = None
    assert await _expire(db, NOW + timedelta(minutes=14)) == 1
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED


async def test_a_pending_void_paid_meanwhile_is_reconciled_as_an_orphan(db, provider):
    seeded, created = await cancel._awaiting(db, provider)
    provider.get_error = ProviderError("down", status_code=503)
    await cancel._cancel(db, seeded)
    provider.get_error = None
    hook._paid(provider, created)
    provider.calls.clear()
    sent, enqueue = recording_enqueue()

    assert await _expire(db, NOW + timedelta(minutes=14), enqueue) == 1

    assert provider.called("cancel_charge") == 0  # a paid charge is never deleted
    [(job, _row_id)] = sent
    assert job == "process_payment_event"
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED
    # The payment itself: an ORPHAN item for the clinic, never a service change.
    result = await hook._process(db, seeded[0])
    assert result.outcome == "adjustment_paid_late"
    assert REASON_ORPHAN_PAYMENT in cancel._by_reason(await base._items(db))
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_a_late_payment_while_the_void_is_pending_is_an_orphan(db, provider):
    seeded, created = await cancel._awaiting(db, provider)
    provider.get_error = ProviderError("down", status_code=503)
    await cancel._cancel(db, seeded)
    provider.get_error = None
    hook._paid(provider, created)

    result = await hook._process(db, seeded[0])

    assert result.outcome == "adjustment_paid_late"
    assert result.applied_adjustment_id is None
    assert cancel._by_reason(await base._items(db)) == {
        REASON_REFUND_DUE_POLICY: [6000],
        REASON_ORPHAN_PAYMENT: [None],
    }
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_the_void_retry_is_scoped_to_its_own_tenant_account(db, provider):
    seeded, _created = await cancel._awaiting(db, provider)
    provider.get_error = ProviderError("down", status_code=503)
    await cancel._cancel(db, seeded)
    provider.get_error = None
    provider.tenants_seen.clear()

    await _expire(db, NOW + timedelta(minutes=14))

    assert set(provider.tenants_seen) == {seeded[0].id}


async def test_voided_hint_counts_a_pending_void(db, provider):
    seeded, _created = await cancel._awaiting(db, provider)
    provider.get_error = ProviderError("down", status_code=503)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        settled = await adjustment_queries.settle_open_adjustments(
            session, tenant=tenant, deposit=deposit, now=NOW
        )
        assert list(settled.values()) == [adjustment_queries.SETTLED_CANCELLED_UNVOIDED]
        assert adjustment_queries.voided_difference_hint(deposit) == 3000
        await session.commit()
    async with db() as session:
        items = (await session.scalars(select(PaymentAttentionItem))).all()
    assert items == []


# --------------------------------------------------------------------------
# follow-up review minor 1: the void retry never loops forever
# --------------------------------------------------------------------------


async def _void_pending(db, provider):
    seeded, created = await cancel._awaiting(db, provider)
    provider.get_error = ProviderError("down", status_code=503, retryable=True)
    await cancel._cancel(db, seeded)
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED_VOID_PENDING
    provider.get_error = None
    provider.calls.clear()
    return seeded, created


async def test_a_charge_the_provider_no_longer_knows_stops_the_retry(db, provider):
    await _void_pending(db, provider)
    provider.get_error = ProviderError("get_charge_failed", status_code=404)

    assert await _expire(db, NOW + timedelta(minutes=14)) == 1
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED
    assert provider.called("cancel_charge") == 0
    calls = len(provider.calls)
    await _expire(db, NOW + timedelta(minutes=16))
    assert len(provider.calls) == calls  # final: never asked again


async def test_an_already_refunded_charge_stops_the_retry_without_deleting(db, provider):
    await _void_pending(db, provider)
    provider.state = ChargeState(status=PaymentStatus.REFUNDED)

    assert await _expire(db, NOW + timedelta(minutes=14)) == 1
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED
    assert provider.called("cancel_charge") == 0


async def test_a_delete_the_provider_refuses_for_good_stops_the_retry(db, provider):
    await _void_pending(db, provider)
    provider.cancel_error = ProviderError("cancel_failed", status_code=400)

    assert await _expire(db, NOW + timedelta(minutes=14)) == 1
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED
    calls = len(provider.calls)
    await _expire(db, NOW + timedelta(minutes=16))
    assert len(provider.calls) == calls


async def test_an_outage_that_never_ends_gives_up_after_the_retry_window(db, provider):
    await _void_pending(db, provider)
    provider.get_error = ProviderError("down", status_code=503, retryable=True)

    assert await _expire(db, NOW + timedelta(days=1)) == 0
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED_VOID_PENDING
    window = service_change.VOID_RETRY_WINDOW
    assert await _expire(db, NOW + window + timedelta(hours=1)) == 1
    assert (await base._adjustments(db))[0].status == STATUS_CANCELLED
    calls = len(provider.calls)
    await _expire(db, NOW + window + timedelta(hours=2))
    assert len(provider.calls) == calls
