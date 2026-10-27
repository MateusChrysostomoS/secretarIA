"""The webhook of the DIFFERENCE Pix of a service change (event_processor + service_change).

Review focus (plan 2026-10-26, items 1, 2 and 5 live in test_service_change.py / here):
* a paid difference applies the pending edit ONCE - a redelivery, RECEIVED + CONFIRMED, or the
  reconciliation's synthetic event never write it twice;
* a difference paid AFTER it expired/was cancelled is an ORPHAN attention item, never a service
  changed in silence;
* the provider is the authority: status, reference and amount are checked before anything moves.
"""

from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    PaymentAttentionItem,
    PaymentWebhookEvent,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    STATUS_APPLIED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    REASON_AMOUNT_OVER,
    REASON_AMOUNT_UNDER,
    REASON_ORPHAN_PAYMENT,
    REASON_REFERENCE_MISMATCH,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from secretaria.services.payments import event_processor, service_change
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from secretaria.services.payments.webhook_payload import event_to_dict
from tests import test_service_change as base
from tests.payments_support import make_event

NOW = base.NOW
_request = base._request
_seed = base._seed
# The fixtures of the service-change module, reused (same seed, same scripted provider).
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks


async def _awaiting(db, provider, **seed_kw):
    async with db() as session:
        seeded = await _seed(session, **seed_kw)
    created = await _request(db, seeded, "Avaliação", minutes=60)
    assert created.kind == "awaiting_payment"
    return seeded, created


def _paid(provider, created, *, amount=3000, reference=None):
    provider.state = ChargeState(
        status=PaymentStatus.PAID,
        paid_amount_cents=amount,
        external_reference=reference if reference is not None else str(created.adjustment_id),
        paid_at=NOW + timedelta(minutes=5),
    )


async def _process(db, tenant, *, event_id="evt_diff_1", status=PaymentStatus.PAID, amount=3000):
    event = make_event(
        status, event_id=event_id, charge_id="pay_diff_1", amount=amount, reference=None
    )
    async with db() as session:
        row = PaymentWebhookEvent(
            tenant_id=tenant.id,
            provider="asaas",
            event_id=event.event_id,
            provider_charge_id=event.provider_charge_id,
            event_type=event.event_type,
            payload={"normalized": event_to_dict(event)},
        )
        session.add(row)
        await session.commit()
        return await event_processor.process_payment_event_row(
            session, row.id, now=NOW + timedelta(minutes=5)
        )


async def _adjustment(db) -> DepositAdjustment:
    async with db() as session:
        [row] = (await session.scalars(select(DepositAdjustment))).all()
        return row


async def _appointment(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


async def _notices(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(ProfessionalEditNotice))


async def test_paid_difference_applies_the_change_once(db, provider):
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created)

    first = await _process(db, seeded[0])
    again = await _process(db, seeded[0], event_id="evt_diff_confirmed")

    assert first.outcome == "adjustment_applied"
    assert first.applied_adjustment_id == created.adjustment_id
    assert again.outcome == "adjustment_already_applied"
    assert again.applied_adjustment_id is None
    adjustment = await _adjustment(db)
    assert adjustment.status == STATUS_APPLIED and adjustment.resolved_at is not None
    row = await _appointment(db, seeded[2].id)
    assert row.appointment_type == "Avaliação"
    assert (row.end_at - row.start_at) == timedelta(minutes=60)
    assert row.status == AppointmentStatus.SCHEDULED
    assert await _notices(db) == 1  # the doctor's "Consulta alterada", once
    assert await _items(db) == []
    async with db() as session:
        deposit = await session.get(PixDeposit, seeded[3].id)
    # The original sinal is untouched: the difference lives on the adjustment.
    assert (deposit.amount_cents, deposit.status) == (6000, PixDepositStatus.PAID)
    assert provider.called("get_charge") == 2


