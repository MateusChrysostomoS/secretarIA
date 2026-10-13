"""services/release_offer.py - a slot the clinic released with a PAID deposit (TASK-052).

Owner decisions 2026-10-12 (3rd round). D15: the platform NEVER refunds. "Manter" is a request to
the CLINIC (24 h to decide); "Remarcar" carries the deposit until the clinic says it refunded;
"Cancelar" always notifies the clinic at once. Every transition is one guarded UPDATE.
"""

from datetime import timedelta
from uuid import uuid4

import pytest_asyncio
from sqlalchemy import func, select

from secretaria.core.whatsapp_limits import (
    MAX_BUTTON_ID_CHARS,
    MAX_BUTTON_LABEL_CHARS,
    MAX_INTERACTIVE_BODY_CHARS,
)
from secretaria.models import PaymentAttentionItem, PixDeposit, PixDepositStatus
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.models.pix_deposit import FULFILLMENT_NEEDS_ATTENTION, FULFILLMENT_RESOLVED
from secretaria.services import release_offer as ro
from secretaria.services.payments import attention_view
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import (
    CLOSES_AT_SIX,
    RELEASED_START,
    T,
    emails,
    make_released,
    new_appointment,
    utc,
)

B = T + timedelta(hours=6)
E = T + timedelta(hours=24)
SP = "America/Sao_Paulo"


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


async def _offer(db, *, now=T, has_secretary=None, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=now,
            paid_notice_allowed=True,
            has_secretary=has_secretary,
        )
        await session.commit()
    return released, offer


async def _row(db, offer_id) -> DepositReleaseOffer:
    async with db() as session:
        return await session.get(DepositReleaseOffer, offer_id)


async def _deposit(db, deposit_id) -> PixDeposit:
    async with db() as session:
        return await session.get(PixDeposit, deposit_id)


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem)))


async def _schedule(db, offer_id) -> list[tuple]:
    async with db() as session:
        return [(e.kind, e.seq, utc(e.due_at), e.outcome) for e in await emails(session, offer_id)]


async def _kinds(db, offer_id, *, pending_only=False) -> list[str]:
    return [
        k for k, _s, _d, out in await _schedule(db, offer_id) if not pending_only or out is None
    ]


async def _call(db, fn, offer_id, at, **kw):
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer_id)
        result = await fn(session, offer=row, now=at, **kw)
        await session.commit()
    return result


async def _sweep(db, at) -> int:
    async with db() as session:
        return await ro.expire_offers(session, now=at)


async def _mark(db, released, offer_id, at=None):
    async with db() as session:
        return await ro.mark_refunded(
            session,
            tenant_id=released.tenant_id,
            offer_id=offer_id,
            note="Estornado no painel do Asaas",
            resolved_by_name="Ana",
            now=at or T + timedelta(hours=3),
        )


async def _carry(db, released, at, *, days=4, check=True):
    async with db() as session:
        if check:  # the provider said "paid, no refund" when the patient confirmed (Task 10)
            offer_id = await session.scalar(
                select(DepositReleaseOffer.id)
                .where(DepositReleaseOffer.conversation_id == released.conversation.id)
                .order_by(DepositReleaseOffer.released_at.desc())
                .limit(1)
            )
            if offer_id is not None:
                await ro.record_carry_check(
                    session, tenant_id=released.tenant_id, offer_id=offer_id,
                    outcome="reusable", now=at,
                )  # fmt: skip
        fresh = await new_appointment(
            session, released, start=RELEASED_START + timedelta(days=days)
        )
        carried = await ro.claim_carry_over(
            session,
            tenant_id=released.tenant_id,
            conversation_id=released.conversation.id,
            new_appointment=fresh,
            now=at,
        )
        await session.commit()
    return fresh, carried


# --- copy and buttons ----------------------------------------------------------------------------


