"""TASK-085 R / UB2 - D-R14: the patient opens the "o valor da sua consulta mudou" link.

`price_change.open_price_notice` (commits) decides WITH THE PRICE OF NOW and
`price_change.price_opening_messages` (pure) is the first message (mapa-u §2.3, §4.6):

* raise + sinal PAID -> a Pix of only the difference (`service_change.price_requote`,
  `mode="applied"`; unpaid -> the consultation stays, the clinic collects);
* drop + sinal PAID -> "quer a diferença de volta?" (`svcdiffyes|` / `svcdiffno|`, D-R2 limits)
  or abated on the day;
* sinal NOT paid -> `requote_awaiting` voids the old Pix; the caller then runs
  `deposit_lifecycle.recharge_after_edit(reason="price_change")`.

Pending items from the earlier slices: a broken link (no row) -> "not_found" (the usual
opening); a difference the clinic is collecting (`collect_due`) -> refused, settled on the day;
a consultation no longer live -> "closed" ("nada muda"); opened twice -> "pending", never a
second charge. The seeded clinic (tests/test_sinal_trocas_ub1_sweep.py): Consulta R$ 200,00,
sinal 30 % = R$ 60,00, the visit 12 days ahead.
"""

from datetime import timedelta
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.appointment_reminder import (
    REMINDER_CHANNEL_EMAIL,
    REMINDER_KIND_PRICE_CHANGE,
    REMINDER_STATUS_SENT,
    AppointmentReminder,
)
from secretaria.models.deposit_adjustment import (
    KIND_DECREASE,
    KIND_INCREASE,
    MODE_APPLIED,
    MODE_SUSPEND,
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CLINIC_KEPT,
    STATUS_COLLECT_DUE,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import REASON_SINAL_REQUOTE_FAILED
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services.payments import (
    deposit_lifecycle,
    notify,
    price_change,
    registry,
    service_change,
)
from secretaria.services.payments.provider import (
    ChargeState,
    CreatedCharge,
    PaymentStatus,
    ProviderError,
)
from tests.payments_support import FakeProvider, FakeRedis, make_sessionmaker
from tests.test_sinal_trocas_ub1_sweep import (
    CHANGED_AT,
    NOW,
    START,
    catalog,
    seed_tenant,
    seed_visit,
)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(price_change, "async_session_factory", maker)
    monkeypatch.setattr(deposit_lifecycle, "async_session_factory", maker)
    monkeypatch.setattr(notify, "async_session_factory", maker)
    yield maker
    await engine.dispose()


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(name="asaas", state=ChargeState(PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(
        provider_charge_id="pay_diff", copy_paste="COPYDIFF", provider_customer_id="cus_1"
    )
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def seed(db, *, awaiting=False, start=START, price="R$ 200,00", **visit):
    """One clinic, one Portal patient with a conversation, one consultation with its sinal."""
    async with db() as session:
        tenant = await seed_tenant(session, price=price)
        kwargs = {"awaiting_cents": 6000} if awaiting else {}
        patient, appointment, deposit = await seed_visit(
            session, tenant, start=start, **kwargs, **visit
        )
        session.add(Conversation(id=uuid4(), tenant_id=tenant.id, patient_id=patient.id))
        await session.commit()
    return tenant, patient, appointment, deposit


async def reprice(db, tenant_id, price: str | None) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        entries = catalog()
        if price is None:
            entries[0].pop("price")
        else:
            entries[0]["price"] = price
        tenant.appointment_types = entries
        await session.commit()


async def add_notice(
    db, appointment, *, answered=False, invalidated=False, start=None, tenant_id=None
) -> UUID:
    """The row the sweep wrote and the e-mail links to (born `sent`)."""
    async with db() as session:
        row = AppointmentReminder(
            id=uuid4(),
            tenant_id=tenant_id or appointment.tenant_id,
            appointment_id=appointment.id,
            patient_id=appointment.patient_id,
            kind=REMINDER_KIND_PRICE_CHANGE,
            appointment_start_at=start or appointment.start_at,
            due_at=CHANGED_AT,
            status=REMINDER_STATUS_SENT,
            channel=REMINDER_CHANNEL_EMAIL,
            with_prompt=False,
            sent_at=CHANGED_AT,
            answered_at=CHANGED_AT if answered else None,
            invalidated_at=CHANGED_AT if invalidated else None,
        )
        session.add(row)
        await session.commit()
        return row.id


async def open_(db, tenant_id, reminder_id, patient_id, *, now=NOW):
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        return await price_change.open_price_notice(
            session, tenant=tenant, reminder_id=reminder_id, patient_id=patient_id, now=now
        )


async def notice(db, reminder_id) -> AppointmentReminder:
    async with db() as session:
        return await session.get(AppointmentReminder, reminder_id)


async def adjustments(db) -> list[DepositAdjustment]:
    async with db() as session:
        return list(
            await session.scalars(select(DepositAdjustment).order_by(DepositAdjustment.created_at))
        )


def texts(opening, tenant) -> list[str]:
    return [m.body for m in price_change.price_opening_messages(opening, tenant=tenant)]


# ------------------------------------------------------------------ a rise, sinal paid


@pytest.mark.parametrize("timing", ["after", "before"])
async def test_a_rise_opens_with_the_pix_of_only_the_difference(db, provider, timing):
    tenant, patient, appointment, _deposit = await seed(db)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.deposit_timing = timing
        await session.commit()
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")  # sinal R$ 60 -> R$ 90

    opening = await open_(db, tenant.id, rid, patient.id)

    assert (opening.kind, opening.diff_cents, opening.pix_copy_paste) == ("pix", 3000, "COPYDIFF")
    assert opening.expires_at is not None and opening.appointment_id == appointment.id
    [adjustment] = await adjustments(db)
    assert (adjustment.kind, adjustment.mode, adjustment.status) == (
        KIND_INCREASE,
        MODE_APPLIED,
        STATUS_AWAITING_PAYMENT,
    )
    assert adjustment.id == opening.adjustment_id
    assert service_change.is_price_change(adjustment.pending_edit)
    saved = await notice(db, rid)
    assert saved.answered_at is not None and saved.answer == price_change.ANSWER_OPENED
    [body] = texts(opening, tenant)
    assert body.startswith("O valor da sua consulta (Consulta, 30/11 às 10:00) subiu.")
    assert "Falta pagar a diferença do sinal: R$ 30,00." in body
    hhmm = (opening.expires_at - timedelta(hours=3)).strftime("%H:%M")  # São Paulo, UTC-3
    assert f"Use o Pix abaixo até {hhmm}." in body
    assert "sua consulta continua marcada e a clínica combina a diferença com você" in body
    assert body.endswith("```COPYDIFF```")
    # Nothing of the consultation changed (mode applied: it stays as it is).
    async with db() as session:
        row = await session.get(Appointment, appointment.id)
    assert (row.appointment_type, row.status) == ("Consulta", AppointmentStatus.SCHEDULED)


async def test_opening_twice_points_at_the_earlier_pix_and_never_charges_again(db, provider):
    tenant, patient, appointment, _deposit = await seed(db)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")
    first = await open_(db, tenant.id, rid, patient.id)

    again = await open_(db, tenant.id, rid, patient.id)

    assert again.kind == "pending"
    assert (again.adjustment_id, again.expires_at) == (first.adjustment_id, first.expires_at)
    assert provider.called("create_pix_charge") == 1
    assert len(await adjustments(db)) == 1
    hhmm = (first.expires_at - timedelta(hours=3)).strftime("%H:%M")
    assert texts(again, tenant) == [
        f"O Pix da diferença já está na mensagem acima e vale até {hhmm}."
    ]


async def test_a_reopened_link_after_everything_settled_says_it_was_sent_above(db, provider):
    tenant, patient, appointment, _deposit = await seed(db)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")
    await open_(db, tenant.id, rid, patient.id)
    async with db() as session:  # the Pix expired unpaid: collect_due, nothing open
        assert (
            await service_change.expire_adjustments(session, now=NOW + timedelta(minutes=31)) == 1
        )

    again = await open_(db, tenant.id, rid, patient.id, now=NOW + timedelta(hours=1))

    assert (again.kind, again.adjustment_id) == ("pending", None)
    assert texts(again, tenant) == [price_change.PRICE_SENT_BEFORE_TEXT]
    assert provider.called("create_pix_charge") == 1


async def test_the_provider_down_answers_retry_and_leaves_the_link_working(db, provider):
    tenant, patient, appointment, _deposit = await seed(db)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")
    provider.create_error = ProviderError("create_failed", status_code=500)

    opening = await open_(db, tenant.id, rid, patient.id)

    assert opening.kind == "retry"
    assert texts(opening, tenant) == [price_change.PRICE_RETRY_TEXT]
    assert (await notice(db, rid)).answered_at is None  # the same link works again
    assert await adjustments(db) == []
    provider.create_error = None
    assert (await open_(db, tenant.id, rid, patient.id)).kind == "pix"


async def test_a_difference_the_clinic_collects_is_refused_and_settled_on_the_day(db, provider):
    tenant, patient, appointment, deposit = await seed(db)
    async with db() as session:
        session.add(
            DepositAdjustment(
                id=uuid4(),
                tenant_id=tenant.id,
                deposit_id=deposit.id,
                appointment_id=appointment.id,
                kind=KIND_INCREASE,
                mode=MODE_APPLIED,
                status=STATUS_COLLECT_DUE,
                old_service="Consulta",
                new_service="Exame",
                diff_cents=3000,
                resolved_at=NOW - timedelta(days=1),
            )
        )
        await session.commit()
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 100,00")  # a drop that would otherwise be asked back

    opening = await open_(db, tenant.id, rid, patient.id)

    assert opening.kind == "too_close"
    assert texts(opening, tenant) == ["A diferença do sinal será acertada no dia da consulta."]
    assert provider.calls == [] and len(await adjustments(db)) == 1
    assert (await notice(db, rid)).answered_at is not None


async def test_no_payment_account_settles_the_rise_on_the_day(db, provider):
    tenant, patient, appointment, _deposit = await seed(db)
    async with db() as session:
        from secretaria.models.clinic_payment_account import ClinicPaymentAccount

        await session.execute(
            ClinicPaymentAccount.__table__.delete().where(
                ClinicPaymentAccount.tenant_id == tenant.id
            )
        )
        await session.commit()
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")

    opening = await open_(db, tenant.id, rid, patient.id)

    assert opening.kind == "too_close" and provider.calls == []


# ------------------------------------------------------------------ a drop, sinal paid


async def test_a_drop_far_ahead_asks_whether_the_patient_wants_it_back(db, provider):
    tenant, patient, appointment, _deposit = await seed(db)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 100,00")  # sinal R$ 60 -> R$ 30

    opening = await open_(db, tenant.id, rid, patient.id)

    assert (opening.kind, opening.diff_cents) == ("refund_question", 3000)
    [adjustment] = await adjustments(db)
    assert (adjustment.kind, adjustment.status) == (KIND_DECREASE, STATUS_ASKED)
    [message] = price_change.price_opening_messages(opening, tenant=tenant)
    assert isinstance(message, notify.Buttons)
    assert message.body == (
        "O valor da sua consulta (Consulta) baixou e o sinal que você pagou ficou R$ 30,00 "
        "maior. Quer essa diferença de volta?"
    )
    assert list(message.buttons) == [
        (f"svcdiffyes|{adjustment.id}", "Sim, quero de volta"),
        (f"svcdiffno|{adjustment.id}", "Não, abater no valor"),
    ]
    assert provider.calls == []


async def test_a_drop_close_to_the_visit_is_abated_on_the_day(db, provider):
    start = NOW + timedelta(days=1)  # under the 2 days of the refund question (D-R2)
    tenant, patient, appointment, _deposit = await seed(db, start=start)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 100,00")

    opening = await open_(db, tenant.id, rid, patient.id)

    assert (opening.kind, opening.diff_cents) == ("abated", 3000)
    [adjustment] = await adjustments(db)
    assert adjustment.status == STATUS_CLINIC_KEPT
    assert texts(opening, tenant) == [
        "O valor da sua consulta baixou. A diferença de R$ 30,00 do sinal será descontada no "
        "dia da consulta."
    ]


async def test_a_rearmed_notice_never_abates_the_same_difference_twice(db, provider):
    """UB1 re-review note: a later edit re-arms an answered notice; the settled abatement is
    netted, so the same price opens as "nothing changes"."""
    start = NOW + timedelta(days=1)
    tenant, patient, appointment, _deposit = await seed(db, start=start)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 100,00")
    assert (await open_(db, tenant.id, rid, patient.id)).kind == "abated"
    async with db() as session:  # the sweep re-arms it after another price edit
        row = await session.get(AppointmentReminder, rid)
        row.answered_at = row.answer = None
        await session.commit()

    opening = await open_(db, tenant.id, rid, patient.id, now=NOW + timedelta(minutes=5))

    assert (opening.kind, opening.diff_cents) == ("unchanged", 0)
    assert texts(opening, tenant) == [price_change.PRICE_BACK_TEXT]
    assert len(await adjustments(db)) == 1


@pytest.mark.parametrize(
    ("price", "diff", "text"),
    [
        ("R$ 200,00", 0, price_change.PRICE_BACK_TEXT),  # back to the price paid
        ("R$ 220,00", 600, price_change.PRICE_SMALL_TEXT),  # + R$ 6: under R$ 10
        (None, None, price_change.PRICE_NOTHING_TEXT),  # no readable price any more
    ],
)
async def test_nothing_changes_when_the_price_is_back_small_or_unreadable(
    db, provider, price, diff, text
):
    tenant, patient, appointment, _deposit = await seed(db)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, price)

    opening = await open_(db, tenant.id, rid, patient.id)

    assert (opening.kind, opening.diff_cents) == ("unchanged", diff)
    assert texts(opening, tenant) == [text]
    assert provider.calls == [] and await adjustments(db) == []
    assert (await notice(db, rid)).answered_at is not None


async def test_another_open_question_comes_first_and_the_link_stays_unanswered(db, provider):
    tenant, patient, appointment, deposit = await seed(db)
    async with db() as session:
        question = DepositAdjustment(
            id=uuid4(),
            tenant_id=tenant.id,
            deposit_id=deposit.id,
            appointment_id=appointment.id,
            kind=KIND_DECREASE,
            status=STATUS_ASKED,
            old_service="Exame",
            new_service="Consulta",
            diff_cents=3000,
            asked_at=NOW - timedelta(hours=1),
        )
        session.add(question)
        await session.commit()
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")

    opening = await open_(db, tenant.id, rid, patient.id)

    assert (opening.kind, opening.adjustment_id, opening.expires_at) == (
        "pending",
        question.id,
        None,
    )
    assert texts(opening, tenant) == [price_change.PRICE_ANSWER_ABOVE_TEXT]
    assert (await notice(db, rid)).answered_at is None
    assert provider.calls == []


# ------------------------------------------------------------------ sinal not paid yet


async def test_an_unpaid_sinal_voids_the_old_pix_and_the_caller_recharges(
    db, provider, monkeypatch
):
    tenant, patient, appointment, deposit = await seed(db, awaiting=True)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")  # sinal R$ 60 -> R$ 90

    opening = await open_(db, tenant.id, rid, patient.id)

    assert (opening.kind, opening.old_amount_cents) == ("requoted", 6000)
    assert texts(opening, tenant) == []  # the new Pix message is the first message
    assert provider.called("cancel_charge") == 1
    async with db() as session:
        old = await session.get(PixDeposit, deposit.id)
    assert (old.status, old.appointment_id) == (PixDepositStatus.EXPIRED, None)
    assert (await notice(db, rid)).answered_at is not None

    sent: list = []

    async def fake_deliver(session, tenant_, patient_, messages, **kw):
        sent.extend(m.body for m in messages)

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    new_id = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        old_amount_cents=opening.old_amount_cents,
        redis=FakeRedis(),
        reason="price_change",
    )
    assert new_id is not None
    [body] = sent
    assert body.startswith(deposit_lifecycle.PRICE_REQUOTE_OPENING_TEXT)
    assert "R$ 90,00" in body and "COPYDIFF" in body


async def test_an_unpaid_sinal_under_the_minimum_is_only_voided_and_told(db, provider, monkeypatch):
    tenant, patient, appointment, _deposit = await seed(db, awaiting=True)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 10,00")  # sinal R$ 3: under the provider's R$ 5
    assert (await open_(db, tenant.id, rid, patient.id)).kind == "requoted"
    sent: list = []

    async def fake_deliver(session, tenant_, patient_, messages, **kw):
        sent.extend(m.body for m in messages)

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        old_amount_cents=6000,
        reason="price_change",
    )
    assert created is None and provider.called("create_pix_charge") == 0
    assert sent == [
        f"{deposit_lifecycle.PRICE_REQUOTE_OPENING_TEXT} "
        f"{deposit_lifecycle.PRICE_REQUOTE_NO_SINAL_TEXT}"
    ]