async def test_paid_difference_counts_for_the_next_change(db, provider):
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created)
    await _process(db, seeded[0])

    async with db() as session:
        deposit = await session.get(PixDeposit, seeded[3].id)
        assert await service_change.held_deposit_cents(session, deposit) == 9000
    # Back to Consulta (sinal R$ 60): R$ 30 cheaper than what is held -> asks.
    back = await _request(db, seeded, "Consulta")
    assert (back.kind, back.diff_cents) == ("asked", 3000)


async def test_difference_paid_after_it_expired_is_an_orphan_and_changes_nothing(db, provider):
    seeded, created = await _awaiting(db, provider)
    async with db() as session:
        await service_change.expire_adjustments(session, now=NOW + timedelta(minutes=31))
    assert (await _adjustment(db)).status == STATUS_EXPIRED
    _paid(provider, created)

    result = await _process(db, seeded[0])

    assert result.outcome == "adjustment_paid_late"
    assert result.applied_adjustment_id is None
    [item] = await _items(db)
    assert item.reason == REASON_ORPHAN_PAYMENT
    assert item.provider_charge_id == "pay_diff_1"
    assert item.paid_amount_cents == 3000
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"
    assert (await _adjustment(db)).status == STATUS_EXPIRED
    assert await _notices(db) == 0
    # A redelivery does not open a second item.
    await _process(db, seeded[0], event_id="evt_diff_again")
    assert len(await _items(db)) == 1


async def test_a_smaller_amount_is_refused_like_the_sinal(db, provider):
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, amount=2000)

    result = await _process(db, seeded[0], amount=2000)

    assert result.outcome == "adjustment_amount_under"
    [item] = await _items(db)
    assert (item.reason, item.paid_amount_cents) == (REASON_AMOUNT_UNDER, 2000)
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"
    assert (await _adjustment(db)).status == STATUS_CANCELLED


async def test_a_bigger_amount_applies_and_flags_the_excess(db, provider):
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, amount=5000)

    result = await _process(db, seeded[0], amount=5000)

    assert result.outcome == "adjustment_applied"
    [item] = await _items(db)
    assert (item.reason, item.paid_amount_cents) == (REASON_AMOUNT_OVER, 5000)
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Avaliação"


async def test_a_wrong_reference_changes_nothing(db, provider):
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, reference="someone-else")

    result = await _process(db, seeded[0])

    assert result.outcome == "adjustment_reference_mismatch"
    [item] = await _items(db)
    assert item.reason == REASON_REFERENCE_MISMATCH
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_a_forged_paid_event_is_not_confirmed_by_the_provider(db, provider):
    seeded, _created = await _awaiting(db, provider)
    # provider.state stays PENDING
    result = await _process(db, seeded[0])

    assert result.outcome == "ignored_not_paid_at_provider"
    assert (await _adjustment(db)).status == STATUS_AWAITING_PAYMENT
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_paid_for_a_cancelled_consultation_is_an_orphan(db, provider):
    seeded, created = await _awaiting(db, provider)
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        row.status = AppointmentStatus.CANCELLED
        await session.commit()
    _paid(provider, created)

    result = await _process(db, seeded[0])

    assert result.outcome == "adjustment_not_applicable"
    [item] = await _items(db)
    assert item.reason == REASON_ORPHAN_PAYMENT
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"
    assert (await _adjustment(db)).status == STATUS_CANCELLED


async def _move_row(db, appointment_id, delta: timedelta) -> Appointment:
    async with db() as session:
        row = await session.get(Appointment, appointment_id)
        row.start_at = row.start_at + delta
        row.end_at = row.end_at + delta
        await session.commit()
        return row


async def test_time_moved_before_payment_still_fits_applies_on_the_current_time(db, provider):
    seeded, created = await _awaiting(db, provider)
    moved = await _move_row(db, seeded[2].id, timedelta(days=1))
    _paid(provider, created)

    result = await _process(db, seeded[0])

    assert result.outcome == "adjustment_applied"
    row = await _appointment(db, seeded[2].id)
    assert row.appointment_type == "Avaliação"
    # The later reschedule wins: the service lands on the CURRENT start, with its own length.
    assert row.start_at.replace(tzinfo=None) == moved.start_at.replace(tzinfo=None)
    assert (row.end_at - row.start_at) == timedelta(minutes=60)
    assert await _items(db) == []
    assert (await _adjustment(db)).status == STATUS_APPLIED