def test_the_cards_fit_whatsapp_and_name_the_choices():
    rid = uuid4()
    for buttons, labels in (
        (ro.offer_buttons(rid), ["Manter horário", "Remarcar", "Cancelar"]),
        (ro.offer_buttons(rid, keep=False), ["Remarcar", "Cancelar"]),
        (ro.keep_fallback_buttons(rid), ["Remarcar", "Cancelar"]),
        (ro.check_buttons(rid), ["Recebido", "Não recebido"]),
    ):
        assert [label for _id, label in buttons] == labels
        for button_id, label in buttons:
            assert len(label) <= MAX_BUTTON_LABEL_CHARS and len(button_id) <= MAX_BUTTON_ID_CHARS
            assert button_id.split("|")[0] in ro.RELEASE_OFFER_ACTIONS
            assert button_id.endswith(f"|{rid}")


def test_the_texts_say_the_clinic_decides_and_never_name_a_bank():
    deposit = PixDeposit(amount_cents=6000)
    body = ro.offer_body(
        deposit=deposit, decide_by_at=B, timezone=SP, justification=None, confirmed=False
    )
    assert body.startswith(ro.OFFER_HEAD) and "R$ 60,00" in body and "até 20:00 de 05/03" in body
    assert "*Manter horário* para pedir à clínica o mesmo horário" in body
    no_keep = ro.offer_body(
        deposit=deposit,
        decide_by_at=B,
        timezone=SP,
        justification=None,
        confirmed=False,
        keep=False,
    )
    assert "Manter" not in no_keep
    long = ro.offer_body(
        deposit=deposit, decide_by_at=B, timezone=SP, justification="x" * 3000, confirmed=True
    )
    assert len(long) <= MAX_INTERACTIVE_BODY_CHARS and long.startswith(ro.OFFER_HEAD_CONFIRMED)
    assert "\n" not in ro.template_text(long)
    assert "decide em até 24 horas" in ro.KEEP_REQUESTED_TEXT
    assert ro.AFTER_START_TEXT.startswith("O horário da consulta já passou")
    for text in (body, ro.CHECK_TEXT, ro.KEEP_DECLINED_TEXT, ro.CANCEL_DONE_TEXT):
        assert "banco" not in text.lower()
        for promise in ("devolvemos", "estorno automático", "reembolsamos"):
            assert promise not in text
    assert "conta de onde saiu o Pix" in ro.CHECK_TEXT


# --- opening -------------------------------------------------------------------------------------


async def test_opening_is_idempotent_and_sets_the_instants(db):
    released, offer = await _offer(db)
    assert (offer.decision, offer.refund_status, offer.deposit_paid) == ("none", "not_due", True)
    assert (utc(offer.released_at), utc(offer.decide_by_at), utc(offer.expires_at)) == (T, B, E)
    assert utc(offer.slot_start_at) == RELEASED_START
    async with db() as session:
        again = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T + timedelta(minutes=5),
        )
        await session.commit()
        count = await session.scalar(select(func.count()).select_from(DepositReleaseOffer))
    assert again.id == offer.id and count == 1 and await _items(db) == []

    _other, unpaid = await _offer(db, deposit_status=PixDepositStatus.AWAITING)
    assert (unpaid.deposit_paid, unpaid.decision) == (False, "not_offered")
    await _sweep(db, E + timedelta(days=3))  # fires on the PAID offer above, never on this one
    after = await _row(db, unpaid.id)
    assert (after.decision, after.refund_status, after.closed_at) == (
        "not_offered",
        "not_due",
        None,
    )
    assert await _schedule(db, unpaid.id) == []


async def test_the_closing_is_the_consultation_doctors(db):
    # Owner 2026-10-12: B uses the DOCTOR's closing for now (the "clinic with a secretary" rule is
    # Task 16, pending). The doctor closes at 18:00 (F = T + 3 h 30); the clinic at 22:00 (T + 6 h).
    _a, doctors = await _offer(db, professional_hours=CLOSES_AT_SIX)
    _b, no_doctor = await _offer(db, business_hours=CLOSES_AT_SIX)
    assert utc(doctors.decide_by_at) == T + timedelta(hours=3, minutes=30)
    assert utc(no_doctor.decide_by_at) == T + timedelta(hours=3, minutes=30)  # no doctor: clinic's
    # The hook Task 16 will fill: True switches to the clinic's hours (22:00 -> B = T + 6 h).
    _c, clinics = await _offer(db, professional_hours=CLOSES_AT_SIX, has_secretary=True)
    assert utc(clinics.decide_by_at) == B