async def test_a_failed_new_pix_tells_the_patient_and_opens_one_clinic_item(
    db, provider, monkeypatch
):
    tenant, patient, appointment, _deposit = await seed(db, awaiting=True)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")
    assert (await open_(db, tenant.id, rid, patient.id)).kind == "requoted"
    sent: list = []

    async def fake_deliver(session, tenant_, patient_, messages, **kw):
        sent.extend(m.body for m in messages)

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    provider.create_error = ProviderError("create_failed", status_code=500)
    for _ in range(2):  # a retry never opens a second item
        await deposit_lifecycle.recharge_after_edit(
            tenant_id=tenant.id,
            appointment_id=appointment.id,
            old_amount_cents=6000,
            reason="price_change",
        )
    assert sent[0] == (
        f"{deposit_lifecycle.PRICE_REQUOTE_OPENING_TEXT} "
        f"{deposit_lifecycle.REQUOTE_CLINIC_WILL_CALL_TEXT}"
    )
    async with db() as session:
        items = list(await session.scalars(select(PaymentAttentionItem)))
    assert [(i.reason, i.amount_cents) for i in items] == [(REASON_SINAL_REQUOTE_FAILED, 9000)]


async def test_an_edit_keeps_its_own_wording(db, provider, monkeypatch):
    """Characterization: `reason` defaults to "edit" - today's texts, byte for byte."""
    tenant, patient, appointment, _deposit = await seed(db, awaiting=True)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 10,00")
    await open_(db, tenant.id, rid, patient.id)
    sent: list = []

    async def fake_deliver(session, tenant_, patient_, messages, **kw):
        sent.extend(m.body for m in messages)

    monkeypatch.setattr(notify, "deliver", fake_deliver)
    await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=6000
    )
    assert sent == [
        f"{deposit_lifecycle.REQUOTE_OPENING_TEXT} {deposit_lifecycle.REQUOTE_NO_SINAL_TEXT}"
    ]