async def test_time_moved_and_the_longer_service_no_longer_fits_is_an_orphan_and_told(
    db, provider, monkeypatch
):
    sent: list = []

    async def fake_deliver(session, tenant, patient, outgoing, **kw):
        sent.append((patient.id, [m.body for m in outgoing]))

    monkeypatch.setattr(service_change.notify, "deliver", fake_deliver)
    seeded, created = await _awaiting(db, provider)
    moved = await _move_row(db, seeded[2].id, timedelta(days=1))
    async with db() as session:
        # Another patient right after the (moved) 30-minute slot: 60 minutes no longer fit.
        session.add(
            Appointment(
                id=uuid4(), tenant_id=seeded[0].id, patient_id=seeded[1].id,
                google_event_id="evt-other", appointment_type="Consulta",
                start_at=moved.end_at, end_at=moved.end_at + timedelta(minutes=30),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    _paid(provider, created)

    result = await _process(db, seeded[0])

    assert result.outcome == "adjustment_not_applicable"
    assert result.not_applied_adjustment_id == created.adjustment_id
    [item] = await _items(db)
    assert item.reason == REASON_ORPHAN_PAYMENT
    row = await _appointment(db, seeded[2].id)
    assert row.appointment_type == "Consulta"
    assert (row.end_at - row.start_at) == timedelta(minutes=30)
    assert (await _adjustment(db)).status == STATUS_CANCELLED

    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        await event_processor.run_post_commit(session, tenant, result)
    [(patient_id, [text])] = sent
    assert patient_id == seeded[1].id
    assert "não foi possível" in text and "clínica" in text
    assert "devol" not in text.lower() and "estorn" not in text.lower()


async def test_a_convenio_changed_after_the_request_survives_the_payment(db, provider):
    seeded, created = await _awaiting(db, provider)
    plan_id = uuid4()
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        row.insurance = "Unimed"
        row.insurance_plan_id = plan_id
        row.attendee_name = "Filho"
        await session.commit()
    _paid(provider, created)

    result = await _process(db, seeded[0])

    assert result.outcome == "adjustment_applied"
    row = await _appointment(db, seeded[2].id)
    assert row.appointment_type == "Avaliação"
    assert (row.insurance, row.attendee_name) == ("Unimed", "Filho")
    assert row.insurance_plan_id == plan_id
    assert (row.end_at - row.start_at) == timedelta(minutes=60)


async def test_expired_event_of_an_unpaid_difference_expires_it(db, provider):
    seeded, _created = await _awaiting(db, provider)

    result = await _process(db, seeded[0], status=PaymentStatus.EXPIRED, event_id="evt_overdue")

    assert result.outcome == "adjustment_expired"
    assert result.expired_adjustment_id == (await _adjustment(db)).id
    assert (await _adjustment(db)).status == STATUS_EXPIRED
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_the_applied_change_runs_the_hooks_after_commit(db, provider, monkeypatch):
    seen: list = []

    async def hook(session, tenant, notice):
        seen.append(notice)

    monkeypatch.setattr(service_change, "ADJUSTMENT_APPLIED_HOOKS", [hook])
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created)
    result = await _process(db, seeded[0])

    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        await event_processor.run_post_commit(session, tenant, result)
    [notice] = seen
    assert (notice.adjustment_id, notice.new_service) == (created.adjustment_id, "Avaliação")


async def test_another_clinics_charge_id_is_never_matched(db, provider):
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created)
    stranger = Tenant(id=uuid4(), clinic_name="Other", phone_number_id=str(uuid4())[:12])
    async with db() as session:
        session.add(stranger)
        await session.commit()
    async with db() as session:
        stranger = await session.get(Tenant, stranger.id)
        found = await service_change.find_adjustment(
            session, tenant_id=stranger.id, charge_id="pay_diff_1"
        )
    assert found is None