async def test_a_deposit_of_another_clinic_never_gets_an_offer(db):
    released, _ = await _offer(db)
    other, _ = await _offer(db)
    async with db() as session:
        crossed = await ro.open_offer(
            session, tenant=other.tenant, appointment=released.appointment, deposit=released.deposit
        )
    assert crossed is None


# --- Manter: a request to the clinic -------------------------------------------------------------


async def test_manter_asks_the_clinic_once_and_recreates_nothing(db):
    _released, offer = await _offer(db)
    at = T + timedelta(hours=1)

    assert await _call(db, ro.request_keep, offer.id, at) == ro.KEEP_REQUESTED_NOW
    assert await _call(db, ro.request_keep, offer.id, at) == ro.KEEP_ALREADY_REQUESTED

    row = await _row(db, offer.id)
    assert row.decision == "keep_requested" and utc(row.keep_deadline_at) == at + timedelta(
        hours=24
    )
    assert utc(row.first_contact_at) == at and row.new_appointment_id is None
    assert await _kinds(db, offer.id) == ["clinic_keep_request"]


async def test_manter_after_b_is_allowed_and_pauses_b_and_e(db):
    _released, offer = await _offer(db)
    await _sweep(db, B)  # nobody answered: the refund fell due at B

    assert (
        await _call(db, ro.request_keep, offer.id, B + timedelta(hours=1)) == ro.KEEP_REQUESTED_NOW
    )

    assert await _sweep(db, E) == 0  # an open negotiation is not "no answer"
    assert (await _row(db, offer.id)).decision == "keep_requested"


async def test_manter_after_s_or_after_the_refund_was_marked_is_answered_not_recorded(db):
    _a, soon = await _offer(db, start=T + timedelta(hours=2))
    assert await _call(db, ro.request_keep, soon.id, T + timedelta(hours=3)) == ro.KEEP_AFTER_START

    released, offer = await _offer(db)
    await _sweep(db, B)
    await _mark(db, released, offer.id, at=B + timedelta(minutes=5))
    assert (
        await _call(db, ro.request_keep, offer.id, B + timedelta(hours=1)) == ro.KEEP_AFTER_REFUND
    )
    assert (await _row(db, offer.id)).decision == "none"


async def test_the_clinic_refusing_makes_the_refund_due_and_tells_both(db):
    _released, offer = await _offer(db)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    at = T + timedelta(hours=2)

    assert await _call(db, ro.decline_keep, offer.id, at, outcome="refused", resolved_by="Ana")
    assert not await _call(db, ro.decline_keep, offer.id, at, outcome="refused", resolved_by="Ana")

    row = await _row(db, offer.id)
    assert (row.decision, row.keep_outcome, row.keep_resolved_by) == (
        "keep_declined",
        "refused",
        "Ana",
    )
    assert (row.refund_status, row.refund_source) == ("due", "keep_refused")
    assert len(await _items(db)) == 1
    schedule = await _schedule(db, offer.id)
    assert ("clinic_keep_declined", 0, at, None) in schedule
    assert ("patient_keep_declined", 0, at, None) in schedule
    assert ("clinic_refund_day", 1, at + timedelta(hours=24), None) in schedule


async def test_no_clinic_decision_within_24h_of_the_tap_counts_as_a_refusal(db):
    _released, offer = await _offer(db)
    tap = T + timedelta(hours=1)
    await _call(db, ro.request_keep, offer.id, tap)

    assert await _sweep(db, tap + timedelta(hours=23, minutes=59)) == 0
    assert await _sweep(db, tap + timedelta(hours=24)) == 1

    row = await _row(db, offer.id)
    assert (row.decision, row.keep_outcome, row.refund_source) == (
        "keep_declined",
        "timeout",
        "keep_timeout",
    )