async def test_an_unpaid_sinal_of_a_service_switched_off_is_never_voided(db, provider):
    tenant, patient, appointment, deposit = await seed(db, awaiting=True)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, None)

    opening = await open_(db, tenant.id, rid, patient.id)

    assert (opening.kind, opening.diff_cents) == ("unchanged", None)
    assert provider.calls == []
    async with db() as session:
        assert (await session.get(PixDeposit, deposit.id)).status == PixDepositStatus.AWAITING


async def test_an_unpaid_sinal_being_paid_answers_retry(db, provider):
    tenant, patient, appointment, deposit = await seed(db, awaiting=True)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")
    provider.state = ChargeState(PaymentStatus.PAID)

    opening = await open_(db, tenant.id, rid, patient.id)

    assert opening.kind == "retry"
    assert provider.called("cancel_charge") == 0
    assert (await notice(db, rid)).answered_at is None
    async with db() as session:
        assert (await session.get(PixDeposit, deposit.id)).status == PixDepositStatus.AWAITING


# ------------------------------------------------------------------ closed, close, foreign


@pytest.mark.parametrize("state", ["cancelled", "started"])
async def test_a_consultation_no_longer_live_says_nothing_changes(db, provider, state):
    tenant, patient, appointment, _deposit = await seed(db)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")
    now = NOW
    if state == "cancelled":
        async with db() as session:
            row = await session.get(Appointment, appointment.id)
            row.status = AppointmentStatus.CANCELLED
            await session.commit()
    else:
        now = START + timedelta(minutes=5)

    opening = await open_(db, tenant.id, rid, patient.id, now=now)

    assert opening.kind == "closed"
    assert texts(opening, tenant) == [price_change.PRICE_CLOSED_TEXT]
    assert provider.calls == [] and await adjustments(db) == []