def test_module_exports_the_contract():
    for name in ("request_service_change", "answer_refund_question", "expire_adjustments"):
        assert callable(getattr(service_change, name))


# --------------------------------------------------------------------------
# fix round 1: one item per charge across RECEIVED + CONFIRMED; a refund touches its own items
# --------------------------------------------------------------------------


async def test_underpaid_then_a_second_paid_event_keeps_exactly_one_item(db, provider):
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, amount=2000)

    first = await _process(db, seeded[0], amount=2000)
    second = await _process(
        db, seeded[0], event_id="evt_diff_confirmed", amount=2000
    )

    assert first.outcome == "adjustment_amount_under"
    assert second.outcome == "adjustment_paid_replay"
    [item] = await _items(db)
    assert item.reason == REASON_AMOUNT_UNDER
    assert (await _adjustment(db)).status == STATUS_CANCELLED
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_wrong_reference_then_a_replay_keeps_exactly_one_item(db, provider):
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, reference="someone-else")

    first = await _process(db, seeded[0])
    second = await _process(db, seeded[0], event_id="evt_diff_confirmed")

    assert first.outcome == "adjustment_reference_mismatch"
    assert second.outcome == "adjustment_paid_replay"
    [item] = await _items(db)
    assert item.reason == REASON_REFERENCE_MISMATCH


async def test_paid_after_expiry_opens_one_orphan_and_a_replay_adds_nothing(db, provider):
    seeded, created = await _awaiting(db, provider)
    async with db() as session:
        await service_change.expire_adjustments(session, now=NOW + timedelta(minutes=31))
    _paid(provider, created)

    first = await _process(db, seeded[0])
    second = await _process(db, seeded[0], event_id="evt_diff_confirmed")

    assert (first.outcome, second.outcome) == ("adjustment_paid_late", "adjustment_paid_replay")
    [item] = await _items(db)
    assert item.reason == REASON_ORPHAN_PAYMENT
    assert (await _adjustment(db)).status == STATUS_EXPIRED


async def test_a_refund_of_the_difference_stamps_only_its_own_items(db, provider):
    from secretaria.models.payment_attention_item import REASON_REFUND_DUE_POLICY
    from secretaria.services.payments import attention

    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, amount=5000)
    await _process(db, seeded[0], amount=5000)  # applied + AMOUNT_OVER item on the difference
    async with db() as session:
        deposit = await session.get(PixDeposit, seeded[3].id)
        await attention.open_attention_item(
            session,
            tenant_id=seeded[0].id,
            reason=REASON_REFUND_DUE_POLICY,
            dedupe_key="asaas:pay_sinal:refund_due_policy",
            deposit=deposit,
            suggested_refund_cents=6000,
        )
        await session.commit()

    result = await _process(
        db, seeded[0], status=PaymentStatus.REFUNDED, event_id="evt_diff_refund"
    )

    assert result.outcome == "adjustment_refund_observed"
    by_reason = {item.reason: item for item in await _items(db)}
    assert by_reason[REASON_AMOUNT_OVER].provider_refund_seen_at is not None
    sinal_item = by_reason[REASON_REFUND_DUE_POLICY]
    assert sinal_item.provider_refund_seen_at is None
    assert sinal_item.status == "open"


# --------------------------------------------------------------------------
# follow-up C (2026-10-27): an UNDERPAID difference - never applied, the clinic's item, and the
# patient told ONCE with the exact numbers (after the commit, the not-applied hook)
# --------------------------------------------------------------------------


def _recording_deliver(monkeypatch) -> list:
    sent: list = []

    async def fake_deliver(session, tenant, patient, outgoing, **kw):
        sent.append((patient.id, [m.body for m in outgoing]))

    monkeypatch.setattr(service_change.notify, "deliver", fake_deliver)
    return sent


async def _post_commit(db, tenant_id, result) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        await event_processor.run_post_commit(session, tenant, result)


