"""Cancelling a consultation whose sinal had a service-change adjustment (plan 2026-10-26, Task 8).

Review focus 3: cancelling with a difference Pix still unpaid VOIDS that Pix and keeps ONE
coherent refund. What the clinic holds for a sinal (`service_change.held_deposit_cents`) is the
original amount + every PAID difference - every difference the patient already asked back (that
money has its own `service_diff_refund_due` item). Every cancellation figure (the suggested
refund, the partial retention, the notices, the warnings) uses that one number, so the items of a
deposit never add up to more than the patient paid. Nothing here ever returns money.

Seed (tests/test_service_change.py): sinal R$ 60 of "Consulta"; "Avaliação" = +R$ 30 (Pix of the
difference), "Retorno" = -R$ 30 (asks, 5 days ahead).
"""

from datetime import timedelta

from secretaria.models import Appointment, AppointmentStatus, PixDeposit, Tenant
from secretaria.models.deposit_adjustment import (
    STATUS_APPLIED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_CANCELLED_VOID_PENDING,
    STATUS_KEPT_CREDIT,
    STATUS_REFUND_REQUESTED,
)
from secretaria.models.payment_attention_item import (
    REASON_ORPHAN_PAYMENT,
    REASON_PAID_THEN_CANCELLED,
    REASON_REFUND_DUE_POLICY,
    REASON_RELEASED_REFUND_DUE,
    REASON_SERVICE_DIFF_REFUND_DUE,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import release_offer
from secretaria.services.payments import adjustment_queries, deposit_lifecycle, service_change
from secretaria.services.payments.provider import ProviderError
from secretaria.workers.shared.deposit import _cancel_warning_line
from tests import test_service_change as base, test_service_change_webhook as hook

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks

_adjustments = base._adjustments
_items = base._items


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _seed(db, **kw):
    async with db() as session:
        return await base._seed(session, **kw)


async def _awaiting(db, provider, **kw):
    seeded = await _seed(db, **kw)
    created = await base._request(db, seeded, "Avaliação", minutes=60)
    assert created.kind == "awaiting_payment"
    return seeded, created


async def _paid_difference(db, provider, **kw):
    """A sinal of R$ 60 plus a PAID difference of R$ 30: the clinic holds R$ 90."""
    seeded, created = await _awaiting(db, provider, **kw)
    hook._paid(provider, created)
    result = await hook._process(db, seeded[0])
    assert result.outcome == "adjustment_applied"
    provider.calls.clear()
    return seeded


async def _decrease_asked(db, **kw):
    seeded = await _seed(db, **kw)
    asked = await base._request(db, seeded, "Retorno", minutes=20)
    assert asked.kind == "asked" and asked.diff_cents == 3000
    return seeded, asked


async def _answer(db, seeded, adjustment_id, *, accept: bool) -> str:
    async with db() as session:
        outcome = await service_change.answer_refund_question(
            session,
            tenant_id=seeded[0].id,
            adjustment_id=adjustment_id,
            accept=accept,
            now=NOW + timedelta(minutes=1),
        )
        await session.commit()
        return outcome


async def _cancel(db, seeded, *, by=deposit_lifecycle.CANCELLED_BY_PATIENT, now=None, **tenant_kw):
    """Cancel the consultation (row CANCELLED, then the money hook) like every cancel surface."""
    tenant, _patient, appointment, _deposit = seeded
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        for key, value in tenant_kw.items():
            setattr(tenant, key, value)
        appointment = await session.get(Appointment, appointment.id)
        appointment.status = AppointmentStatus.CANCELLED
        outcome = await deposit_lifecycle.on_appointment_cancelled(
            session,
            tenant=tenant,
            appointment=appointment,
            now=now or NOW + timedelta(minutes=10),
            cancelled_by=by,
        )
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
        notice = deposit_lifecycle.cancellation_notice(outcome, tenant, deposit)
        await session.commit()
        return outcome, notice


def _by_reason(items) -> dict[str, list[int | None]]:
    out: dict[str, list[int | None]] = {}
    for item in items:
        out.setdefault(item.reason, []).append(item.suggested_refund_cents)
    return out


def _no_money_out(provider) -> None:
    assert {call[0] for call in provider.calls} <= {"get_charge", "cancel_charge"}


# --------------------------------------------------------------------------
# 1. an unpaid difference Pix is voided by the cancellation
# --------------------------------------------------------------------------


async def test_cancelling_with_an_unpaid_difference_voids_its_pix_and_cancels_the_adjustment(
    db, provider
):
    seeded, _created = await _awaiting(db, provider)
    provider.calls.clear()

    outcome, _ = await _cancel(db, seeded)

    assert outcome == "refund_due"
    assert [c for c in provider.calls] == [
        ("get_charge", "pay_diff_1"),
        ("cancel_charge", "pay_diff_1"),
    ]
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_CANCELLED and adjustment.resolved_at is not None
    # The unpaid difference was never money held: only the R$ 60 is due back.
    assert _by_reason(await _items(db)) == {REASON_REFUND_DUE_POLICY: [6000]}
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_a_difference_paid_after_the_cancellation_is_an_orphan_never_a_service_change(
    db, provider
):
    seeded, created = await _awaiting(db, provider)
    await _cancel(db, seeded)

    hook._paid(provider, created)
    result = await hook._process(db, seeded[0])

    assert result.outcome == "adjustment_paid_late"
    assert _by_reason(await _items(db)) == {
        REASON_REFUND_DUE_POLICY: [6000],
        REASON_ORPHAN_PAYMENT: [None],
    }
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_a_difference_already_paid_at_the_provider_is_never_deleted(db, provider):
    seeded, created = await _awaiting(db, provider)
    hook._paid(provider, created)
    provider.calls.clear()

    outcome, _ = await _cancel(db, seeded)

    assert outcome == "refund_due"
    assert provider.called("cancel_charge") == 0
    # Left for the webhook / the sweep: the consultation is gone, so it becomes an ORPHAN item.
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_AWAITING_PAYMENT
    result = await hook._process(db, seeded[0])
    assert result.outcome == "adjustment_not_applicable"
    assert _by_reason(await _items(db)) == {
        REASON_REFUND_DUE_POLICY: [6000],
        REASON_ORPHAN_PAYMENT: [None],
    }
    assert (await base._appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_an_unknown_provider_state_cancels_the_adjustment_without_deleting(db, provider):
    seeded, _created = await _awaiting(db, provider)
    provider.calls.clear()
    provider.get_error = ProviderError("down", status_code=503)

    await _cancel(db, seeded)

    assert provider.called("cancel_charge") == 0
    # Cancelled anyway: a payment that still lands is an ORPHAN item (Task 5), never applied.
    # Follow-up B (2026-10-27): the void is retried by the sweep (provider_down tests).
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_CANCELLED_VOID_PENDING


async def test_a_failed_void_still_cancels_the_adjustment(db, provider):
    seeded, _created = await _awaiting(db, provider)
    provider.cancel_error = ProviderError("boom", status_code=500)

    await _cancel(db, seeded)

    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_CANCELLED_VOID_PENDING


VOIDED_LINE = "O Pix da diferença do sinal (R$ 30,00) foi cancelado: não pague esse código."


async def test_the_cancel_notice_tells_the_patient_the_difference_pix_no_longer_counts(
    db, provider
):
    """Browser proof 2026-10-26: the patient was never told the pending Pix stopped counting."""
    seeded, _created = await _awaiting(db, provider)

    outcome, notice = await _cancel(db, seeded)

    assert outcome == "refund_due"
    assert notice.endswith(VOIDED_LINE)
    assert "clínica fará a devolução do seu sinal" in notice  # the sinal's own line is kept


async def test_an_unreachable_provider_still_tells_the_patient_not_to_pay(db, provider):
    seeded, _created = await _awaiting(db, provider)
    provider.get_error = ProviderError("down", status_code=503)

    _outcome, notice = await _cancel(db, seeded)

    assert notice.endswith(VOIDED_LINE)  # the adjustment is cancelled: a late payment is ORPHAN


async def test_a_difference_paid_meanwhile_is_not_called_cancelled(db, provider):
    seeded, created = await _awaiting(db, provider)
    hook._paid(provider, created)

    _outcome, notice = await _cancel(db, seeded)

    assert "Pix da diferença" not in notice


async def test_without_a_pending_difference_the_cancel_notice_is_unchanged(db, provider):
    seeded = await _paid_difference(db, provider)

    _outcome, notice = await _cancel(db, seeded, by=deposit_lifecycle.CANCELLED_BY_CLINIC)

    assert notice == "A clínica fará a devolução do seu sinal; você será avisado."


async def test_the_voided_difference_is_readable_after_the_cancel(db, provider):
    seeded, _created = await _awaiting(db, provider)
    tenant, _patient, appointment, _deposit = seeded
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        appointment = await session.get(Appointment, appointment.id)
        appointment.status = AppointmentStatus.CANCELLED
        await deposit_lifecycle.on_appointment_cancelled(
            session, tenant=tenant, appointment=appointment, now=NOW + timedelta(minutes=10)
        )
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
        assert adjustment_queries.voided_difference_hint(deposit) == 3000
    assert adjustment_queries.voided_difference_hint(PixDeposit(amount_cents=6000)) == 0


async def test_cancelling_twice_settles_the_adjustment_once_and_opens_one_item(db, provider):
    seeded, _created = await _awaiting(db, provider)
    first, _ = await _cancel(db, seeded)
    provider.calls.clear()
    second, _ = await _cancel(db, seeded, by=deposit_lifecycle.CANCELLED_BY_CLINIC)

    assert (first, second) == ("refund_due", None)
    assert provider.calls == []
    assert len(await _items(db)) == 1


# --------------------------------------------------------------------------
# 2. after a PAID difference the refund is the total the clinic holds
# --------------------------------------------------------------------------


async def test_a_clinic_cancellation_after_a_paid_difference_asks_back_the_total(db, provider):
    seeded = await _paid_difference(db, provider)

    outcome, notice = await _cancel(db, seeded, by=deposit_lifecycle.CANCELLED_BY_CLINIC)

    assert outcome == "refund_due"
    assert _by_reason(await _items(db)) == {REASON_PAID_THEN_CANCELLED: [9000]}
    assert "clínica fará a devolução" in notice
    _no_money_out(provider)


async def test_an_early_patient_cancellation_after_a_paid_difference_asks_back_the_total(
    db, provider
):
    seeded = await _paid_difference(db, provider)

    outcome, _ = await _cancel(db, seeded)

    assert outcome == "refund_due"
    assert _by_reason(await _items(db)) == {REASON_REFUND_DUE_POLICY: [9000]}
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_APPLIED  # a paid difference is never "voided"
    _no_money_out(provider)


async def test_partial_retention_applies_to_the_total(db, provider):
    seeded = await _paid_difference(db, provider, days_ahead=0.05)  # ~1 h ahead

    outcome, notice = await _cancel(
        db, seeded, pix_retention_policy="partial", pix_partial_refund_percent=40
    )

    assert outcome == "partial_refund_due"
    assert _by_reason(await _items(db)) == {REASON_REFUND_DUE_POLICY: [3600]}  # 40 % of R$ 90
    assert "R$ 36,00" in notice


async def test_total_retention_keeps_everything_and_asks_nothing(db, provider):
    seeded = await _paid_difference(db, provider, days_ahead=0.05)

    outcome, _ = await _cancel(db, seeded)

    assert outcome == "retained"
    assert await _items(db) == []
    async with db() as session:
        deposit = await session.get(PixDeposit, seeded[3].id)
    assert deposit.status == PixDepositStatus.CANCELLED_RETAINED
    assert deposit.amount_cents == 6000  # the sinal row itself never changes


# --------------------------------------------------------------------------
# 3. a cheaper service: one refund, never two
# --------------------------------------------------------------------------


async def test_a_refund_already_requested_is_not_counted_twice(db, provider):
    seeded, asked = await _decrease_asked(db)
    assert await _answer(db, seeded, asked.adjustment_id, accept=True) == STATUS_REFUND_REQUESTED

    outcome, _ = await _cancel(db, seeded, by=deposit_lifecycle.CANCELLED_BY_CLINIC)

    assert outcome == "refund_due"
    by_reason = _by_reason(await _items(db))
    assert by_reason == {
        REASON_SERVICE_DIFF_REFUND_DUE: [3000],
        REASON_PAID_THEN_CANCELLED: [3000],  # R$ 60 - the R$ 30 already asked back
    }
    assert sum(v for values in by_reason.values() for v in values) == 6000  # what was paid
    _no_money_out(provider)


async def test_a_pending_refund_question_is_closed_by_the_cancellation(db, provider):
    seeded, asked = await _decrease_asked(db)

    outcome, _ = await _cancel(db, seeded)

    assert outcome == "refund_due"
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_CANCELLED
    assert _by_reason(await _items(db)) == {REASON_REFUND_DUE_POLICY: [6000]}
    # A late "Sim, quero de volta" can no longer open a second item for the same money.
    assert await _answer(db, seeded, asked.adjustment_id, accept=True) == "already_answered"
    assert len(await _items(db)) == 1


async def test_a_difference_kept_as_credit_is_part_of_the_refund(db, provider):
    seeded, asked = await _decrease_asked(db)
    assert await _answer(db, seeded, asked.adjustment_id, accept=False) == STATUS_KEPT_CREDIT

    await _cancel(db, seeded)

    assert _by_reason(await _items(db)) == {REASON_REFUND_DUE_POLICY: [6000]}


# --------------------------------------------------------------------------
# texts: preview, notices, warnings read the same total
# --------------------------------------------------------------------------


async def test_the_deposit_lookup_carries_the_total_for_every_text(db, provider):
    seeded = await _paid_difference(db, provider, days_ahead=0.05)
    tenant, _patient, appointment, deposit = seeded
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        tenant.pix_retention_policy = "partial"
        tenant.pix_partial_refund_percent = 50
        appointment = await session.get(Appointment, appointment.id)
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
        assert adjustment_queries.held_cents_hint(deposit) == 9000
        assert deposit.amount_cents == 6000

        preview = deposit_lifecycle.preview_cancellation_outcome(
            tenant, deposit, appointment, now=NOW
        )
        warning = deposit_lifecycle.release_warning_text(preview, tenant, deposit)
        line = _cancel_warning_line(tenant, deposit, appointment, NOW)

    assert preview == "partial_refund_due"
    assert "R$ 90,00" in warning and "R$ 45,00" in warning
    assert "R$ 45,00" in line  # 50 % of R$ 90
    assert "R$ 90,00" in release_offer.warning_text(deposit)


def test_without_a_lookup_the_texts_fall_back_to_the_sinal():
    tenant = Tenant(clinic_name="C", pix_refund_window_hours=24, pix_partial_refund_percent=50)
    deposit = PixDeposit(amount_cents=6000, percent_applied=30)
    assert adjustment_queries.held_cents_hint(deposit) == 6000
    notice = deposit_lifecycle.cancellation_notice("partial_refund_due", tenant, deposit)
    assert "R$ 30,00" in notice


async def test_held_total_matches_the_service_change_definition(db, provider):
    seeded = await _paid_difference(db, provider)
    async with db() as session:
        deposit = await session.get(PixDeposit, seeded[3].id)
        assert await adjustment_queries.held_total_cents(session, deposit) == (
            await service_change.held_deposit_cents(session, deposit)
        ) == 9000


# --------------------------------------------------------------------------
# the clinic RELEASES a paid slot (TASK-052 offer): same rules
# --------------------------------------------------------------------------


async def _release(db, seeded):
    tenant, _patient, appointment, _deposit = seeded
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        appointment = await session.get(Appointment, appointment.id)
        appointment.status = AppointmentStatus.CANCELLED
        deposit = await deposit_lifecycle.get_deposit_for_appointment(session, appointment.id)
        offer = await release_offer.open_offer(
            session, tenant=tenant, appointment=appointment, deposit=deposit, now=NOW
        )
        body = release_offer.offer_body(
            deposit=deposit, decide_by_at=offer.decide_by_at, timezone=tenant.timezone,
            justification=None, confirmed=False,
        )
        await session.commit()
        return offer, body


async def _choose_cancel(db, offer):
    async with db() as session:
        offer = await session.get(type(offer), offer.id)
        outcome = await release_offer.choose_cancel(
            session, offer=offer, now=NOW + timedelta(minutes=5)
        )
        await session.commit()
        return outcome


async def test_releasing_a_paid_slot_voids_an_unpaid_difference(db, provider):
    seeded, _created = await _awaiting(db, provider)
    provider.calls.clear()

    await _release(db, seeded)

    assert provider.called("cancel_charge") == 1
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_CANCELLED


async def test_the_release_refund_is_the_total_held(db, provider):
    seeded = await _paid_difference(db, provider)

    offer, body = await _release(db, seeded)
    await _choose_cancel(db, offer)

    assert "R$ 90,00" in body
    assert _by_reason(await _items(db)) == {REASON_RELEASED_REFUND_DUE: [9000]}
    _no_money_out(provider)


async def test_the_release_refund_closes_a_pending_question_and_never_double_counts(db, provider):
    seeded, asked = await _decrease_asked(db)
    offer, _body = await _release(db, seeded)
    await _choose_cancel(db, offer)

    assert _by_reason(await _items(db)) == {REASON_RELEASED_REFUND_DUE: [6000]}
    assert await _answer(db, seeded, asked.adjustment_id, accept=True) == "already_answered"
    assert len(await _items(db)) == 1