async def test_within_an_hour_of_the_visit_the_difference_is_settled_on_the_day(db, provider):
    tenant, patient, appointment, _deposit = await seed(db)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")

    opening = await open_(db, tenant.id, rid, patient.id, now=START - timedelta(minutes=50))

    assert opening.kind == "too_close" and provider.calls == []


async def test_a_clinic_that_switched_the_sinal_off_changes_nothing(db, provider):
    tenant, patient, appointment, _deposit = await seed(db)
    rid = await add_notice(db, appointment)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.pix_deposit_enabled = False
        row.appointment_types = catalog("R$ 300,00")
        await session.commit()

    opening = await open_(db, tenant.id, rid, patient.id)

    assert opening.kind == "unchanged" and provider.calls == []


async def test_links_that_are_not_this_patients_current_notice_are_not_found(db, provider):
    tenant, patient, appointment, _deposit = await seed(db)
    other_tenant, other_patient, other_appointment, _d = await seed(db)
    await reprice(db, tenant.id, "R$ 300,00")
    mine = await add_notice(db, appointment)
    old_version = await add_notice(db, appointment, start=START - timedelta(days=1))
    invalidated = await add_notice(db, appointment, invalidated=True)
    theirs = await add_notice(db, other_appointment)
    async with db() as session:  # a reminder of another kind, same consultation
        day = AppointmentReminder(
            id=uuid4(),
            tenant_id=tenant.id,
            appointment_id=appointment.id,
            patient_id=patient.id,
            kind="day",
            appointment_start_at=appointment.start_at,
            due_at=CHANGED_AT,
            status="sent",
            channel="whatsapp",
            with_prompt=True,
        )
        session.add(day)
        await session.commit()

    cases = [
        (tenant.id, uuid4(), patient.id),  # broken link: no such row
        (tenant.id, mine, other_patient.id),  # another patient opening my link
        (other_tenant.id, mine, patient.id),  # another clinic
        (tenant.id, theirs, patient.id),  # another clinic's row
        (tenant.id, old_version, patient.id),  # the consultation moved since the e-mail
        (tenant.id, invalidated, patient.id),
        (tenant.id, day.id, patient.id),  # not a price notice
    ]
    for tenant_id, rid, patient_id in cases:
        opening = await open_(db, tenant_id, rid, patient_id)
        assert opening == price_change.PriceOpening(kind="not_found")
        assert texts(opening, tenant) == []
    assert provider.calls == [] and await adjustments(db) == []
    assert (await notice(db, mine)).answered_at is None
    # ... and the owner still gets the action.
    assert (await open_(db, tenant.id, mine, patient.id)).kind == "pix"