async def test_the_clinic_accepting_moves_the_deposit_and_closes_the_refund(db):
    released, offer = await _offer(db)
    await _sweep(db, B)  # the refund was due (item open, notices scheduled)...
    await _call(db, ro.request_keep, offer.id, B + timedelta(minutes=10))
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        again = await new_appointment(session, released, start=RELEASED_START)
        assert await ro.claim_keep(
            session, offer=row, new_appointment=again, resolved_by="Ana", now=B + timedelta(hours=1)
        )
        await session.commit()

    kept = await _row(db, offer.id)
    assert (kept.decision, kept.keep_outcome, kept.new_appointment_id) == (
        "kept",
        "accepted",
        again.id,
    )
    assert kept.refund_status == "not_due"  # ...and is not any more: the money is in use
    deposit = await _deposit(db, released.deposit.id)
    assert deposit.appointment_id == again.id and deposit.status is PixDepositStatus.PAID
    assert deposit.fulfillment_status == FULFILLMENT_RESOLVED
    (item,) = await _items(db)
    assert (item.status, item.resolution_action) == ("resolved", "rebooked")
    # The refund notices of B/E are dropped; what is left is the request and the patient's answer.
    assert await _kinds(db, offer.id, pending_only=True) == [
        "clinic_keep_request",
        "patient_keep_accepted",
    ]


async def test_accepting_after_the_patient_moved_on_is_refused(db):
    released, offer = await _offer(db)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    await _call(db, ro.choose_reschedule, offer.id, T + timedelta(hours=2),
                conversation_id=released.conversation.id)  # fmt: skip
    async with db() as session:
        row = await session.get(DepositReleaseOffer, offer.id)
        late = await new_appointment(session, released, start=RELEASED_START)
        assert not await ro.claim_keep(
            session, offer=row, new_appointment=late, resolved_by="Ana", now=T + timedelta(hours=3)
        )


# --- Cancelar ------------------------------------------------------------------------------------


async def test_cancel_always_notifies_the_clinic_now(db):
    _a, before_s = await _offer(db)
    _b, after_s = await _offer(db, start=T + timedelta(hours=2))
    _c, after_b = await _offer(db, now=T - timedelta(hours=7))  # its B already passed
    await _sweep(db, T)
    for offer, at in ((before_s, T + timedelta(hours=1)), (after_s, T + timedelta(hours=3)),
                      (after_b, T + timedelta(hours=1))):  # fmt: skip
        assert await _call(db, ro.choose_cancel, offer.id, at) == ro.CANCEL_NOTIFIED
        assert "clinic_refund_cancel" in await _kinds(db, offer.id)
        row = await _row(db, offer.id)
        assert (row.decision, row.refund_status) == ("cancelled", "due")
    assert len(await _items(db)) == 3  # one per deposit, never two


async def test_cancel_after_the_clinic_refused_manter_still_notifies(db):
    _released, offer = await _offer(db)
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, offer.id, T + timedelta(hours=2), outcome="refused")

    assert await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=3)) == ro.CANCEL_NOTIFIED
    assert len(await _items(db)) == 1


async def test_cancel_after_marked_twice_after_kept_or_after_e(db):
    released, marked = await _offer(db)
    await _sweep(db, B)
    await _mark(db, released, marked.id, at=B + timedelta(minutes=5))
    assert (
        await _call(db, ro.choose_cancel, marked.id, B + timedelta(hours=1))
        == ro.CANCEL_AFTER_REFUND
    )
    assert "clinic_refund_cancel" not in await _kinds(db, marked.id)

    _x, twice = await _offer(db)
    assert await _call(db, ro.choose_cancel, twice.id, T + timedelta(hours=1)) == ro.CANCEL_NOTIFIED
    assert await _call(db, ro.choose_cancel, twice.id, T + timedelta(hours=2)) == ro.CANCEL_CLOSED

    _y, silent = await _offer(db)
    await _sweep(db, E)
    assert (
        await _call(db, ro.choose_cancel, silent.id, E + timedelta(minutes=1)) == ro.CANCEL_CLOSED
    )
    assert (await _row(db, silent.id)).decision == "no_answer_cancelled"


