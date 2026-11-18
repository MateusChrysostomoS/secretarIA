"""TASK-085 R / UA2 - the engine of the 3rd round (D-R10/D-R11): the old consultation LEAVES the
agenda while the Pix of a suspended change waits, and comes back (or not) when the change ends.

Contract: tasks/TASK-085/sinal-trocas/mapa-u.md §1.2, §4.4, §4.6 (+ UA1 deltas 1-11). The seeded
"before" clinic of tests/test_service_change.py: the old consultation (Consulta, +5 days, 30 min,
event `evt-1` on the clinic's agenda) changed to Avaliação one day later (`_requested`); after the
commit the old event is taken off the agenda (`old_slot.vacate_old_slot`, here called by the test
as the patient's / clinic's paths do). A fake Google agenda (the UA1 double). Pinned here:

* the buttons: "Outro horário" in the place of "Manter a antiga" when the old slot was taken;
  `lost_slot_buttons`; labels <= 20 letters;
* `choose_after_expiry`: "keep" decides nothing; "other" (choice or lost slot) frees the new hold
  and leaves the status; "both" also from `old_slot_lost` (its item closed);
* silence -> the old visit comes back with a NEW event; its slot taken -> `old_slot_lost` (+ the
  clinic's item, the patient's notice); the agenda down -> `restoring`, retried by the sweep; the
  lead reached -> `old_slot_lost`;
* no room for a choice -> `restoring` -> back;
* the sweep finishes the old events still `releasing` (`retry_vacates`);
* "Reenviar o Pix" copies `old_slot`; a `difkeep` never outlives its change (after the commit);
* paid after the old event left -> a NEW event (`difpaid`); a failure -> the agenda item;
* the webhook: a paid suspended change that cannot apply -> `restoring` -> back;
* the occupancy of `_window_fits`, `open_adjustment` and the agenda totals with the new states.

Characterization (the old event never left: `old_slot` NULL) stays in
tests/test_sinal_trocas_s1_unpaid.py / s1_paid.py.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

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
    MODE_SUSPEND,
    OLD_SLOT_RELEASED,
    OLD_SLOT_RELEASING,
    OLD_SLOT_REPLACED,
    OLD_SLOT_RESTORED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_EXPIRED,
    STATUS_KEPT_OLD,
    STATUS_OLD_SLOT_LOST,
    STATUS_RESTORING,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_RESOLVED,
    REASON_AMOUNT_UNDER,
    REASON_SERVICE_DIFF_AGENDA_PENDING,
    REASON_SERVICE_DIFF_REBOOK_DUE,
)
from secretaria.services import booking_hold
from secretaria.services.payments import (
    adjustment_queries,
    event_processor,
    notify,
    old_slot,
    service_change,
)
from secretaria.services.payments.attention_view import reason_text
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from secretaria.services.payments.webhook_payload import event_to_dict
from tests import test_service_change as base
from tests.payments_support import make_event
from tests.test_sinal_trocas_s1_unpaid import CLOCK, _requested
from tests.test_sinal_trocas_ua1_old_slot import OLD_END, OLD_START, _Calendar

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks
PIX_EXPIRED = NOW + timedelta(minutes=31)  # the difference Pix ran until +30 min
DEADLINE = PIX_EXPIRED + timedelta(hours=24)  # the choice's deadline (CHOICE_TTL)


@pytest.fixture(autouse=True)
def clocks(monkeypatch, db):
    CLOCK["now"] = NOW
    monkeypatch.setattr(booking_hold, "_now", lambda: CLOCK["now"])
    monkeypatch.setattr(old_slot, "_now", lambda: CLOCK["now"])
    monkeypatch.setattr(service_change, "_now", lambda: CLOCK["now"])
    # `vacate_old_slot` / `discard_restore_event` open their own session (after the commit).
    monkeypatch.setattr(old_slot, "async_session_factory", db)


@pytest.fixture
def cal(monkeypatch) -> _Calendar:
    calendar = _Calendar()
    calendar.add("evt-1")  # the old consultation's event

    async def _resolve(session, tenant, appointment):
        return calendar

    monkeypatch.setattr(service_change, "calendar_for_appointment", _resolve)
    return calendar


@pytest.fixture
def sent(monkeypatch):
    out: list = []

    async def fake_deliver(session, tenant, patient, messages, **kw):
        out.extend(messages)

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    return out


@pytest.fixture
def replans(monkeypatch):
    seen: list = []

    async def fake(appointment_id, **kw):
        seen.append(appointment_id)
        return 0

    from secretaria.services import reminder_hooks

    monkeypatch.setattr(reminder_hooks, "after_appointment_rescheduled", fake)
    return seen


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


async def _vacated(db, **kw):
    """A suspended change whose old event already left the agenda (`released`)."""
    seeded, created, new_start = await _requested(db, **kw)
    assert created.kind == "suspended"
    assert await old_slot.vacate_old_slot(seeded[0].id, created.adjustment_id) == "released"
    return seeded, created, new_start


async def _adj(db, adjustment_id) -> DepositAdjustment:
    async with db() as session:
        return await session.get(DepositAdjustment, adjustment_id)


async def _row(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _all(db, model):
    async with db() as session:
        return list((await session.scalars(select(model))).all())


async def _sweep(db, at):
    CLOCK["now"] = at
    async with db() as session:
        resolved = await service_change.expire_adjustments(session, now=at)
    await adjustment_queries.drain_restore_discards()
    return resolved


async def _set(db, adjustment_id, **values):
    async with db() as session:
        await session.execute(
            update(DepositAdjustment).where(DepositAdjustment.id == adjustment_id).values(**values)
        )
        await session.commit()


async def _choose(db, tenant_id, adjustment_id, choice, *, at=None):
    at = at or PIX_EXPIRED + timedelta(hours=1)
    CLOCK["now"] = at
    async with db() as session:
        outcome = await service_change.choose_after_expiry(
            session, tenant_id=tenant_id, adjustment_id=adjustment_id, choice=choice, now=at
        )
        await session.commit()
    await adjustment_queries.drain_restore_discards()
    return outcome


def _other_visit(seeded, start=OLD_START, end=OLD_END):
    return Appointment(
        id=uuid4(), tenant_id=seeded[0].id, google_event_id=f"evt{uuid4().hex[:6]}",
        appointment_type="Consulta", start_at=start, end_at=end,
        status=AppointmentStatus.SCHEDULED,
    )


async def _add(db, *rows):
    async with db() as session:
        session.add_all(rows)
        await session.commit()


def _ids(message) -> list[str]:
    return [button_id.split("|")[0] for button_id, _label in message.buttons]


# --------------------------------------------------------------------------
# the buttons and the texts (mapa-u §4.4 / §4.6)
# --------------------------------------------------------------------------


def test_the_other_time_action_and_labels():
    assert service_change.ACTION_CHOICE_OTHER == "svcdiffother"
    assert service_change.ACTION_CHOICE_OTHER in service_change.CHOICE_ACTIONS
    assert service_change.CHOICE_OF_ACTION["svcdiffother"] == service_change.CHOICE_OTHER == "other"
    assert service_change.LABEL_CHOICE_OTHER == "Outro horário"
    assert service_change.LABEL_CANCEL_CONSULTATION == "Cancelar consulta"
    for label in (service_change.LABEL_CHOICE_OTHER, service_change.LABEL_CANCEL_CONSULTATION):
        assert len(label) <= 20


def test_a_taken_old_slot_puts_other_time_first():
    adjustment_id = uuid4()
    assert service_change.choice_buttons(adjustment_id, old_slot_free=False) == [
        (f"svcdiffother|{adjustment_id}", "Outro horário"),
        (f"svcdiffresend|{adjustment_id}", "Reenviar o Pix"),
        (f"svcdiffboth|{adjustment_id}", "Cancelar ambas"),
    ]
    assert service_change.lost_slot_buttons(adjustment_id) == [
        (f"svcdiffother|{adjustment_id}", "Outro horário"),
        (f"svcdiffboth|{adjustment_id}", "Cancelar consulta"),
    ]


def test_the_lost_slot_and_price_texts():
    lost = service_change.old_slot_lost_text("Consulta")
    assert "(Consulta) foi ocupado" in lost and "continua valendo" in lost
    assert '"Outro horário"' in lost and "a clínica vai falar com você" in lost
    unpaid = service_change.price_unpaid_text(3000)
    assert "venceu" in unpaid and "continua marcada" in unpaid


def test_the_clinic_texts_of_the_new_reasons():
    rebook = reason_text("service_diff_rebook_due", None, 6000)
    assert "R$ 60,00" in rebook and "remarque a consulta" in rebook
    unpaid = reason_text("service_diff_unpaid", None, 3000)
    assert "troca ou novo preço" in unpaid and "R$ 30,00" in unpaid
    no_mail = reason_text("price_change_no_email", None, None)
    assert "não foi possível avisá-lo por e-mail" in no_mail and "o envio falhou" in no_mail


# --------------------------------------------------------------------------
# the choice message after the Pix expired
# --------------------------------------------------------------------------


async def test_the_choice_offers_keep_while_the_old_slot_is_free(db, provider, cal, sent):
    _seeded, created, _new = await _vacated(db)
    assert await _sweep(db, PIX_EXPIRED) == 1
    [message] = sent
    assert _ids(message) == ["svcdiffkeep", "svcdiffresend", "svcdiffboth"]
    assert "tentamos voltar para a consulta antiga" in message.body
    assert (await _adj(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE


async def test_the_choice_offers_another_time_when_the_old_slot_was_taken(
    db, provider, cal, sent
):
    seeded, created, _new = await _vacated(db)
    cal.add("evt-other")  # somebody booked the freed slot
    assert await _sweep(db, PIX_EXPIRED) == 1
    [message] = sent
    assert _ids(message) == ["svcdiffother", "svcdiffresend", "svcdiffboth"]
    assert "Outro horário" in message.body and "clínica vai falar com você" in message.body
    assert (await _adj(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE


async def test_an_unreadable_agenda_still_offers_keep(db, provider, cal, sent):
    _seeded, _created, _new = await _vacated(db)
    cal.fail_read = True
    assert await _sweep(db, PIX_EXPIRED) == 1
    [message] = sent
    assert _ids(message)[0] == "svcdiffkeep"  # the tap checks again


# --------------------------------------------------------------------------
# choose_after_expiry
# --------------------------------------------------------------------------


async def test_keep_is_no_longer_decided_by_the_engine(db, provider, cal, sent):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "keep")
    assert outcome.kind == "already_decided"
    assert (await _adj(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE
    assert len(await _all(db, BookingHold)) == 1


async def test_other_time_frees_the_new_hold_and_leaves_the_change_open(db, provider, cal, sent):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "other")
    assert (outcome.kind, outcome.appointment_id) == ("rebook", seeded[2].id)
    assert service_change.choice_answer_text(outcome) == (
        "Certo! Escolha o novo dia da sua consulta (Consulta). O sinal pago continua valendo."
    )
    adjustment = await _adj(db, created.adjustment_id)
    assert adjustment.status == STATUS_AWAITING_CHOICE  # `finish_rebook` closes it later
    assert await _all(db, BookingHold) == []
    # Rebookable by the patient's reschedule (UA3) - still the change it was.
    async with db() as session:
        assert (
            await old_slot.rebookable(
                session, tenant_id=seeded[0].id, appointment_id=seeded[2].id
            )
        ).id == created.adjustment_id


async def test_other_time_after_the_deadline_or_while_restoring_is_already_decided(
    db, provider, cal, sent
):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    late = await _choose(
        db, seeded[0].id, created.adjustment_id, "other", at=DEADLINE + timedelta(minutes=1)
    )
    assert late.kind == "already_decided"
    await _set(db, created.adjustment_id, status=STATUS_RESTORING)
    restoring = await _choose(db, seeded[0].id, created.adjustment_id, "other")
    assert restoring.kind == "already_decided"


async def test_a_lost_slot_still_offers_other_time_and_cancel(db, provider, cal, sent):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    await _set(db, created.adjustment_id, status=STATUS_OLD_SLOT_LOST)
    other = await _choose(
        db, seeded[0].id, created.adjustment_id, "other", at=DEADLINE + timedelta(hours=5)
    )
    assert other.kind == "rebook"  # no deadline while the clinic has not resolved it
    resend = await _choose(db, seeded[0].id, created.adjustment_id, "resend")
    assert resend.kind == "already_decided"


async def test_cancel_from_a_lost_slot_closes_the_clinics_item(db, provider, cal, sent):
    seeded, created, _new = await _vacated(db)
    cal.add("evt-other")
    await _sweep(db, PIX_EXPIRED)
    await _sweep(db, DEADLINE + timedelta(minutes=1))  # silence, slot taken -> lost
    assert (await _adj(db, created.adjustment_id)).status == STATUS_OLD_SLOT_LOST
    [item] = await _all(db, PaymentAttentionItem)
    assert (item.reason, item.status) == (REASON_SERVICE_DIFF_REBOOK_DUE, ATTENTION_OPEN)

    outcome = await _choose(
        db, seeded[0].id, created.adjustment_id, "both", at=DEADLINE + timedelta(hours=2)
    )

    assert (outcome.kind, outcome.appointment_id) == ("cancel_old", seeded[2].id)
    assert (await _adj(db, created.adjustment_id)).status == STATUS_CANCELLED
    [item] = await _all(db, PaymentAttentionItem)
    assert item.status == ATTENTION_RESOLVED


# --------------------------------------------------------------------------
# silence at the deadline (D-R8/D-R11)
# --------------------------------------------------------------------------


async def test_silence_brings_the_old_visit_back_with_a_new_event(
    db, provider, cal, sent, replans
):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    sent.clear()

    assert await _sweep(db, DEADLINE - timedelta(minutes=1)) == 0
    assert await _sweep(db, DEADLINE + timedelta(minutes=1)) == 1

    adjustment = await _adj(db, created.adjustment_id)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_KEPT_OLD, OLD_SLOT_RESTORED)
    row = await _row(db, seeded[2].id)
    restored = old_slot.restore_event_id(created.adjustment_id, calendar=cal)
    assert row.google_event_id == restored and cal.events[restored]["status"] == "confirmed"
    assert cal.events["evt-1"]["status"] == "cancelled"  # never re-used
    assert await _all(db, BookingHold) == []
    [message] = sent
    assert message.body == "Pronto! Sua consulta antiga voltou para a agenda, como era: Consulta."
    assert replans == [seeded[2].id]


async def test_silence_with_the_old_slot_taken_hands_it_to_the_clinic(
    db, provider, cal, sent, replans
):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    await _add(db, _other_visit(seeded))  # another patient's consultation on the old slot
    sent.clear()

    assert await _sweep(db, DEADLINE + timedelta(minutes=1)) == 1

    adjustment = await _adj(db, created.adjustment_id)
    assert adjustment.status == STATUS_OLD_SLOT_LOST and adjustment.resolved_at is None
    [item] = await _all(db, PaymentAttentionItem)
    assert (item.reason, item.amount_cents) == (REASON_SERVICE_DIFF_REBOOK_DUE, 6000)
    assert await _all(db, BookingHold) == []
    [message] = sent
    assert message.body == service_change.old_slot_lost_text("Consulta")
    assert _ids(message) == ["svcdiffother", "svcdiffboth"]
    assert replans == []  # still hidden from the patient: the clinic rebooks
    assert cal.created == []
    # Decided once: the next tick changes nothing.
    assert await _sweep(db, DEADLINE + timedelta(minutes=5)) == 0
    assert len(sent) == 1


async def test_silence_with_the_agenda_down_retries_until_it_answers(
    db, provider, cal, sent, replans
):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    sent.clear()
    cal.fail_read = True

    assert await _sweep(db, DEADLINE + timedelta(minutes=1)) == 1  # the choice ended
    adjustment = await _adj(db, created.adjustment_id)
    assert adjustment.status == STATUS_RESTORING and adjustment.resolved_at is None
    assert await _all(db, BookingHold) == []  # "Reenviar" is gone with it
    assert sent == []
    assert await _sweep(db, DEADLINE + timedelta(minutes=3)) == 0  # still down

    cal.fail_read = False
    assert await _sweep(db, DEADLINE + timedelta(minutes=5)) == 1

    adjustment = await _adj(db, created.adjustment_id)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_KEPT_OLD, OLD_SLOT_RESTORED)
    [message] = sent
    assert message.body == service_change.kept_old_notice_text("Consulta")
    assert replans == [seeded[2].id]


async def test_restoring_too_close_to_the_old_visit_becomes_a_lost_slot(
    db, provider, cal, sent
):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    await _set(db, created.adjustment_id, status=STATUS_RESTORING)
    await booking_hold_release(db, created.adjustment_id)
    cal.fail_read = True
    sent.clear()

    # More than an hour before the old visit: still retried.
    assert await _sweep(db, OLD_START - timedelta(hours=2)) == 0
    assert (await _adj(db, created.adjustment_id)).status == STATUS_RESTORING
    # Within CHOICE_MIN_LEAD of it: the clinic takes over.
    assert await _sweep(db, OLD_START - timedelta(minutes=50)) == 1
    assert (await _adj(db, created.adjustment_id)).status == STATUS_OLD_SLOT_LOST
    [item] = await _all(db, PaymentAttentionItem)
    assert item.reason == REASON_SERVICE_DIFF_REBOOK_DUE
    [message] = sent
    assert _ids(message) == ["svcdiffother", "svcdiffboth"]


async def booking_hold_release(db, adjustment_id):
    async with db() as session:
        await booking_hold.release_difference_hold_in(session, adjustment_id)
        await session.commit()


async def test_restoring_of_a_cancelled_visit_ends_cancelled(db, provider, cal, sent):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    await _set(db, created.adjustment_id, status=STATUS_RESTORING)
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        row.status = AppointmentStatus.CANCELLED
        await session.commit()
    sent.clear()

    assert await _sweep(db, PIX_EXPIRED + timedelta(minutes=5)) == 1
    assert (await _adj(db, created.adjustment_id)).status == STATUS_CANCELLED
    assert sent == [] and await _all(db, PaymentAttentionItem) == []


# --------------------------------------------------------------------------
# no room for a choice
# --------------------------------------------------------------------------


async def test_no_room_for_a_choice_brings_the_old_visit_back(db, provider, cal, sent, replans):
    seeded, created, _new = await _vacated(
        db, days_ahead=2 / 24, new_offset=timedelta(minutes=30)
    )
    assert await _sweep(db, NOW + timedelta(minutes=56)) == 1
    adjustment = await _adj(db, created.adjustment_id)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_KEPT_OLD, OLD_SLOT_RESTORED)
    [message] = sent
    assert message.body == service_change.kept_old_notice_text("Consulta")
    assert replans == [seeded[2].id]


async def test_no_room_and_the_old_slot_taken_is_a_lost_slot(db, provider, cal, sent):
    seeded, created, _new = await _vacated(
        db, days_ahead=2 / 24, new_offset=timedelta(minutes=30)
    )
    start = seeded[2].start_at
    await _add(db, _other_visit(seeded, start=start, end=start + timedelta(minutes=30)))
    assert await _sweep(db, NOW + timedelta(minutes=56)) == 1
    assert (await _adj(db, created.adjustment_id)).status == STATUS_OLD_SLOT_LOST
    [message] = sent
    assert _ids(message) == ["svcdiffother", "svcdiffboth"]


# --------------------------------------------------------------------------
# the sweep finishes the old events still to leave (UA1: retry_vacates)
# --------------------------------------------------------------------------


async def test_the_sweep_takes_a_pending_old_event_off_the_agenda(db, provider, cal, sent):
    seeded, created, _new = await _requested(db)
    await _set(
        db,
        created.adjustment_id,
        old_slot=OLD_SLOT_RELEASING,
        old_slot_at=NOW - timedelta(minutes=5),
    )
    assert await _sweep(db, NOW + timedelta(minutes=10)) == 0  # nothing of the change decided
    assert (await _adj(db, created.adjustment_id)).old_slot == OLD_SLOT_RELEASED
    assert cal.cancelled == ["evt-1"]
    assert (await _row(db, seeded[2].id)).google_event_id == "evt-1"  # the row is never written


# --------------------------------------------------------------------------
# "Reenviar o Pix" and the restore event left behind (UA1 delta 10)
# --------------------------------------------------------------------------


async def test_resend_copies_the_old_slot_and_discards_the_old_restore_event(
    db, provider, cal, sent
):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    leftover = old_slot.restore_event_id(created.adjustment_id, calendar=cal)
    cal.add(leftover)  # a restore that failed after creating its event (UA1 M1)
    provider.created_charge = provider.created_charge.__class__(
        provider_charge_id="pay_diff_2", copy_paste="000201pix2"
    )

    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "resend")

    assert outcome.kind == "resent"
    old = await _adj(db, created.adjustment_id)
    new = await _adj(db, outcome.adjustment_id)
    assert (old.status, new.status) == (STATUS_EXPIRED, STATUS_AWAITING_PAYMENT)
    assert (new.old_slot, new.old_slot_at) == (old.old_slot, old.old_slot_at)
    assert new.old_slot == OLD_SLOT_RELEASED
    assert cal.events[leftover]["status"] == "cancelled"


async def test_a_resend_that_fails_discards_nothing(db, provider, cal, sent):
    from secretaria.services.payments.provider import ProviderError

    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    leftover = old_slot.restore_event_id(created.adjustment_id, calendar=cal)
    cal.add(leftover)
    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "resend")
    assert outcome.kind == "resend_failed"
    assert cal.events[leftover]["status"] == "confirmed"  # still the change's to adopt


async def test_cancel_both_discards_a_left_restore_event_after_the_commit(
    db, provider, cal, sent
):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    leftover = old_slot.restore_event_id(created.adjustment_id, calendar=cal)
    cal.add(leftover)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "both")
    assert outcome.kind == "cancel_old"
    assert cal.events[leftover]["status"] == "cancelled"


async def test_a_cancellation_discards_after_the_commit_and_a_rollback_drops_it(
    db, provider, cal, sent
):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    leftover = old_slot.restore_event_id(created.adjustment_id, calendar=cal)
    cal.add(leftover)

    async def settle(commit: bool):
        async with db() as session:
            tenant = await session.get(Tenant, seeded[0].id)
            deposit = await session.get(PixDeposit, seeded[3].id)
            settled = await adjustment_queries.settle_open_adjustments(
                session, tenant=tenant, deposit=deposit, now=PIX_EXPIRED + timedelta(hours=1)
            )
            # Nothing is deleted before the commit (the change is still restorable).
            assert cal.events[leftover]["status"] == "confirmed"
            if commit:
                await session.commit()
            else:
                await session.rollback()
        await adjustment_queries.drain_restore_discards()
        return settled

    rolled_back = await settle(commit=False)
    assert rolled_back == {str(created.adjustment_id): adjustment_queries.SETTLED_CHOICE_CLOSED}
    assert cal.events[leftover]["status"] == "confirmed"
    assert (await _adj(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE

    await settle(commit=True)
    assert (await _adj(db, created.adjustment_id)).status == STATUS_CANCELLED
    assert cal.events[leftover]["status"] == "cancelled"


@pytest.mark.parametrize("stage", [STATUS_RESTORING, STATUS_OLD_SLOT_LOST])
async def test_a_cancellation_closes_the_new_open_states(db, provider, cal, sent, stage):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    if stage == STATUS_OLD_SLOT_LOST:
        await _add(db, _other_visit(seeded))
        await _sweep(db, DEADLINE + timedelta(minutes=1))
        [item] = await _all(db, PaymentAttentionItem)
        assert item.status == ATTENTION_OPEN
    else:
        await _set(db, created.adjustment_id, status=stage)
    assert (await _adj(db, created.adjustment_id)).status == stage

    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        settled = await adjustment_queries.settle_open_adjustments(
            session, tenant=tenant, deposit=deposit, now=DEADLINE + timedelta(hours=1)
        )
        await session.commit()
    await adjustment_queries.drain_restore_discards()

    assert settled == {str(created.adjustment_id): adjustment_queries.SETTLED_SUSPENSION_CLOSED}
    assert (await _adj(db, created.adjustment_id)).status == STATUS_CANCELLED
    assert all(item.status == ATTENTION_RESOLVED for item in await _all(db, PaymentAttentionItem))
    async with db() as session:
        assert (await session.get(PixDeposit, seeded[3].id)).fulfillment_status != (
            "needs_attention"
        )


# --------------------------------------------------------------------------
# paid after the old event left (update_agenda_after_payment)
# --------------------------------------------------------------------------


def _paid(provider, adjustment_id, amount=3000):
    provider.state = ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=amount,
        external_reference=str(adjustment_id), paid_at=NOW + timedelta(minutes=5),
    )


async def _webhook(db, tenant_id, status, *, at, amount=3000, event_id="evt_diff_ua2"):
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
        await event_processor.run_post_commit(session, tenant, result, now=at)
    return result


@pytest.fixture
def agenda_hooks(monkeypatch):
    monkeypatch.setattr(
        service_change,
        "ADJUSTMENT_APPLIED_HOOKS",
        [service_change.update_agenda_after_payment],
    )
    monkeypatch.setattr(
        service_change,
        "ADJUSTMENT_NOT_APPLIED_HOOKS",
        [service_change.notify_patient_adjustment_not_applied],
    )


async def test_paid_after_the_old_event_left_creates_the_new_event(
    db, provider, cal, sent, replans, agenda_hooks
):
    seeded, created, new_start = await _vacated(db)
    _paid(provider, created.adjustment_id)
    result = await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=5))

    assert result.outcome == "adjustment_applied"
    adjustment = await _adj(db, created.adjustment_id)
    assert adjustment.old_slot == OLD_SLOT_REPLACED
    row = await _row(db, seeded[2].id)
    paid_event = old_slot.paid_event_id(created.adjustment_id, calendar=cal)
    assert row.google_event_id == paid_event
    assert (row.appointment_type, row.start_at.replace(tzinfo=None)) == (
        "Avaliação", new_start.replace(tzinfo=None),
    )
    assert [event["id"] for event in cal.created] == [paid_event]
    assert await _all(db, PaymentAttentionItem) == []


async def test_paid_with_the_new_agenda_down_opens_the_agenda_item(
    db, provider, cal, sent, agenda_hooks
):
    seeded, created, _new = await _vacated(db)
    cal.fail_create = RuntimeError("down")
    _paid(provider, created.adjustment_id)
    result = await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=5))

    assert result.outcome == "adjustment_applied"
    [item] = await _all(db, PaymentAttentionItem)
    assert item.reason == REASON_SERVICE_DIFF_AGENDA_PENDING
    assert (await _adj(db, created.adjustment_id)).old_slot == OLD_SLOT_RELEASED


async def test_a_paid_change_whose_old_event_never_left_moves_it_as_today(
    db, provider, cal, sent, agenda_hooks
):
    """Characterization: `old_slot` NULL -> today's path (the old event is moved, not
    recreated)."""
    seeded, created, _new = await _requested(db)
    calls: list = []

    async def update_event_details(*args, **kw):
        calls.append(args[0])

    async def is_slot_free(*args, **kw):
        return True

    cal.update_event_details = update_event_details
    cal.is_slot_free = is_slot_free
    _paid(provider, created.adjustment_id)
    await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=5))
    assert (await _adj(db, created.adjustment_id)).old_slot is None
    assert cal.created == [] and (await _row(db, seeded[2].id)).google_event_id == "evt-1"


# --------------------------------------------------------------------------
# the webhook: paid but not applicable -> restoring -> back (mapa-u §7 item 9)
# --------------------------------------------------------------------------


async def test_an_underpaid_suspended_change_brings_the_old_visit_back(
    db, provider, cal, sent, replans, agenda_hooks
):
    seeded, created, _new = await _vacated(db)
    _paid(provider, created.adjustment_id, amount=2000)
    result = await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=5))

    assert result.outcome == "adjustment_amount_under"
    assert result.restore_adjustment_id == created.adjustment_id
    adjustment = await _adj(db, created.adjustment_id)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_KEPT_OLD, OLD_SLOT_RESTORED)
    [item] = await _all(db, PaymentAttentionItem)
    assert item.reason == REASON_AMOUNT_UNDER
    row = await _row(db, seeded[2].id)
    assert row.google_event_id == old_slot.restore_event_id(created.adjustment_id, calendar=cal)
    assert row.appointment_type == "Consulta"
    underpaid, kept = sent
    assert "Faltam" in underpaid.body
    assert kept.body == service_change.kept_old_notice_text("Consulta")
    assert replans == [seeded[2].id]
    assert await _all(db, BookingHold) == []


async def test_a_paid_change_that_no_longer_fits_restores_or_hands_over(
    db, provider, cal, sent, agenda_hooks
):
    seeded, created, new_start = await _vacated(db)
    # The NEW window was taken meanwhile (a booking the clinic made) AND the old one too.
    await _add(
        db,
        _other_visit(seeded, start=new_start, end=new_start + timedelta(minutes=30)),
        _other_visit(seeded),
    )
    _paid(provider, created.adjustment_id)
    result = await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=5))

    assert result.outcome == "adjustment_not_applicable"
    assert (await _adj(db, created.adjustment_id)).status == STATUS_OLD_SLOT_LOST
    reasons = sorted(item.reason for item in await _all(db, PaymentAttentionItem))
    assert reasons == ["orphan_payment", REASON_SERVICE_DIFF_REBOOK_DUE]
    assert _ids(sent[-1]) == ["svcdiffother", "svcdiffboth"]


async def test_a_paid_change_of_a_cancelled_visit_still_ends_cancelled(
    db, provider, cal, sent, agenda_hooks
):
    seeded, created, _new = await _vacated(db)
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        row.status = AppointmentStatus.CANCELLED
        await session.commit()
    _paid(provider, created.adjustment_id)
    result = await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=NOW + timedelta(minutes=5))
    assert result.outcome == "adjustment_not_applicable"
    assert result.restore_adjustment_id is None
    assert (await _adj(db, created.adjustment_id)).status == STATUS_CANCELLED


async def test_the_webhook_expired_without_room_restores(db, provider, cal, sent, agenda_hooks):
    seeded, created, _new = await _vacated(
        db, days_ahead=2 / 24, new_offset=timedelta(minutes=30)
    )
    result = await _webhook(
        db, seeded[0].id, PaymentStatus.EXPIRED, at=NOW + timedelta(minutes=56)
    )
    assert result.outcome == "adjustment_restoring"
    assert (await _adj(db, created.adjustment_id)).status == STATUS_KEPT_OLD


# --------------------------------------------------------------------------
# occupancy and the open states
# --------------------------------------------------------------------------


async def test_a_suspended_visit_does_not_block_another_change_onto_its_slot(db, provider, cal):
    """`_window_fits` (mapa-u §1.3): a consultation suspended by its own change left the agenda,
    so another change may land on its slot; a live one still blocks."""
    seeded, created, _new = await _vacated(db)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        other = _other_visit(
            seeded, start=OLD_START + timedelta(hours=2), end=OLD_START + timedelta(hours=3)
        )
        session.add(other)
        await session.flush()
        fits = await service_change._window_fits(session, tenant, other, OLD_START, OLD_END)
        assert fits is True
        await session.execute(
            update(DepositAdjustment)
            .where(DepositAdjustment.id == created.adjustment_id)
            .values(status=STATUS_KEPT_OLD)
        )
        assert await service_change._window_fits(session, tenant, other, OLD_START, OLD_END) is (
            False
        )
        await session.rollback()


@pytest.mark.parametrize("stage", [STATUS_RESTORING, STATUS_OLD_SLOT_LOST])
async def test_the_new_open_states_block_another_change_and_show_as_suspended(
    db, provider, cal, stage
):
    seeded, created, _new = await _vacated(db)
    await _set(db, created.adjustment_id, status=stage)
    async with db() as session:
        found = await service_change.open_adjustment(
            session, tenant_id=seeded[0].id, deposit_id=seeded[3].id
        )
        totals = await adjustment_queries.load_adjustment_totals(
            session, seeded[0].id, [seeded[3].id]
        )
    assert found.id == created.adjustment_id
    assert totals[seeded[3].id].pending_difference_mode == MODE_SUSPEND
    assert totals[seeded[3].id].pending_difference_cents is None
    assert totals[seeded[3].id].pending_edit["appointment_type"] == "Avaliação"
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        refused = await service_change.request_paid_change(
            session, tenant=tenant, appointment=row, deposit=deposit, new_service="Retorno",
            pending_edit=base._edit(row, "Retorno", 20), now=NOW,
        )
        await session.rollback()
    assert (refused.kind, refused.reason) == ("blocked", "choice_pending")


async def test_nothing_changes_for_a_legacy_row_or_another_mode(db, provider, cal, sent):
    """Characterization: the sweep's new steps never touch a legacy (`mode` NULL) or an
    `applied` change."""
    seeded, created, _new = await _requested(db, timing="after", new_offset=timedelta(0))
    assert created.kind == "applied_awaiting_payment"
    await _set(db, created.adjustment_id, old_slot=None)
    before = await _adj(db, created.adjustment_id)
    assert await _sweep(db, NOW + timedelta(minutes=10)) == 0
    after = await _adj(db, created.adjustment_id)
    assert (after.status, after.old_slot) == (before.status, None)
    assert cal.cancelled == [] and cal.created == []
    assert (await _adj(db, created.adjustment_id)).status == STATUS_AWAITING_PAYMENT


# --------------------------------------------------------------------------
# fix round 1 (review M1): savepoints, and a crash right after the commit
# --------------------------------------------------------------------------


async def _leftover(db, cal):
    """A suspended change waiting for the patient's choice, with the restore event a failed
    restore left behind (UA1 M1)."""
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    leftover = old_slot.restore_event_id(created.adjustment_id, calendar=cal)
    cal.add(leftover)
    _CAL[:] = [cal]
    return seeded, created, leftover


_CAL: list = []


def _difkeeps(_session):
    return [e for e in _CAL[-1].events.values() if "difkeep" in e["id"]]


async def _settle_with_savepoint(db, seeded, *, savepoint: str, outer: str):
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        await adjustment_queries.settle_open_adjustments(
            session, tenant=tenant, deposit=deposit, now=PIX_EXPIRED + timedelta(hours=1)
        )
        if savepoint == "released":
            async with session.begin_nested():
                await session.execute(select(1))
        else:
            with pytest.raises(RuntimeError):
                async with session.begin_nested():
                    raise RuntimeError("rolled back to the savepoint")
        await adjustment_queries.drain_restore_discards()
        # Nothing is deleted while the outer transaction is open.
        assert all(event["status"] == "confirmed" for event in _difkeeps(session))
        if outer == "commit":
            await session.commit()
        else:
            await session.rollback()
    await adjustment_queries.drain_restore_discards()


async def test_a_released_savepoint_never_discards_before_the_outer_commit(
    db, provider, cal, sent
):
    seeded, created, leftover = await _leftover(db, cal)
    await _settle_with_savepoint(db, seeded, savepoint="released", outer="rollback")

    # The savepoint's "commit" spawned nothing; the outer rollback dropped the request.
    assert cal.events[leftover]["status"] == "confirmed"
    assert (await _adj(db, created.adjustment_id)).status == STATUS_AWAITING_CHOICE


async def test_a_rolled_back_savepoint_keeps_the_request_for_the_outer_commit(
    db, provider, cal, sent
):
    seeded, created, leftover = await _leftover(db, cal)
    await _settle_with_savepoint(db, seeded, savepoint="rolled_back", outer="commit")
    assert (await _adj(db, created.adjustment_id)).status == STATUS_CANCELLED
    assert cal.events[leftover]["status"] == "cancelled"


@pytest.mark.parametrize("closed_by", ["both", "resend", "cancellation"])
async def test_the_sweep_discards_a_restore_event_the_post_commit_task_lost(
    db, provider, cal, sent, monkeypatch, closed_by
):
    """The process dies right after the commit (the post-commit task never runs): the next
    sweep deletes the leftover restore event - once."""
    seeded, created, leftover = await _leftover(db, cal)
    monkeypatch.setattr(
        adjustment_queries, "discard_restore_after_commit", lambda *args, **kwargs: None
    )
    if closed_by == "cancellation":
        async with db() as session:
            tenant = await session.get(Tenant, seeded[0].id)
            deposit = await session.get(PixDeposit, seeded[3].id)
            await adjustment_queries.settle_open_adjustments(
                session, tenant=tenant, deposit=deposit, now=PIX_EXPIRED + timedelta(hours=1)
            )
            await session.commit()
    else:
        if closed_by == "resend":
            provider.created_charge = provider.created_charge.__class__(
                provider_charge_id="pay_diff_2", copy_paste="000201pix2"
            )
        outcome = await _choose(db, seeded[0].id, created.adjustment_id, closed_by)
        assert outcome.kind == ("cancel_old" if closed_by == "both" else "resent")
    assert cal.events[leftover]["status"] == "confirmed"  # the fast path was lost

    reads: list = []
    real_get = cal.get_event

    async def counting_get(event_id):
        reads.append(event_id)
        return await real_get(event_id)

    cal.get_event = counting_get
    await _sweep(db, PIX_EXPIRED + timedelta(hours=2))
    assert cal.events[leftover]["status"] == "cancelled"
    assert reads == [leftover]
    await _sweep(db, PIX_EXPIRED + timedelta(hours=3))
    assert reads == [leftover]  # checked once per change


async def test_the_sweep_never_touches_a_restored_visit(db, provider, cal, sent, replans):
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    await _sweep(db, DEADLINE + timedelta(minutes=1))  # silence -> back with `difkeep`
    restored = old_slot.restore_event_id(created.adjustment_id, calendar=cal)
    await _sweep(db, DEADLINE + timedelta(minutes=5))
    assert cal.events[restored]["status"] == "confirmed"
    assert (await _row(db, seeded[2].id)).google_event_id == restored


# --------------------------------------------------------------------------
# fix round 1 (review M3, M4)
# --------------------------------------------------------------------------


async def test_other_time_of_a_lost_slot_still_works_after_the_old_start(db, provider, cal, sent):
    """mapa-u §7 item 4: the slot may be lost less than an hour before the old visit; "Outro
    horário" holds until the clinic resolves it."""
    seeded, created, _new = await _vacated(db)
    await _sweep(db, PIX_EXPIRED)
    await _set(db, created.adjustment_id, status=STATUS_OLD_SLOT_LOST)
    late = OLD_START + timedelta(hours=1)
    outcome = await _choose(db, seeded[0].id, created.adjustment_id, "other", at=late)
    assert outcome.kind == "rebook"
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        row.status = AppointmentStatus.CANCELLED
        await session.commit()
    gone = await _choose(db, seeded[0].id, created.adjustment_id, "other", at=late)
    assert gone.kind == "already_decided"


def test_the_no_window_reply_never_offers_the_old_visit():
    from secretaria.services.payments.service_change import ChoiceOutcome

    text = service_change.choice_answer_text(ChoiceOutcome(kind="no_window"))
    assert "manter" not in text.lower() and "Escolha uma das outras opções" in text