async def test_a_suspended_consultation_is_not_found(db, provider):
    tenant, patient, appointment, deposit = await seed(db)
    async with db() as session:
        session.add(
            DepositAdjustment(
                id=uuid4(),
                tenant_id=tenant.id,
                deposit_id=deposit.id,
                appointment_id=appointment.id,
                kind=KIND_INCREASE,
                mode=MODE_SUSPEND,
                status=STATUS_AWAITING_PAYMENT,
                old_service="Consulta",
                new_service="Exame",
                diff_cents=3000,
                expires_at=NOW + timedelta(minutes=30),
            )
        )
        await session.commit()
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 300,00")

    opening = await open_(db, tenant.id, rid, patient.id)

    assert opening.kind == "not_found"
    assert (await notice(db, rid)).answered_at is None


async def test_every_kind_has_a_message_rule():
    """No kind falls through silently except the two whose first message is elsewhere."""
    tenant = Tenant(id=uuid4(), clinic_name="C", timezone="America/Sao_Paulo")
    silent = {"requoted", "not_found"}
    from typing import get_args

    for kind in get_args(price_change.PriceOpeningKind):
        opening = price_change.PriceOpening(
            kind=kind, adjustment_id=uuid4(), diff_cents=1000, pix_copy_paste="X"
        )
        messages = price_change.price_opening_messages(opening, tenant=tenant)
        assert bool(messages) is (kind not in silent), kind
        for message in messages:
            assert "None" not in message.body and len(message.body) <= 1024


