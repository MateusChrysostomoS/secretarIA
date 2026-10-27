"""The service change of an appointment with a PAID sinal (services/payments/service_change.py).

Spec: docs/superpowers/specs/2026-10-26-troca-servico-diferenca-sinal-design.md (Task 5).
Prices of the seeded clinic (sinal of 30 %): Consulta R$ 200 (sinal R$ 60, the paid one),
Avaliação R$ 300 (R$ 90: +R$ 30 -> Pix of the difference), Consulta plus R$ 230 (R$ 69: +R$ 9 ->
changes without a Pix), Retorno R$ 100 (R$ 30: -R$ 30 -> asks or the clinic keeps it),
Sem preço (no price -> blocked). Every provider call goes to a scripted `FakeProvider`.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Patient,
    PaymentAttentionItem,
    PaymentWebhookEvent,
    PixDeposit,
    Tenant,
)
from secretaria.models.clinic_payment_account import ACCOUNT_CONNECTED
from secretaria.models.deposit_adjustment import (
    KIND_DECREASE,
    KIND_INCREASE,
    STATUS_APPLIED,
    STATUS_ASKED,
    STATUS_AWAITING_PAYMENT,
    STATUS_CLINIC_KEPT,
    STATUS_EXPIRED,
    STATUS_KEPT_CREDIT,
    STATUS_REFUND_REQUESTED,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import REASON_SERVICE_DIFF_REFUND_DUE
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import accounts, jobs, registry, service_change
from secretaria.services.payments.provider import (
    ChargeState,
    CreatedCharge,
    PaymentStatus,
    ProviderError,
)
from tests.payments_support import FakeProvider, make_sessionmaker, recording_enqueue

NOW = datetime(2026, 10, 26, 15, 0, tzinfo=UTC)

CATALOG = [
    {"name": "Consulta", "price": "R$ 200,00", "duration_min": 30, "is_active": True},
    {"name": "Avaliação", "price": "R$ 300,00", "duration_min": 60, "is_active": True},
    {"name": "Consulta plus", "price": "R$ 230,00", "duration_min": 30, "is_active": True},
    {"name": "Retorno", "price": "R$ 100,00", "duration_min": 20, "is_active": True},
    {"name": "Sem preço", "duration_min": 30, "is_active": True},
]


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(state=ChargeState(status=PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201pix")
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


@pytest.fixture(autouse=True)
def no_default_hooks(monkeypatch):
    """The default patient notices are tested on their own; here the hooks only record."""
    monkeypatch.setattr(service_change, "ADJUSTMENT_EXPIRED_HOOKS", [])
    monkeypatch.setattr(service_change, "ADJUSTMENT_APPLIED_HOOKS", [])
    # The expiry sweep's rotating start is per process: every test begins at the head.
    if hasattr(service_change, "_SWEEP_OFFSET"):
        monkeypatch.setattr(service_change, "_SWEEP_OFFSET", 0)
    if hasattr(service_change, "_VOID_OFFSET"):
        monkeypatch.setattr(service_change, "_VOID_OFFSET", 0)


async def _seed(
    session,
    *,
    days_ahead: float = 5,
    min_days: int = 2,
    deposit_status=PixDepositStatus.PAID,
    amount=6000,
):
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinic",
        phone_number_id=str(uuid4())[:12],
        is_active=True,
        timezone="America/Sao_Paulo",
        pix_deposit_enabled=True,
        pix_deposit_percent=30,
        pix_difference_refund_min_days=min_days,
        deposit_hold_ttl_minutes=30,
        appointment_types=CATALOG,
    )
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511999999999", name="Maria")
    session.add_all([tenant, patient])
    await session.flush()
    row = await accounts.get_or_create_account_row(session, tenant_id=tenant.id, provider="asaas")
    cfg.set_payment_account_secrets(row, api_key="key", webhook_token="token")
    row.status = ACCOUNT_CONNECTED
    start = NOW + timedelta(days=days_ahead)
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id="evt-1",
        appointment_type="Consulta",
        start_at=start,
        end_at=start + timedelta(minutes=30),
        phone=patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
    )
    session.add(appointment)
    await session.flush()
    deposit = PixDeposit(
        tenant_id=tenant.id,
        appointment_id=appointment.id,
        patient_id=patient.id,
        amount_cents=amount,
        percent_applied=30,
        status=deposit_status,
        provider="asaas",
        provider_charge_id="pay_sinal",
        external_reference="dep-ref",
        deposit_timing="after",
        paid_amount_cents=amount if deposit_status == PixDepositStatus.PAID else None,
    )
    session.add(deposit)
    await session.commit()
    return tenant, patient, appointment, deposit


def _edit(appointment, service: str, minutes: int = 30) -> dict:
    """The R6 edit dict a caller (patient flow / staff edit) hands over: same slot."""
    return {
        "appointment_id": appointment.id,
        "appointment_type": service,
        "professional_id": None,
        "insurance": None,
        "attendee_name": None,
        "google_event_id": appointment.google_event_id,
        "google_event_link": None,
        "start_at": appointment.start_at,
        "end_at": appointment.start_at + timedelta(minutes=minutes),
        "time_changed": False,
        "doctor_changed": False,
        "calendar_changed": False,
    }


async def _request(db, seeded, service: str, *, minutes: int = 30, now=NOW):
    tenant, _patient, appointment, deposit = seeded
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        appointment = await session.get(Appointment, appointment.id)
        deposit = await session.get(PixDeposit, deposit.id)
        result = await service_change.request_service_change(
            session,
            tenant=tenant,
            appointment=appointment,
            deposit=deposit,
            new_service=service,
            pending_edit=_edit(appointment, service, minutes),
            now=now,
        )
        await session.commit()
        return result


async def _appointment(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _adjustments(db) -> list[DepositAdjustment]:
    async with db() as session:
        return list((await session.scalars(select(DepositAdjustment))).all())


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


async def _notices(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(ProfessionalEditNotice))


# --------------------------------------------------------------------------
# dearer: a Pix of ONLY the difference, the service changes only when it is paid
# --------------------------------------------------------------------------


async def test_dearer_service_creates_the_difference_pix_and_keeps_the_old_service(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    result = await _request(db, seeded, "Avaliação", minutes=60)

    assert result.kind == "awaiting_payment"
    assert (result.diff_cents, result.pix_copy_paste) == (3000, "000201pix")
    [adjustment] = await _adjustments(db)
    assert result.adjustment_id == adjustment.id
    assert (adjustment.kind, adjustment.status) == (KIND_INCREASE, STATUS_AWAITING_PAYMENT)
    assert (adjustment.old_service, adjustment.new_service) == ("Consulta", "Avaliação")
    assert adjustment.diff_cents == 3000
    assert adjustment.provider_charge_id == "pay_diff_1"
    assert adjustment.deposit_id == seeded[3].id
    assert adjustment.appointment_id == seeded[2].id
    expires = adjustment.expires_at.replace(tzinfo=UTC)
    assert expires == NOW + timedelta(minutes=30)
    assert adjustment.pending_edit["appointment_type"] == "Avaliação"
    # The charge: ONLY the difference, referenced by the adjustment's own id.
    [(_name, request)] = [c for c in provider.calls if c[0] == "create_pix_charge"]
    assert request.amount_cents == 3000
    assert request.external_reference == str(adjustment.id)
    # Nothing moved on the consultation, and the doctor was not told anything yet.
    row = await _appointment(db, seeded[2].id)
    assert row.appointment_type == "Consulta"
    assert (row.end_at - row.start_at) == timedelta(minutes=30)
    assert await _notices(db) == 0
    deposit = await _deposit(db, seeded[3].id)
    assert deposit.amount_cents == 6000


async def test_dearer_service_is_charged_even_on_the_day_of_the_visit(db, provider):
    async with db() as session:
        seeded = await _seed(session, days_ahead=0.2, min_days=5)
    result = await _request(db, seeded, "Avaliação")
    assert result.kind == "awaiting_payment" and result.diff_cents == 3000


async def test_a_second_change_while_a_difference_is_unpaid_is_refused(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    first = await _request(db, seeded, "Avaliação")
    second = await _request(db, seeded, "Retorno")

    assert first.kind == "awaiting_payment"
    assert (second.kind, second.reason) == ("blocked", "adjustment_pending")
    assert provider.called("create_pix_charge") == 1
    assert len(await _adjustments(db)) == 1
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"


async def test_a_change_while_the_refund_question_is_open_is_refused(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    first = await _request(db, seeded, "Retorno")
    second = await _request(db, seeded, "Avaliação")

    assert first.kind == "asked"
    assert (second.kind, second.reason) == ("blocked", "refund_question_pending")
    assert provider.called("create_pix_charge") == 0


async def test_a_small_increase_changes_at_once_without_a_pix(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    result = await _request(db, seeded, "Consulta plus")

    assert (result.kind, result.diff_cents) == ("applied_now", 900)
    assert provider.called("create_pix_charge") == 0
    assert await _adjustments(db) == []
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta plus"
    assert result.changed_fields == ("serviço",)
    assert await _notices(db) == 1  # the existing "Consulta alterada" outbox, once


async def test_the_pix_failing_at_the_provider_applies_now_and_the_clinic_collects(db, provider):
    """Follow-up B (2026-10-27): the provider down never makes the patient redo the change -
    details in tests/test_service_change_provider_down.py."""
    provider.create_error = ProviderError("boom", status_code=500, retryable=True)
    async with db() as session:
        seeded = await _seed(session)
    result = await _request(db, seeded, "Avaliação")

    assert (result.kind, result.decision) == ("applied_now", service_change.DECISION_COLLECT)
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Avaliação"
    [item] = await _items(db)
    assert item.amount_cents == 3000


# --------------------------------------------------------------------------
# cheaper: changes at once; asks only far enough ahead and from R$ 10
# --------------------------------------------------------------------------


async def test_cheaper_service_far_ahead_changes_and_asks(db, provider):
    async with db() as session:
        seeded = await _seed(session, days_ahead=5, min_days=2)
    result = await _request(db, seeded, "Retorno", minutes=20)

    assert (result.kind, result.diff_cents) == ("asked", 3000)
    [adjustment] = await _adjustments(db)
    assert result.adjustment_id == adjustment.id
    assert (adjustment.kind, adjustment.status) == (KIND_DECREASE, STATUS_ASKED)
    assert adjustment.provider_charge_id is None
    assert adjustment.asked_at is not None
    row = await _appointment(db, seeded[2].id)
    assert row.appointment_type == "Retorno"
    assert (row.end_at - row.start_at) == timedelta(minutes=20)
    assert await _items(db) == []  # nobody opened anything: the patient decides first
    assert provider.called("create_pix_charge") == 0
    assert await _notices(db) == 1


async def test_cheaper_service_close_to_the_visit_is_kept_by_the_clinic(db, provider):
    async with db() as session:
        seeded = await _seed(session, days_ahead=1, min_days=2)
    result = await _request(db, seeded, "Retorno")

    assert (result.kind, result.diff_cents) == ("applied_now", 3000)
    [adjustment] = await _adjustments(db)
    assert (adjustment.kind, adjustment.status) == (KIND_DECREASE, STATUS_CLINIC_KEPT)
    assert adjustment.resolved_at is not None
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Retorno"
    assert await _items(db) == []


async def test_unreadable_price_is_blocked_and_nothing_is_written(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    result = await _request(db, seeded, "Sem preço")

    assert (result.kind, result.reason) == ("blocked", "price_unavailable")
    assert await _adjustments(db) == []
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"
    assert await _notices(db) == 0


async def test_an_unpaid_sinal_is_not_this_flow(db, provider):
    async with db() as session:
        seeded = await _seed(session, deposit_status=PixDepositStatus.AWAITING)
    result = await _request(db, seeded, "Avaliação")
    assert (result.kind, result.reason) == ("blocked", "deposit_not_paid")
    assert await _adjustments(db) == []


async def test_a_deposit_of_another_appointment_is_a_programming_error(db, provider):
    async with db() as session:
        tenant, _patient, appointment, deposit = await _seed(session)
        other = Appointment(
            id=uuid4(), tenant_id=tenant.id, patient_id=appointment.patient_id,
            google_event_id="evt-2", appointment_type="Consulta", start_at=appointment.start_at,
            end_at=appointment.end_at, status=AppointmentStatus.SCHEDULED,
        )
        session.add(other)
        await session.commit()
        with pytest.raises(ValueError):
            await service_change.request_service_change(
                session, tenant=tenant, appointment=other, deposit=deposit,
                new_service="Avaliação", pending_edit=_edit(other, "Avaliação"), now=NOW,
            )


# --------------------------------------------------------------------------
# the patient's answer to "do you want the difference back?"
# --------------------------------------------------------------------------


async def _answer(db, seeded, adjustment_id, accept: bool, tenant_id=None, now=NOW):
    async with db() as session:
        outcome = await service_change.answer_refund_question(
            session,
            tenant_id=tenant_id or seeded[0].id,
            adjustment_id=adjustment_id,
            accept=accept,
            now=now,
        )
        await session.commit()
        return outcome


async def test_yes_opens_exactly_one_refund_item_with_the_difference(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    asked = await _request(db, seeded, "Retorno")
    first = await _answer(db, seeded, asked.adjustment_id, True)
    again = await _answer(db, seeded, asked.adjustment_id, True)

    assert (first, again) == ("refund_requested", "already_answered")
    [item] = await _items(db)
    assert item.reason == REASON_SERVICE_DIFF_REFUND_DUE
    assert item.suggested_refund_cents == 3000
    assert item.deposit_id == seeded[3].id
    assert item.service_label == "Retorno"
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_REFUND_REQUESTED and adjustment.resolved_at is not None


async def test_no_keeps_the_difference_as_credit_without_an_item(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    asked = await _request(db, seeded, "Retorno")
    outcome = await _answer(db, seeded, asked.adjustment_id, False)

    assert outcome == "kept_credit"
    assert await _items(db) == []
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_KEPT_CREDIT


async def test_an_answer_with_another_clinics_id_changes_nothing(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    asked = await _request(db, seeded, "Retorno")
    outcome = await _answer(db, seeded, asked.adjustment_id, True, tenant_id=uuid4())

    assert outcome == "not_found"
    assert await _items(db) == []
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_ASKED


# --------------------------------------------------------------------------
# the sweep: 24 h without an answer = "no"; an unpaid difference expires
# --------------------------------------------------------------------------


async def _expire(db, now, enqueue=None):
    async with db() as session:
        return await service_change.expire_adjustments(session, now=now, enqueue=enqueue)


async def test_an_unanswered_question_becomes_credit_after_24_hours(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    asked = await _request(db, seeded, "Retorno")

    assert await _expire(db, NOW + timedelta(hours=23)) == 0
    assert (await _adjustments(db))[0].status == STATUS_ASKED
    assert await _expire(db, NOW + timedelta(hours=24, minutes=1)) == 1
    [adjustment] = await _adjustments(db)
    assert adjustment.id == asked.adjustment_id
    assert adjustment.status == STATUS_KEPT_CREDIT
    assert await _items(db) == []
    # A late "yes" no longer opens anything.
    assert await _answer(db, seeded, asked.adjustment_id, True) == "already_answered"
    assert await _items(db) == []


async def test_an_unpaid_difference_expires_voids_the_pix_and_keeps_the_old_service(
    db, provider, monkeypatch
):
    seen: list = []

    async def hook(session, tenant, notice):
        seen.append(notice)

    monkeypatch.setattr(service_change, "ADJUSTMENT_EXPIRED_HOOKS", [hook])
    async with db() as session:
        seeded = await _seed(session)
    await _request(db, seeded, "Avaliação")

    assert await _expire(db, NOW + timedelta(minutes=29)) == 0
    assert provider.called("cancel_charge") == 0
    assert await _expire(db, NOW + timedelta(minutes=31)) == 1

    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_EXPIRED and adjustment.resolved_at is not None
    assert provider.called("cancel_charge") == 1
    assert (await _appointment(db, seeded[2].id)).appointment_type == "Consulta"
    [notice] = seen
    assert notice.adjustment_id == adjustment.id
    assert (notice.old_service, notice.new_service, notice.diff_cents) == (
        "Consulta", "Avaliação", 3000,
    )
    assert notice.patient_id == seeded[1].id
    # A later sweep does not expire it (or notify) twice.
    assert await _expire(db, NOW + timedelta(minutes=40)) == 0
    assert len(seen) == 1


async def test_a_difference_paid_without_webhook_is_reconciled_not_expired(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    created = await _request(db, seeded, "Avaliação")
    provider.state = ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=3000,
        external_reference=str(created.adjustment_id),
    )
    sent, enqueue = recording_enqueue()

    assert await _expire(db, NOW + timedelta(minutes=31), enqueue) == 0
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_AWAITING_PAYMENT
    assert provider.called("cancel_charge") == 0
    [(job, row_id)] = sent
    assert job == "process_payment_event"
    async with db() as session:
        event = await session.get(PaymentWebhookEvent, __import__("uuid").UUID(row_id))
    assert event.provider_charge_id == "pay_diff_1"
    assert event.tenant_id == seeded[0].id


async def test_provider_down_never_expires_blind(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    await _request(db, seeded, "Avaliação")
    provider.get_error = ProviderError("down", status_code=503, retryable=True)

    assert await _expire(db, NOW + timedelta(minutes=31)) == 0
    [adjustment] = await _adjustments(db)
    assert adjustment.status == STATUS_AWAITING_PAYMENT
    assert provider.called("cancel_charge") == 0


async def test_jobs_sweep_runs_the_adjustment_expiry(db, provider):
    async with db() as session:
        seeded = await _seed(session)
    await _request(db, seeded, "Retorno")
    _, enqueue = recording_enqueue()
    async with db() as session:
        count = await jobs.expire_due_adjustments(
            session, now=NOW + timedelta(days=2), enqueue=enqueue
        )
    assert count == 1
    assert (await _adjustments(db))[0].status == STATUS_KEPT_CREDIT


# --------------------------------------------------------------------------
# money held: a paid difference counts for the next change
# --------------------------------------------------------------------------


async def test_held_deposit_counts_paid_increases_and_requested_refunds(db, provider):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        session.add_all(
            [
                DepositAdjustment(
                    tenant_id=tenant.id, deposit_id=deposit.id, appointment_id=appointment.id,
                    kind=KIND_INCREASE, status=STATUS_APPLIED, old_service="Consulta",
                    new_service="Avaliação", diff_cents=3000, provider_charge_id="pay_x",
                ),
                DepositAdjustment(
                    tenant_id=tenant.id, deposit_id=deposit.id, appointment_id=appointment.id,
                    kind=KIND_DECREASE, status=STATUS_REFUND_REQUESTED, old_service="Avaliação",
                    new_service="Consulta plus", diff_cents=2100,
                ),
                DepositAdjustment(
                    tenant_id=tenant.id, deposit_id=deposit.id, appointment_id=appointment.id,
                    kind=KIND_INCREASE, status=STATUS_EXPIRED, old_service="Consulta",
                    new_service="Avaliação", diff_cents=3000, provider_charge_id="pay_y",
                ),
            ]
        )
        await session.commit()
        held = await service_change.held_deposit_cents(session, deposit)
    assert held == 6000 + 3000 - 2100


async def _deposit(db, deposit_id) -> PixDeposit:
    async with db() as session:
        return await session.get(PixDeposit, deposit_id)


# --------------------------------------------------------------------------
# the default patient notices (registered hooks) and the copy
# --------------------------------------------------------------------------


def test_copy_never_promises_an_automatic_refund():
    question = service_change.refund_question_text("Retorno", 3000)
    assert "R$ 30,00" in question and "Retorno" in question
    assert "automátic" not in question.lower()
    pix = service_change.difference_pix_text("Avaliação", 3000, "000201pix", 30)
    assert "R$ 30,00" in pix and "só muda" in pix
    assert pix.rstrip().endswith("```000201pix```")  # the code, fenced, is the LAST thing
    assert "```" not in service_change.difference_pix_text("Avaliação", 3000, None, 30)
    expired = service_change.adjustment_expired_text("Consulta", "Avaliação")
    assert "Consulta" in expired and "Avaliação" in expired
    labels = [label for _id, label in service_change.REFUND_QUESTION_BUTTONS]
    assert labels == ["Sim, quero de volta", "Não, abater no valor"]


async def test_default_expired_hook_tells_the_patient(db, provider, monkeypatch):
    sent: list = []

    async def fake_deliver(session, tenant, patient, outgoing, **kw):
        sent.append((patient.id, outgoing))

    monkeypatch.setattr(service_change.notify, "deliver", fake_deliver)
    async with db() as session:
        tenant, patient, appointment, deposit = await _seed(session)
        notice = service_change.AdjustmentNotice(
            adjustment_id=uuid4(), tenant_id=tenant.id, appointment_id=appointment.id,
            patient_id=patient.id, kind=KIND_INCREASE, old_service="Consulta",
            new_service="Avaliação", diff_cents=3000,
        )
        await service_change.notify_patient_adjustment_expired(session, tenant, notice)
    [(patient_id, [text])] = sent
    assert patient_id == patient.id
    assert "Avaliação" in text.body
