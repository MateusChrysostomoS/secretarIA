"""TASK-085 R / S1 - a difference Pix that was NOT paid, and the patient's three buttons.

Contract: tasks/TASK-085/sinal-trocas/contrato.md §2.2 / §3.6. Pinned here:

* `on_difference_unpaid` (the sweep and the webhook EXPIRED): provider first (paid -> nothing;
  void failed -> nothing), then `suspend` -> `awaiting_choice` + the three buttons (or `kept_old`
  without room), `applied` -> `collect_due` + ONE `service_diff_unpaid` item + a notice;
* silence until the choice deadline -> `kept_old` (D-R8), the new window freed;
* `choose_after_expiry`: keep / resend / both, idempotent, scoped by clinic;
* `settle_open_adjustments` closes a choice and frees the hold (cancellation, D-R9 "liberar");
* legacy rows (`mode` NULL) keep today's expiry.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    PaymentAttentionItem,
    PaymentWebhookEvent,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    KIND_INCREASE,
    MODE_SUSPEND,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_COLLECT_DUE,
    STATUS_EXPIRED,
    STATUS_KEPT_OLD,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    REASON_ORPHAN_PAYMENT,
    REASON_SERVICE_DIFF_UNPAID,
)
from secretaria.services import booking_hold
from secretaria.services.payments import (
    adjustment_queries,
    adjustment_suspension,
    event_processor,
    notify,
    service_change,
)
from secretaria.services.payments.attention_view import reason_text
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.services.payments.webhook_payload import event_to_dict
from tests import test_service_change as base
from tests.payments_support import make_event, recording_enqueue
from tests.test_sinal_trocas_s1_engine import _seed

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks

CLOCK = {"now": NOW}


@pytest.fixture(autouse=True)
def hold_clock(monkeypatch):
    CLOCK["now"] = NOW
    monkeypatch.setattr(booking_hold, "_now", lambda: CLOCK["now"])


@pytest.fixture
def sent(monkeypatch):
    """Every patient message of the engine (`notify.deliver`), recorded."""
    out: list = []

    async def fake_deliver(session, tenant, patient, messages, **kw):
        out.append((patient.id, list(messages)))

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    return out


@pytest.fixture
def followups(monkeypatch):
    seen: list = []

    async def fake_replan(appointment_id, **kw):
        seen.append(appointment_id)
        return 0

    from secretaria.services import reminder_hooks

    monkeypatch.setattr(reminder_hooks, "after_appointment_rescheduled", fake_replan)
    return seen


async def _requested(db, *, timing="before", days_ahead: float = 5, new_offset=timedelta(days=1)):
    seeded = await _seed(db, timing=timing, days_ahead=days_ahead)
    appointment = seeded[2]
    new_start = appointment.start_at + new_offset
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, appointment.id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        result = await service_change.request_paid_change(
            session, tenant=tenant, appointment=row, deposit=deposit, new_service="Avaliação",
            pending_edit={
                **base._edit(row, "Avaliação", 60),
                "start_at": new_start,
                "end_at": new_start + timedelta(minutes=60),
                "time_changed": new_offset != timedelta(0),
            },
            now=NOW,
        )
        await session.commit()
    return seeded, result, new_start


async def _adjustment(db, adjustment_id) -> DepositAdjustment:
    async with db() as session:
        return await session.get(DepositAdjustment, adjustment_id)


async def _all(db, model):
    async with db() as session:
        return list((await session.scalars(select(model))).all())


async def _sweep(db, at, enqueue=None):
    CLOCK["now"] = at
    async with db() as session:
        return await service_change.expire_adjustments(session, now=at, enqueue=enqueue)


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=NOW.tzinfo)


def _buttons(sent) -> list[str]:
    [(_patient, [message])] = sent
    assert isinstance(message, notify.Buttons)
    return [button_id.split("|")[0] for button_id, _label in message.buttons]


# --------------------------------------------------------------------------
# the sweep: suspend
# --------------------------------------------------------------------------


async def test_an_expired_suspended_pix_is_voided_and_the_patient_chooses(
    db, provider, sent, followups
):
    _seeded, created, new_start = await _requested(db)
    assert created.kind == "suspended"
    at = NOW + timedelta(minutes=31)

    assert await _sweep(db, NOW + timedelta(minutes=29)) == 0
    assert await _sweep(db, at) == 1

    adjustment = await _adjustment(db, created.adjustment_id)
    assert adjustment.status == STATUS_AWAITING_CHOICE
    assert adjustment.resolved_at is None
    assert _utc(adjustment.asked_at) == at
    assert _utc(adjustment.expires_at) == at + timedelta(hours=24)
    assert provider.called("cancel_charge") == 1
    [hold] = await _all(db, BookingHold)
    assert hold.adjustment_id == adjustment.id
    assert _utc(hold.expires_at) == at + timedelta(hours=24, minutes=15)
    assert _buttons(sent) == ["svcdiffkeep", "svcdiffresend", "svcdiffboth"]
    [(_p, [message])] = sent
    assert "Manter a antiga" in message.body
    assert all(button_id.endswith(str(adjustment.id)) for button_id, _ in message.buttons)
    # Still suspended for the patient; a second sweep sends nothing new.
    async with db() as session:
        assert await adjustment_suspension.is_suspended(
            session, tenant_id=adjustment.tenant_id, appointment_id=adjustment.appointment_id
        )
    assert await _sweep(db, at + timedelta(minutes=5)) == 0
    assert len(sent) == 1
    assert followups == []


async def test_without_room_for_a_choice_the_old_visit_stands_at_once(
    db, provider, sent, followups
):
    # The visit in 2 h: the Pix runs until +30 min; at +56 min the cutoff (+55) has passed.
    _seeded, created, _new = await _requested(
        db, days_ahead=2 / 24, new_offset=timedelta(minutes=30)
    )
    assert created.kind == "suspended"
    assert await _sweep(db, NOW + timedelta(minutes=56)) == 1

    adjustment = await _adjustment(db, created.adjustment_id)
    assert adjustment.status == STATUS_KEPT_OLD
    assert await _all(db, BookingHold) == []
    [(_p, [message])] = sent
    assert isinstance(message, notify.Text) and "Mantivemos" in message.body
    assert followups == [adjustment.appointment_id]


async def test_without_a_useful_resend_the_buttons_leave_reenviar_out(db, provider, sent):
    # The NEW visit at +110 min: cutoff +45 min. The Pix runs until +30 min; at +31 min a choice
    # still fits (until +45) but a new useful Pix does not (14 min < 30 min).
    _seeded, created, _new = await _requested(
        db, new_offset=timedelta(minutes=110) - timedelta(days=5)
    )
    assert created.kind == "suspended"
    assert await _sweep(db, NOW + timedelta(minutes=31)) == 1
    adjustment = await _adjustment(db, created.adjustment_id)
    assert adjustment.status == STATUS_AWAITING_CHOICE
    assert _utc(adjustment.expires_at) == NOW + timedelta(minutes=45)
    assert _buttons(sent) == ["svcdiffkeep", "svcdiffboth"]


async def test_a_suspended_pix_paid_without_webhook_is_reconciled_not_expired(db, provider, sent):
    _seeded, created, _new = await _requested(db)
    provider.state = ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=3000,
        external_reference=str(created.adjustment_id),
    )
    jobs_sent, enqueue = recording_enqueue()
    assert await _sweep(db, NOW + timedelta(minutes=31), enqueue) == 0
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_AWAITING_PAYMENT
    assert provider.called("cancel_charge") == 0
    assert [job for job, _ in jobs_sent] == ["process_payment_event"]
    assert sent == []


async def test_a_void_that_fails_changes_nothing(db, provider, sent):
    _seeded, created, _new = await _requested(db)
    provider.cancel_error = ProviderError("down", status_code=503, retryable=True)
    assert await _sweep(db, NOW + timedelta(minutes=31)) == 0
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_AWAITING_PAYMENT
    assert sent == []


# --------------------------------------------------------------------------
# the sweep: applied (D-R5)
# --------------------------------------------------------------------------


async def test_an_expired_applied_pix_becomes_a_difference_to_collect(db, provider, sent):
    seeded, created, _new = await _requested(db, timing="after", new_offset=timedelta(0))
    assert created.kind == "applied_awaiting_payment"
    assert await _sweep(db, NOW + timedelta(minutes=31)) == 1

    adjustment = await _adjustment(db, created.adjustment_id)
    assert adjustment.status == STATUS_COLLECT_DUE
    [item] = await _all(db, PaymentAttentionItem)
    assert (item.reason, item.amount_cents, item.deposit_id) == (
        REASON_SERVICE_DIFF_UNPAID, 3000, None,
    )
    assert item.patient_id == seeded[1].id
    assert "R$ 30,00" in reason_text(item.reason, None, item.amount_cents)
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
    assert row.appointment_type == "Avaliação"  # the change stood
    [(_p, [message])] = sent
    assert "R$ 30,00" in message.body
    # The clinic still collecting it blocks the next change; a cancellation closes the item.
    async with db() as session:
        deposit = await session.get(PixDeposit, seeded[3].id)
        assert await adjustment_queries.open_collect_adjustments(
            session, tenant_id=seeded[0].id, deposit=deposit
        ) == [adjustment.id]
        tenant = await session.get(Tenant, seeded[0].id)
        await adjustment_queries.settle_open_adjustments(
            session, tenant=tenant, deposit=deposit, now=NOW + timedelta(hours=1)
        )
        await session.commit()
    [item] = await _all(db, PaymentAttentionItem)
    assert item.status == "resolved"


# --------------------------------------------------------------------------
# the webhook EXPIRED
# --------------------------------------------------------------------------


async def _webhook(db, tenant_id, status, *, event_id="evt_diff_x", at=None, amount=3000):
    at = at or NOW + timedelta(minutes=31)
    CLOCK["now"] = at
    event = make_event(status, event_id=event_id, charge_id="pay_diff_1", amount=amount)
    async with db() as session:
        row = PaymentWebhookEvent(
            tenant_id=tenant_id, provider="asaas", event_id=event.event_id,
            provider_charge_id=event.provider_charge_id, event_type=event.event_type,
            payload={"normalized": event_to_dict(event)},
        )
        session.add(row)
        await session.commit()
        result = await event_processor.process_payment_event_row(session, row.id, now=at)
        tenant = await session.get(Tenant, tenant_id)
        await event_processor.run_post_commit(session, tenant, result)
    return result


async def test_the_webhook_expired_puts_a_suspended_change_to_the_choice(db, provider, sent):
    seeded, created, _new = await _requested(db)
    result = await _webhook(db, seeded[0].id, PaymentStatus.EXPIRED)

    assert result.outcome == "adjustment_choice_pending"
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE
    assert provider.called("cancel_charge") == 1
    assert _buttons(sent) == ["svcdiffkeep", "svcdiffresend", "svcdiffboth"]
    again = await _webhook(db, seeded[0].id, PaymentStatus.EXPIRED, event_id="evt_diff_y")
    assert again.outcome == "adjustment_expired_replay"
    assert len(sent) == 1


async def test_a_payment_while_the_patient_chooses_is_an_orphan_and_changes_nothing(
    db, provider, sent
):
    seeded, created, _new = await _requested(db)
    await _sweep(db, NOW + timedelta(minutes=31))
    provider.state = ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=3000,
        external_reference=str(created.adjustment_id),
    )
    result = await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=40))
    assert result.outcome == "adjustment_paid_late"
    [item] = await _all(db, PaymentAttentionItem)
    assert item.reason == REASON_ORPHAN_PAYMENT
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE
    async with db() as session:
        assert (await session.get(Appointment, seeded[2].id)).appointment_type == "Consulta"


# --------------------------------------------------------------------------
# silence (D-R8)
# --------------------------------------------------------------------------


async def test_silence_until_the_deadline_keeps_the_old_visit(db, provider, sent, followups):
    _seeded, created, _new = await _requested(db)
    await _sweep(db, NOW + timedelta(minutes=31))
    sent.clear()
    deadline = NOW + timedelta(minutes=31, hours=24)

    assert await _sweep(db, deadline - timedelta(minutes=1)) == 0
    assert await _sweep(db, deadline + timedelta(minutes=1)) == 1

    adjustment = await _adjustment(db, created.adjustment_id)
    assert adjustment.status == STATUS_KEPT_OLD and adjustment.resolved_at is not None
    assert await _all(db, BookingHold) == []
    [(_p, [message])] = sent
    assert "Mantivemos" in message.body
    assert followups == [adjustment.appointment_id]
    assert await _sweep(db, deadline + timedelta(minutes=5)) == 0


# --------------------------------------------------------------------------
# choose_after_expiry
# --------------------------------------------------------------------------


async def _choice_ready(db):
    seeded, created, new_start = await _requested(db)
    await _sweep(db, NOW + timedelta(minutes=31))
    return seeded, created, new_start


async def _choose(db, tenant_id, adjustment_id, choice, *, at=None):
    at = at or NOW + timedelta(hours=1)
    CLOCK["now"] = at
    async with db() as session:
        outcome = await service_change.choose_after_expiry(
            session, tenant_id=tenant_id, adjustment_id=adjustment_id, choice=choice, now=at
        )
        await session.commit()
    return outcome


async def test_keep_closes_the_choice_and_frees_the_new_window(db, provider, sent):
    seeded, created, _new = await _choice_ready(db)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "keep")
    assert outcome.kind == "kept_old"
    assert (outcome.adjustment_id, outcome.appointment_id) == (created.adjustment_id, seeded[2].id)
    assert (outcome.old_service, outcome.new_service) == ("Consulta", "Avaliação")
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_KEPT_OLD
    assert await _all(db, BookingHold) == []
    again = await _choose(db, seeded[0].id, created.adjustment_id, "keep")
    assert again.kind == "already_decided"


async def test_both_closes_the_choice_and_hands_the_old_visit_to_the_caller(db, provider, sent):
    seeded, created, _new = await _choice_ready(db)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "both")
    assert (outcome.kind, outcome.appointment_id) == ("cancel_old", seeded[2].id)
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_CANCELLED
    assert await _all(db, BookingHold) == []
    async with db() as session:  # the engine never cancels the visit itself
        assert (await session.get(Appointment, seeded[2].id)).status == AppointmentStatus.SCHEDULED


async def test_resend_creates_a_new_pix_and_hands_the_hold_over(db, provider, sent):
    seeded, created, new_start = await _choice_ready(db)
    provider.created_charge = provider.created_charge.__class__(
        provider_charge_id="pay_diff_2", copy_paste="000201pix2"
    )
    at = NOW + timedelta(hours=1)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "resend", at=at)

    assert outcome.kind == "resent"
    assert (outcome.pix_copy_paste, outcome.diff_cents) == ("000201pix2", 3000)
    assert _utc(outcome.expires_at) == at + timedelta(minutes=30)
    old = await _adjustment(db, created.adjustment_id)
    new = await _adjustment(db, outcome.adjustment_id)
    assert old.status == STATUS_EXPIRED
    assert (new.status, new.mode, new.kind) == (
        STATUS_AWAITING_PAYMENT, MODE_SUSPEND, KIND_INCREASE,
    )
    assert new.pending_edit == old.pending_edit and new.diff_cents == old.diff_cents
    assert new.provider_charge_id == "pay_diff_2"
    [hold] = await _all(db, BookingHold)
    assert hold.adjustment_id == new.id
    assert _utc(hold.start_at) == new_start
    assert _utc(hold.expires_at) == at + timedelta(minutes=45)
    [(_n, request)] = [c for c in provider.calls if c[0] == "create_pix_charge"][1:]
    assert (request.amount_cents, request.external_reference) == (3000, str(new.id))


async def test_resend_with_the_hold_gone_and_the_window_taken_is_no_window(db, provider, sent):
    seeded, created, new_start = await _choice_ready(db)
    async with db() as session:
        await booking_hold.release_difference_hold_in(session, created.adjustment_id)
        session.add(
            Appointment(
                id=uuid4(), tenant_id=seeded[0].id, patient_id=seeded[1].id,
                google_event_id="evt-z", appointment_type="Consulta", start_at=new_start,
                end_at=new_start + timedelta(minutes=30), status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "resend")
    assert outcome.kind == "no_window"
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE
    assert provider.called("create_pix_charge") == 1


async def test_resend_when_the_provider_fails_changes_nothing(db, provider, sent):
    seeded, created, _new = await _choice_ready(db)
    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "resend")
    assert outcome.kind == "resend_failed"
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE
    [hold] = await _all(db, BookingHold)
    assert hold.adjustment_id == created.adjustment_id
    assert len(await _all(db, DepositAdjustment)) == 1


async def test_a_choice_of_another_clinic_or_after_the_deadline_changes_nothing(db, provider):
    seeded, created, _new = await _choice_ready(db)
    other = await _choose(db, uuid4(), created.adjustment_id, "keep")
    assert other.kind == "not_found"
    late = await _choose(
        db, seeded[0].id, created.adjustment_id, "keep", at=NOW + timedelta(hours=25)
    )
    assert late.kind == "already_decided"
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE


# --------------------------------------------------------------------------
# cancellation / release settle everything (D-R9)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("stage", ["awaiting_payment", "awaiting_choice"])
async def test_settling_closes_a_suspended_change_and_frees_its_hold(db, provider, stage):
    seeded, created, _new = await _requested(db)
    if stage == "awaiting_choice":
        await _sweep(db, NOW + timedelta(minutes=31))
    calls_before = provider.called("cancel_charge")
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        settled = await adjustment_queries.settle_open_adjustments(
            session, tenant=tenant, deposit=deposit, now=NOW + timedelta(hours=1),
            close_questions=False,
        )
        await session.commit()
    assert str(created.adjustment_id) in settled
    assert (await _adjustment(db, created.adjustment_id)).status == STATUS_CANCELLED
    assert await _all(db, BookingHold) == []
    voids = provider.called("cancel_charge") - calls_before
    assert voids == (1 if stage == "awaiting_payment" else 0)
    async with db() as session:
        assert not await adjustment_suspension.is_suspended(
            session, tenant_id=seeded[0].id, appointment_id=seeded[2].id
        )


async def test_the_agenda_totals_name_the_mode_of_the_pending_difference(db, provider):
    seeded, created, _new = await _requested(db)
    async with db() as session:
        totals = await adjustment_queries.load_adjustment_totals(
            session, seeded[0].id, [seeded[3].id]
        )
    assert totals[seeded[3].id].pending_difference_cents == 3000
    assert totals[seeded[3].id].pending_difference_mode == "suspend"


# --------------------------------------------------------------------------
# characterization: a legacy row (mode NULL) keeps today's expiry
# --------------------------------------------------------------------------


async def test_a_legacy_row_expires_like_today(db, provider, sent, monkeypatch):
    seen: list = []

    async def hook(session, tenant, notice):
        seen.append(notice.adjustment_id)

    monkeypatch.setattr(service_change, "ADJUSTMENT_EXPIRED_HOOKS", [hook])
    seeded = await _seed(db, timing="before")
    async with db() as session:
        legacy = DepositAdjustment(
            id=uuid4(), tenant_id=seeded[0].id, deposit_id=seeded[3].id,
            appointment_id=seeded[2].id, kind=KIND_INCREASE, status=STATUS_AWAITING_PAYMENT,
            old_service="Consulta", new_service="Avaliação", diff_cents=3000,
            provider_charge_id="pay_diff_1", expires_at=NOW + timedelta(minutes=30),
            pending_edit={"appointment_type": "Avaliação"},
        )
        session.add(legacy)
        await session.commit()
    assert await _sweep(db, NOW + timedelta(minutes=31)) == 1
    assert (await _adjustment(db, legacy.id)).status == STATUS_EXPIRED
    assert seen == [legacy.id]
    assert sent == []