# ------------------------------------------------------------------ the sweep's arithmetic


async def test_the_sweep_does_not_mail_about_an_abatement_already_told(db, provider):
    """UA2/UB1 pending item: `price_notice_needed` uses `preview_price_requote` - the opening's
    own arithmetic - so a drop already abated is not e-mailed again after another edit."""
    start = NOW + timedelta(days=1)
    tenant, patient, appointment, deposit = await seed(db, start=start)
    rid = await add_notice(db, appointment)
    await reprice(db, tenant.id, "R$ 100,00")
    assert (await open_(db, tenant.id, rid, patient.id)).kind == "abated"

    async with db() as session:
        row_tenant = await session.get(Tenant, tenant.id)
        row = await session.get(Appointment, appointment.id)
        dep = await session.get(PixDeposit, deposit.id)
        needed = await price_change.price_notice_needed(
            session, tenant=row_tenant, appointment=row, deposit=dep, now=NOW
        )
        before = await service_change.preview_service_change(
            session,
            tenant=row_tenant,
            appointment=row,
            deposit=dep,
            new_service="Consulta",
            professional_id=None,
            start_at=row.start_at,
            now=NOW,
        )
    assert before.action == "decrease_keep" and before.diff_cents == 3000  # the old reading
    assert needed is False
    async with db() as session:
        assert await session.scalar(select(func.count()).select_from(DepositAdjustment)) == 1