# --- B and E -------------------------------------------------------------------------------------


async def test_b_then_e_without_any_contact(db):
    _released, offer = await _offer(db)

    assert await _sweep(db, B) == 1
    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status, row.refund_source) == ("none", "due", "timeout")
    assert await _kinds(db, offer.id) == ["clinic_refund_noanswer", "clinic_refund_day"]

    assert await _sweep(db, E) == 1
    row = await _row(db, offer.id)
    assert row.decision == "no_answer_cancelled" and row.first_contact_at is None
    assert ro.patient_decision(row) == "cancelled_no_answer"
    assert await _sweep(db, E + timedelta(hours=1)) == 0


async def test_an_unfinished_remarcar_is_cancelled_for_lack_of_answer_at_e(db):
    released, offer = await _offer(db)
    tap = T + timedelta(hours=1)
    async with db() as session:
        await ro.record_contact(session, offer_id=offer.id, now=tap)
        await session.commit()
    assert await _call(db, ro.choose_reschedule, offer.id, tap,
                       conversation_id=released.conversation.id) == ro.RESCHEDULE_CARRYING  # fmt: skip  # noqa: E501

    await _sweep(db, E)  # one late tick: B and E together

    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status) == ("no_answer_cancelled", "due")
    assert utc(row.first_contact_at) == tap


# --- Remarcar ------------------------------------------------------------------------------------


async def test_remarcar_carries_until_the_clinic_marks_refunded(db):
    released, offer = await _offer(db)
    await _sweep(db, B)  # after B: still carries
    resched = ro.choose_reschedule
    conv = released.conversation.id
    assert await _call(db, resched, offer.id, B + timedelta(hours=1), conversation_id=conv) == (
        ro.RESCHEDULE_CARRYING
    )
    await _mark(db, released, offer.id, at=B + timedelta(hours=2))
    assert await _call(db, resched, offer.id, B + timedelta(hours=3), conversation_id=conv) == (
        ro.RESCHEDULE_NEW_CHARGE
    )
    async with db() as session:
        assert not await ro.carry_over_pending(
            session, tenant_id=released.tenant_id, conversation_id=conv
        )


async def test_remarcar_after_cancel_or_after_a_refusal_still_carries(db):
    released, cancelled = await _offer(db)
    await _call(db, ro.choose_cancel, cancelled.id, T + timedelta(hours=1))
    assert await _call(db, ro.choose_reschedule, cancelled.id, T + timedelta(hours=2),
                       conversation_id=released.conversation.id) == ro.RESCHEDULE_CARRYING  # fmt: skip  # noqa: E501

    other, declined = await _offer(db)
    await _call(db, ro.request_keep, declined.id, T + timedelta(hours=1))
    await _call(db, ro.decline_keep, declined.id, T + timedelta(hours=2), outcome="refused")
    assert await _call(db, ro.choose_reschedule, declined.id, T + timedelta(hours=3),
                       conversation_id=other.conversation.id) == ro.RESCHEDULE_CARRYING  # fmt: skip


async def test_remarcar_after_e_or_after_kept_reopens_nothing(db):
    released, offer = await _offer(db)
    await _sweep(db, E)
    assert await _call(db, ro.choose_reschedule, offer.id, E + timedelta(hours=1),
                       conversation_id=released.conversation.id) == ro.RESCHEDULE_NO_ANSWER  # fmt: skip  # noqa: E501
    assert (await _row(db, offer.id)).decision == "no_answer_cancelled"


async def test_a_confirmed_remarcar_carries_the_deposit_cancels_the_refund_and_tells_the_clinic(db):
    released, offer = await _offer(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))  # refund due, notices on
    await _call(db, ro.choose_reschedule, offer.id, T + timedelta(hours=2),
                conversation_id=released.conversation.id)  # fmt: skip

    fresh, carried = await _carry(db, released, T + timedelta(hours=3))
    _again, second = await _carry(db, released, T + timedelta(hours=4), days=5)

    assert carried is not None and carried.appointment_id == fresh.id and second is None
    assert carried.reschedule_count == 0
    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status, row.new_appointment_id) == (
        "rebooked",
        "not_due",
        fresh.id,
    )
    (item,) = await _items(db)
    assert (item.status, item.resolution_action) == ("resolved", "rebooked")
    assert await _kinds(db, offer.id, pending_only=True) == ["clinic_rebooked_deposit"]
    assert ro.patient_decision(row) == "rescheduled"