async def test_an_underpaid_difference_tells_the_patient_the_exact_numbers_once(
    db, provider, monkeypatch
):
    sent = _recording_deliver(monkeypatch)
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, amount=2000)

    first = await _process(db, seeded[0], amount=2000)
    await _post_commit(db, seeded[0].id, first)

    assert first.outcome == "adjustment_amount_under"
    assert first.not_applied_adjustment_id == created.adjustment_id
    assert first.applied_adjustment_id is None
    # Never applied, nothing automatic: the clinic's item, the service as it was.
    assert (await _adjustment(db)).status == STATUS_CANCELLED
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"
    [item] = await _items(db)
    assert (item.reason, item.paid_amount_cents, item.amount_cents) == (
        REASON_AMOUNT_UNDER, 2000, 3000,
    )
    [(patient_id, [text])] = sent
    assert patient_id == seeded[1].id
    assert text == service_change.adjustment_underpaid_text("Avaliação", 3000, 2000)
    assert "Recebemos R$ 20,00" in text
    assert "R$ 30,00" in text and "Faltam R$ 10,00" in text
    assert "clínica vai entrar em contato" in text
    assert "devol" not in text.lower() and "estorn" not in text.lower()
    assert "Pix" in text and "000201pix" not in text  # no new charge for the remainder
    assert provider.called("create_pix_charge") == 1  # only the original difference

    # RECEIVED then CONFIRMED (another event id) and a redelivery of the same row: no 2nd notice.
    second = await _process(db, seeded[0], event_id="evt_diff_confirmed", amount=2000)
    await _post_commit(db, seeded[0].id, second)
    assert second.outcome == "adjustment_paid_replay"
    assert second.not_applied_adjustment_id is None
    async with db() as session:
        row = (
            await session.scalars(
                select(PaymentWebhookEvent).where(PaymentWebhookEvent.event_id == "evt_diff_1")
            )
        ).one()
        replay = await event_processor.process_payment_event_row(session, row.id, now=NOW)
    await _post_commit(db, seeded[0].id, replay)
    assert replay.outcome == "replay_noop"
    assert len(sent) == 1
    assert len(await _items(db)) == 1


async def test_a_wrong_reference_tells_the_patient_nothing(db, provider, monkeypatch):
    """Clinic-side only: a charge whose reference does not match is an item, never a message."""
    sent = _recording_deliver(monkeypatch)
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, reference="someone-else")

    result = await _process(db, seeded[0])
    await _post_commit(db, seeded[0].id, result)

    assert result.outcome == "adjustment_reference_mismatch"
    assert result.not_applied_adjustment_id is None
    assert sent == []
    [item] = await _items(db)
    assert item.reason == REASON_REFERENCE_MISMATCH
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_an_overpaid_difference_applies_and_tells_no_shortfall(db, provider, monkeypatch):
    """Overpay keeps today's behaviour: applied, AMOUNT_OVER item, the usual applied notice."""
    sent = _recording_deliver(monkeypatch)
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, amount=5000)

    result = await _process(db, seeded[0], amount=5000)

    assert result.outcome == "adjustment_applied"
    assert result.not_applied_adjustment_id is None
    assert [i.reason for i in await _items(db)] == [REASON_AMOUNT_OVER]
    assert not any("Faltam" in body for _p, bodies in sent for body in bodies)


async def test_an_underpaid_difference_without_a_known_amount_tells_the_patient_nothing(
    db, provider, monkeypatch
):
    """Follow-up review minor 2: never "Recebemos R$ 0,00" - without a real amount only the
    clinic's item is kept."""
    sent = _recording_deliver(monkeypatch)
    seeded, created = await _awaiting(db, provider)
    _paid(provider, created, amount=None)

    result = await _process(db, seeded[0], amount=None)
    await _post_commit(db, seeded[0].id, result)

    assert result.outcome == "adjustment_amount_under"
    assert result.not_applied_adjustment_id is None
    assert sent == []
    [item] = await _items(db)
    assert item.reason == REASON_AMOUNT_UNDER
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"