async def test_another_clinic_carries_nothing(db):
    released, offer = await _offer(db)
    other, _ = await _offer(db)
    await _call(db, ro.choose_reschedule, offer.id, T, conversation_id=released.conversation.id)
    async with db() as session:
        foreign = await new_appointment(session, other, start=RELEASED_START + timedelta(days=4))
        assert (
            await ro.claim_carry_over(
                session,
                tenant_id=other.tenant_id,
                conversation_id=released.conversation.id,
                new_appointment=foreign,
                now=T,
            )
            is None
        )
    assert (await _deposit(db, released.deposit.id)).appointment_id == released.appointment.id


async def test_a_refused_carry_check_stops_the_carry_and_makes_the_old_deposit_due(db):
    # Owner 2026-10-12: the provider said refunded (or could not be read) when the patient
    # confirmed - the booking charges a NEW Pix and the old deposit is a refund the clinic owes.
    released, offer = await _offer(db)
    await _call(db, ro.choose_reschedule, offer.id, T + timedelta(hours=1),
                conversation_id=released.conversation.id)  # fmt: skip
    async with db() as session:
        first = await ro.record_carry_check(
            session, tenant_id=released.tenant_id, offer_id=offer.id,
            outcome="unknown", now=T + timedelta(hours=2),
        )  # fmt: skip
        again = await ro.record_carry_check(
            session, tenant_id=released.tenant_id, offer_id=offer.id,
            outcome="reusable", now=T + timedelta(hours=2, minutes=1),
        )  # fmt: skip
        await session.commit()
    assert (first, again) == ("unknown", "unknown")  # recorded ONCE: the first answer stands
    async with db() as session:
        assert not await ro.carry_over_pending(
            session, tenant_id=released.tenant_id, conversation_id=released.conversation.id
        )
    _fresh, carried = await _carry(db, released, T + timedelta(hours=3), check=False)
    assert carried is None
    row = await _row(db, offer.id)
    assert (row.decision, row.refund_status, row.refund_source) == (
        "rebooked", "due", "carry_unknown",
    )  # fmt: skip
    assert row.deposit_reused_at is None and row.new_appointment_id is None
    assert (await _deposit(db, released.deposit.id)).appointment_id == released.appointment.id
    (item,) = await _items(db)
    assert item.status == "open"
    assert "clinic_carry_refused" in await _kinds(db, offer.id, pending_only=True)


async def test_marking_refunded_after_the_deposit_was_reused_is_refused(db):
    released, offer = await _offer(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))  # refund due, notices on
    await _call(db, ro.choose_reschedule, offer.id, T + timedelta(hours=2),
                conversation_id=released.conversation.id)  # fmt: skip
    _fresh, carried = await _carry(db, released, T + timedelta(hours=3))
    assert carried is not None
    assert await _mark(db, released, offer.id, at=T + timedelta(hours=4)) == "deposit_reused"
    row = await _row(db, offer.id)
    assert utc(row.deposit_reused_at) == T + timedelta(hours=3) and row.refund_status == "not_due"
    assert "patient_check" not in await _kinds(db, offer.id)
    assert "clinic_refund_cancel" not in await _kinds(db, offer.id, pending_only=True)


async def test_a_tap_that_loses_to_the_sweep_opens_no_second_item(db):
    _released, offer = await _offer(db)
    async with db() as session:
        stale = await session.get(DepositReleaseOffer, offer.id)
    await _sweep(db, B)
    async with db() as session:
        assert not await ro.settle_if_expired(session, offer=stale, now=B)
    assert len(await _items(db)) == 1


# --- devolvido / Recebido / Não recebido -------------------------------------------------------


async def test_marking_refunded_rules(db):
    released, offer = await _offer(db)
    assert await _mark(db, released, offer.id) == "not_due"  # the patient may still choose
    await _call(db, ro.request_keep, offer.id, T + timedelta(hours=1))
    await _sweep(db, B)  # paused: nothing falls due during the negotiation
    assert await _mark(db, released, offer.id) == "keep_pending"

    other, due = await _offer(db)
    await _sweep(db, B)
    await _call(db, ro.request_keep, due.id, B + timedelta(minutes=1))  # after B: refund was due
    assert await _mark(db, other, due.id) == "keep_pending"  # decide "Manter" first
    await _call(db, ro.decline_keep, due.id, B + timedelta(hours=1), outcome="refused")
    assert await _mark(db, other, due.id, at=B + timedelta(hours=2)) == "marked"
    assert await _mark(db, other, due.id, at=B + timedelta(hours=3)) == "already"
    assert [k for k in await _kinds(db, due.id) if k == "patient_check"] == ["patient_check"]
    assert (await _deposit(db, other.deposit.id)).status is PixDepositStatus.CANCELLED_REFUNDED

    async with db() as session:
        outcome = await ro.mark_refunded(
            session, tenant_id=released.tenant_id, offer_id=due.id, note="Estornado no painel",
            resolved_by_name="Ana",
        )  # fmt: skip
    assert outcome == "not_found"  # another clinic's offer


async def test_resolving_the_item_another_way_ends_every_notice(db):
    released, offer = await _offer(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    (item,) = await _items(db)
    async with db() as session:
        await attention_view.resolve_item(
            session,
            released.tenant_id,
            item.id,
            action="kept_as_credit",
            note="Crédito para a próxima consulta",
            resolved_by_name="Ana",
        )
    assert (await _row(db, offer.id)).refund_status == "clinic_decided"
    assert await _kinds(db, offer.id, pending_only=True) == []
    assert (
        await _deposit(db, released.deposit.id)
    ).fulfillment_status != FULFILLMENT_NEEDS_ATTENTION


async def test_received_and_not_received(db):
    released, offer = await _offer(db)
    await _call(db, ro.choose_cancel, offer.id, T + timedelta(hours=1))
    assert await _call(db, ro.record_received, offer.id, T + timedelta(hours=2)) == "too_early"
    await _mark(db, released, offer.id)

    assert await _call(db, ro.request_reminder, offer.id, T + timedelta(hours=4)) == "sent"
    assert await _call(db, ro.request_reminder, offer.id, T + timedelta(hours=8)) == "recent"
    assert await _call(db, ro.request_reminder, offer.id, T + timedelta(hours=29)) == "sent"
    assert await _call(db, ro.record_received, offer.id, T + timedelta(hours=30)) == "closed"
    assert (
        await _call(db, ro.record_received, offer.id, T + timedelta(hours=31)) == "already_closed"
    )

    reminders = [
        (k, s) for k, s, _d, _o in await _schedule(db, offer.id) if k == "clinic_refund_reminder"
    ]
    assert reminders == [("clinic_refund_reminder", 1), ("clinic_refund_reminder", 2)]
    assert (
        await _kinds(db, offer.id, pending_only=True) == []
    )  # received: the E reminder never goes


def test_patient_decision_maps_every_state_for_the_clinic_list():
    offer = DepositReleaseOffer(decision="none", refund_status="not_due")
    cases = {
        ("none", "not_due"): "no_answer",
        ("rescheduling", "due"): "no_answer",
        ("keep_requested", "not_due"): "keep_requested",
        ("kept", "not_due"): "kept",
        ("keep_declined", "due"): "keep_declined",
        ("rebooked", "not_due"): "rescheduled",
        ("cancelled", "due"): "cancelled",
        ("no_answer_cancelled", "due"): "cancelled_no_answer",
        ("cancelled", "received"): "received",
        ("no_answer_cancelled", "not_received"): "not_received",
        ("not_offered", "not_due"): "not_offered",
    }
    for (decision, refund), expected in cases.items():
        offer.decision, offer.refund_status = decision, refund
        assert ro.patient_decision(offer) == expected
