"""TASK-085 R / S2 - D-R6: an edit of a consultation whose Pix sinal is NOT paid yet.

`requote_awaiting` runs inside the edit's transaction (after the edit was flushed): same amount =
nothing; otherwise the provider is asked first (a paid charge is never voided), the old Pix is
voided and its row detached (a late payment becomes an ORPHAN item for the clinic, never lost).
`recharge_after_edit` runs after the commit: the new sinal, opening with the "o valor mudou"
sentence; 0 = only the notice; `old_amount_cents=None` = the edit made a sinal due (OQ-10, both
timings). Characterization first: a clinic without sinal and a sinal whose amount did not move
behave exactly as today (no provider call, no message).
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    Conversation,
    FlowState,
    Patient,
    Tenant,
)
from secretaria.models.insurance import TenantInsurancePlan
from secretaria.models.payment_attention_item import (
    REASON_ORPHAN_PAYMENT,
    REASON_SINAL_REQUOTE_FAILED,
    PaymentAttentionItem,
)
from secretaria.models.pix_deposit import (
    DEPOSIT_TIMING_AFTER,
    DEPOSIT_TIMING_BEFORE,
    PixDeposit,
    PixDepositStatus,
)
from secretaria.services import tenant_config as cfg
from secretaria.services.payments import (
    accounts,
    adjustment_queries,
    deposit_lifecycle,
    event_processor,
    notify,
    payer_data,
    payer_data_after,
    registry,
)
from secretaria.services.payments.provider import (
    ChargeState,
    CreatedCharge,
    PaymentStatus,
    ProviderError,
)
from tests.payments_support import FakeProvider, FakeRedis, make_event, make_sessionmaker

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
START = datetime(2026, 11, 25, 13, 0, tzinfo=UTC)
CATALOG = [
    {"name": "Consulta", "duration_min": 30, "is_active": True, "price": "R$ 200,00"},
    {"name": "Exame", "duration_min": 30, "is_active": True, "price": "R$ 300,00"},
    {"name": "Curativo", "duration_min": 30, "is_active": True, "price": "R$ 10,00"},
    {"name": "Avaliação", "duration_min": 30, "is_active": True, "price": ""},
]
OPENING = (
    "O valor do sinal mudou com a alteração; o Pix anterior foi cancelado (não pague aquele código)"
)


@pytest_asyncio.fixture
async def db(monkeypatch):
    engine, maker = await make_sessionmaker()
    monkeypatch.setattr(deposit_lifecycle, "async_session_factory", maker)
    monkeypatch.setattr(notify, "async_session_factory", maker)
    yield maker
    await engine.dispose()


class _Whatsapp:
    persists_outbound = False
    sent: list = []

    def __init__(self, *a, **k):
        pass

    @classmethod
    def for_tenant(cls, tenant, waba_token):
        return cls()

    async def send_text_message(self, to, body):
        _Whatsapp.sent.append((to, body))
        return {}

    async def send_buttons(self, to, body, buttons):
        _Whatsapp.sent.append((to, body))
        return {}


@pytest.fixture(autouse=True)
def _whatsapp(monkeypatch):
    _Whatsapp.sent = []
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", _Whatsapp)
    monkeypatch.setattr(notify, "WhatsAppClient", _Whatsapp)


@pytest.fixture
def provider(monkeypatch) -> FakeProvider:
    fake = FakeProvider(name="asaas", state=ChargeState(PaymentStatus.PENDING))
    fake.created_charge = CreatedCharge(
        provider_charge_id="pay_new", copy_paste="COPYNEW", provider_customer_id="cus_1"
    )
    monkeypatch.setitem(registry._PROVIDERS, "asaas", fake)
    return fake


async def _seed(
    session,
    *,
    service="Consulta",
    timing=DEPOSIT_TIMING_AFTER,
    enabled=True,
    with_account=True,
    deposit_amount: int | None = 6000,
    deposit_status=PixDepositStatus.AWAITING,
    external_reference: str | None = "dep-ref",
    charge_id: str = "pay_old",
):
    tenant = Tenant(
        id=uuid4(),
        clinic_name="Clinica",
        phone_number_id=str(uuid4())[:12],
        pix_deposit_enabled=enabled,
        payment_provider="asaas",
        appointment_types=CATALOG,
        deposit_timing=timing,
    )
    patient = Patient(id=uuid4(), tenant_id=tenant.id, wa_id="5511900000000", name="Paciente")
    session.add_all([tenant, patient])
    await session.flush()
    appointment = Appointment(
        id=uuid4(),
        tenant_id=tenant.id,
        patient_id=patient.id,
        google_event_id="evt-1",
        appointment_type=service,
        phone=patient.wa_id,
        status=AppointmentStatus.SCHEDULED,
        start_at=START,
        end_at=START + timedelta(minutes=30),
    )
    session.add(appointment)
    await session.flush()
    deposit = None
    if deposit_amount is not None:
        deposit = PixDeposit(
            id=uuid4(),
            tenant_id=tenant.id,
            appointment_id=appointment.id,
            patient_id=patient.id,
            provider="asaas",
            provider_charge_id=charge_id,
            asaas_payment_id=charge_id,
            external_reference=external_reference,
            amount_cents=deposit_amount,
            percent_applied=30,
            pix_copy_paste="COPYOLD",
            status=deposit_status,
            deposit_timing=DEPOSIT_TIMING_AFTER,
        )
        session.add(deposit)
    if with_account:
        row = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        cfg.set_payment_account_secrets(row, api_key="key", webhook_token="tok")
        row.status = "connected"
    await session.commit()
    return tenant, patient, appointment, deposit


async def _edit_and_requote(session, tenant, appointment, deposit, **edit):
    """The caller's side: write the edit, flush, then ask for the requote."""
    for key, value in edit.items():
        setattr(appointment, key, value)
    await session.flush()
    return await deposit_lifecycle.requote_awaiting(
        session, tenant=tenant, appointment=appointment, deposit=deposit, now=NOW
    )


# ------------------------------------------------------------------ characterization


async def test_same_amount_is_unchanged_and_never_calls_the_provider(db, provider):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        # The professional changed, the price did not: nothing to requote.
        outcome = await _edit_and_requote(session, tenant, appointment, deposit, phone="5511")
        assert outcome == "unchanged"
        assert deposit.status is PixDepositStatus.AWAITING
        assert deposit.appointment_id == appointment.id
    assert provider.calls == []


async def test_a_paid_sinal_is_not_this_flow(db, provider):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(
            session, deposit_status=PixDepositStatus.PAID
        )
        outcome = await _edit_and_requote(
            session, tenant, appointment, deposit, appointment_type="Exame"
        )
    assert outcome == "unchanged"
    assert provider.calls == []


async def test_a_clinic_without_sinal_gets_nothing_after_an_edit(db, provider):
    async with db() as session:
        tenant, _p, appointment, _d = await _seed(session, enabled=False, deposit_amount=None)
    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=None
    )
    assert created is None
    assert provider.calls == [] and _Whatsapp.sent == []


async def test_a_deposit_of_another_appointment_or_clinic_is_never_touched(db, provider):
    async with db() as session:
        tenant, _p, appointment, _d = await _seed(session)
        _t2, _p2, _a2, foreign = await _seed(session, charge_id="pay_foreign")
        outcome = await _edit_and_requote(
            session, tenant, appointment, foreign, appointment_type="Exame"
        )
        assert outcome == "unchanged"
        assert foreign.status is PixDepositStatus.AWAITING
    assert provider.calls == []


# ------------------------------------------------------------------ requote_awaiting


async def test_a_new_amount_voids_the_old_pix_and_detaches_the_row(db, provider):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        outcome = await _edit_and_requote(
            session, tenant, appointment, deposit, appointment_type="Exame"
        )
        assert outcome == "voided"
        await session.commit()
        row = await session.get(PixDeposit, deposit.id)
        assert row.status is PixDepositStatus.EXPIRED
        assert row.appointment_id is None
        assert row.resolved_at is not None
        assert row.external_reference == "dep-ref"
    # The provider is asked BEFORE the void (a paid charge is never voided).
    assert [c[0] for c in provider.calls] == ["get_charge", "cancel_charge"]
    assert provider.calls[1][1] == "pay_old"


async def test_a_paid_charge_blocks_the_edit_and_is_never_voided(db, provider):
    provider.state = ChargeState(PaymentStatus.PAID, paid_amount_cents=6000)
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        outcome = await _edit_and_requote(
            session, tenant, appointment, deposit, appointment_type="Exame"
        )
        assert outcome == "payment_in_flight"
        assert deposit.status is PixDepositStatus.AWAITING
        assert deposit.appointment_id == appointment.id
    assert provider.called("cancel_charge") == 0


async def test_a_row_paid_between_the_callers_read_and_the_call_blocks_the_edit(db, provider):
    """Fix round 1: the caller read the deposit AWAITING; the webhook paid it before the lock.
    Never "unchanged" (the edit would commit over a sinal paid at the old amount)."""
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        assert deposit.status is PixDepositStatus.AWAITING  # the caller's (stale) read
        async with db() as webhook:
            row = await webhook.get(PixDeposit, deposit.id)
            row.status = PixDepositStatus.PAID
            await webhook.commit()
        outcome = await _edit_and_requote(
            session, tenant, appointment, deposit, appointment_type="Exame"
        )
        assert outcome == "payment_in_flight"
        assert deposit.status is PixDepositStatus.PAID
        assert deposit.appointment_id == appointment.id
    assert provider.calls == []


@pytest.mark.parametrize("failure", ["read", "cancel", "no_account"])
async def test_a_failed_read_or_void_blocks_the_edit(db, provider, failure):
    if failure == "read":
        provider.get_error = ProviderError("timeout", status_code=503)
    if failure == "cancel":
        provider.cancel_error = ProviderError("cancel_failed", status_code=500)
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(
            session, with_account=failure != "no_account"
        )
        outcome = await _edit_and_requote(
            session, tenant, appointment, deposit, appointment_type="Exame"
        )
        assert outcome == "void_failed"
        assert deposit.status is PixDepositStatus.AWAITING
        assert deposit.appointment_id == appointment.id


async def test_a_legacy_no_sinal_convenio_leaves_the_unpaid_pix_alone(db, provider):
    """TASK-085 R D-R12: a convênio never exempts the sinal, so moving to a plan saved as "não
    cobra sinal" keeps the same amount - nothing is voided, the provider is never called."""
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        plan = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano X", charge_deposit=False)
        session.add(plan)
        await session.flush()
        outcome = await _edit_and_requote(
            session, tenant, appointment, deposit, insurance="Plano X", insurance_plan_id=plan.id
        )
        assert deposit.status is PixDepositStatus.AWAITING
    assert outcome == "unchanged"
    assert provider.calls == []


async def test_the_new_amount_uses_the_clinics_current_percentage(db, provider):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        tenant.pix_deposit_percent = 50
        await session.flush()
        cents = await deposit_lifecycle.requote_amount_cents(
            session, tenant=tenant, appointment=SimpleNamespace(
                appointment_type="Consulta", professional_id=None, unit_id=None,
                insurance_plan_id=None, insurance_professional_plan_id=None,
            )
        )
        assert cents == 10000
        # Same service, new percentage: the amount moved, so the old Pix goes.
        outcome = await _edit_and_requote(session, tenant, appointment, deposit, phone="1")
    assert outcome == "voided"


async def test_an_amount_under_the_provider_minimum_is_zero(db, provider):
    async with db() as session:
        tenant, _p, _a, _d = await _seed(session)
        cents = await deposit_lifecycle.requote_amount_cents(
            session, tenant=tenant, appointment=SimpleNamespace(appointment_type="Curativo")
        )
    assert cents == 0  # 30% of R$ 10 = R$ 3 < R$ 5 (Asaas)


async def test_a_legacy_row_without_reference_keeps_the_appointment_as_reference(db, provider):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session, external_reference=None)
        outcome = await _edit_and_requote(
            session, tenant, appointment, deposit, appointment_type="Exame"
        )
        assert outcome == "voided"
        # The reference the provider holds for it is still recognised after the detach.
        assert deposit.external_reference == str(appointment.id)
        assert event_processor.expected_reference(deposit) == str(appointment.id)


async def test_a_late_payment_of_the_voided_pix_is_an_orphan_item(db, provider):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        await _edit_and_requote(session, tenant, appointment, deposit, appointment_type="Exame")
        await session.commit()
        provider.state = ChargeState(
            PaymentStatus.PAID, paid_amount_cents=6000, external_reference="dep-ref"
        )
        account = await cfg.get_payment_account(session, tenant.id, "asaas")
        result = await event_processor.apply_normalized_event(
            session, tenant=tenant, account=account, provider=provider,
            event=make_event(charge_id="pay_old", reference="dep-ref"), now=NOW,
        )
        await session.commit()
        items = (await session.scalars(select(PaymentAttentionItem))).all()
        await session.refresh(appointment)
    assert result.outcome == "paid_appointment_missing"
    assert [i.reason for i in items] == [REASON_ORPHAN_PAYMENT]
    assert appointment.status is AppointmentStatus.SCHEDULED


async def test_the_deleted_webhook_of_the_voided_pix_leaves_the_edited_consultation(
    db, provider
):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(session)
        await _edit_and_requote(session, tenant, appointment, deposit, appointment_type="Exame")
        await session.commit()
        account = await cfg.get_payment_account(session, tenant.id, "asaas")
        result = await event_processor.apply_normalized_event(
            session, tenant=tenant, account=account, provider=provider,
            event=make_event(PaymentStatus.EXPIRED, charge_id="pay_old",
                             event_type="PAYMENT_DELETED"),
            now=NOW,
        )
        await session.commit()
        await session.refresh(appointment)
    assert result.outcome == "expired_replay"
    assert appointment.status is AppointmentStatus.SCHEDULED


# ------------------------------------------------------------------ recharge_after_edit


async def _voided(db, provider, **seed):
    async with db() as session:
        tenant, patient, appointment, deposit = await _seed(session, **seed)
        outcome = await _edit_and_requote(
            session, tenant, appointment, deposit, appointment_type="Exame"
        )
        assert outcome == "voided"
        await session.commit()
    provider.calls.clear()
    return tenant, patient, appointment, deposit


async def test_after_the_void_the_new_sinal_is_created_and_sent_in_one_message(db, provider):
    tenant, _p, appointment, old = await _voided(db, provider)
    new_id = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents,
        waba_token="wt",
    )
    assert new_id is not None
    async with db() as session:
        new = await session.get(PixDeposit, new_id)
    assert new.amount_cents == 9000  # 30% of R$ 300
    assert new.appointment_id == appointment.id
    assert new.status is PixDepositStatus.AWAITING
    assert new.deposit_timing == DEPOSIT_TIMING_AFTER
    assert provider.calls[0][1].amount_cents == 9000
    assert len(_Whatsapp.sent) == 1
    body = _Whatsapp.sent[0][1]
    assert body.startswith(OPENING)
    assert "R$ 90,00" in body and "COPYNEW" in body and "COPYOLD" not in body


async def test_a_new_amount_of_zero_only_tells_the_patient(db, provider, monkeypatch):
    tenant, _p, appointment, old = await _voided(db, provider)
    async with db() as session:
        row = await session.get(Tenant, tenant.id)
        row.pix_deposit_percent = 0
        await session.commit()
    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )
    assert created is None
    assert provider.called("create_pix_charge") == 0
    assert len(_Whatsapp.sent) == 1
    assert _Whatsapp.sent[0][1].startswith(OPENING)
    assert "não pede sinal" in _Whatsapp.sent[0][1]


CLINIC_WILL_CALL = deposit_lifecycle.REQUOTE_CLINIC_WILL_CALL_TEXT


async def _items(db):
    async with db() as session:
        return (await session.scalars(select(PaymentAttentionItem))).all()


async def test_a_provider_failure_opens_an_item_and_tells_the_patient_the_next_step(
    db, provider
):
    """Fix round 1 (ruling): the old Pix is dead and the new one was refused - the clinic gets an
    item to collect the new sinal and the patient is told the clinic will get in touch."""
    tenant, patient, appointment, old = await _voided(db, provider)
    provider.create_error = ProviderError("create_failed", status_code=500)
    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )
    assert created is None
    assert [body for _to, body in _Whatsapp.sent] == [f"{OPENING}. {CLINIC_WILL_CALL}"]
    items = await _items(db)
    assert len(items) == 1
    item = items[0]
    assert item.reason == REASON_SINAL_REQUOTE_FAILED and item.tenant_id == tenant.id
    assert item.amount_cents == 9000 and item.patient_id == patient.id
    assert item.deposit_id is None and item.service_label == "Exame"
    # A retry of the same edit never opens a second item.
    await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )
    assert len(await _items(db)) == 1


async def test_a_payer_question_that_cannot_be_asked_opens_the_item_too(db, provider):
    tenant, _p, appointment, old = await _voided(db, provider)
    provider.create_error = ProviderError("payer_document_required", status_code=400)
    # No Redis and no conversation: the question cannot go out.
    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )
    assert created is None
    assert [body for _to, body in _Whatsapp.sent] == [OPENING + ".", CLINIC_WILL_CALL]
    assert [i.reason for i in await _items(db)] == [REASON_SINAL_REQUOTE_FAILED]


async def test_a_sinal_made_due_that_the_provider_refuses_stays_silent(db, provider):
    """Characterization (OQ-10, nothing was voided): like a booking's own Pix, a refusal is
    only logged - no item, no message."""
    async with db() as session:
        tenant, _p, appointment, _d = await _seed(session, deposit_amount=None)
    provider.create_error = ProviderError("create_failed", status_code=500)
    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=None
    )
    assert created is None and _Whatsapp.sent == [] and await _items(db) == []


async def test_recharge_is_idempotent(db, provider):
    tenant, _p, appointment, old = await _voided(db, provider)
    first = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )
    second = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )
    assert first is not None and second is None
    assert provider.called("create_pix_charge") == 1


async def test_a_cancelled_consultation_is_never_recharged(db, provider):
    tenant, _p, appointment, old = await _voided(db, provider)
    async with db() as session:
        row = await session.get(Appointment, appointment.id)
        row.status = AppointmentStatus.CANCELLED
        await session.commit()
    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents
    )
    assert created is None and provider.calls == [] and _Whatsapp.sent == []


async def test_recharge_never_raises(db, provider, monkeypatch):
    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(deposit_lifecycle, "async_session_factory", _boom)
    assert (
        await deposit_lifecycle.recharge_after_edit(
            tenant_id=uuid4(), appointment_id=uuid4(), old_amount_cents=100
        )
        is None
    )


@pytest.mark.parametrize("timing", [DEPOSIT_TIMING_AFTER, DEPOSIT_TIMING_BEFORE])
async def test_an_edit_that_now_requires_a_sinal_charges_it_in_both_timings(
    db, provider, timing
):
    """OQ-10: no sinal before (a convênio without sinal), the edit made one due."""
    async with db() as session:
        tenant, _p, appointment, _d = await _seed(session, timing=timing, deposit_amount=None)
    new_id = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=None
    )
    assert new_id is not None
    async with db() as session:
        new = await session.get(PixDeposit, new_id)
    # The consultation already exists: the charge behaves as an `after` sinal in both timings.
    assert new.deposit_timing == DEPOSIT_TIMING_AFTER and new.amount_cents == 6000
    assert len(_Whatsapp.sent) == 1
    body = _Whatsapp.sent[0][1]
    assert not body.startswith(OPENING)
    assert "passou a pedir um sinal" in body and "COPYNEW" in body


async def test_the_before_hook_of_a_new_booking_still_skips(db, provider):
    """Characterization: only the edit path bypasses the `before` guard."""
    async with db() as session:
        tenant, patient, appointment, _d = await _seed(
            session, timing=DEPOSIT_TIMING_BEFORE, deposit_amount=None
        )
        created = await deposit_lifecycle.maybe_create_deposit(
            session, tenant=tenant, patient=patient, appointment=appointment, waba_token="wt"
        )
    assert created is None and provider.calls == []


async def test_a_provider_that_needs_the_cpf_asks_for_it_after_the_notice(
    db, provider, monkeypatch
):
    tenant, patient, appointment, old = await _voided(db, provider, timing=DEPOSIT_TIMING_BEFORE)
    async with db() as session:
        session.add(
            Conversation(
                id=uuid4(), tenant_id=tenant.id, patient_id=patient.id,
                flow_state=FlowState.IDLE,
            )
        )
        await session.commit()
    provider.create_error = ProviderError("payer_document_required", status_code=400)
    redis = FakeRedis()
    created = await deposit_lifecycle.recharge_after_edit(
        tenant_id=tenant.id, appointment_id=appointment.id, old_amount_cents=old.amount_cents,
        redis=redis,
    )
    assert created is None
    bodies = [body for _to, body in _Whatsapp.sent]
    assert bodies[0] == OPENING + "."
    assert len(bodies) == 2  # then the CPF question
    async with db() as session:
        conversation = await session.scalar(select(Conversation))
    assert conversation.flow_state is FlowState.AWAITING_PAYER_DATA

    # The answer arrives: the Pix of the (already existing) consultation is created even in a
    # `before` clinic.
    provider.create_error = None
    await payer_data.remember(
        redis, tenant_id=tenant.id, conversation_id=conversation.id, kind="document",
        value="52998224725",
    )
    async with db() as session:
        t = await session.get(Tenant, tenant.id)
        p = await session.get(Patient, patient.id)
        outcome = await payer_data_after.resume_after_booking(
            session, tenant=t, patient=p, conversation_id=conversation.id, waba_token="wt",
            redis=redis,
        )
        assert outcome == "created"
        new = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appointment.id)
        )
    assert new is not None and new.amount_cents == 9000


# ------------------------------------------------------------------ texts and the agenda view


def test_the_card_line_and_the_blocked_texts():
    assert deposit_lifecycle.requote_card_line_text(6000, 9000) == (
        "O sinal passa de R$ 60,00 para R$ 90,00; ao confirmar, cancelamos o Pix anterior e "
        "enviamos um novo."
    )
    zero = deposit_lifecycle.requote_card_line_text(6000, 0)
    assert "não pede sinal" in zero and "cancelamos o Pix anterior" in zero
    paid = deposit_lifecycle.requote_blocked_text("payment_in_flight")
    failed = deposit_lifecycle.requote_blocked_text("void_failed")
    assert "não foi feita" in paid and "não foi feita" in failed and paid != failed
    assert "a clínica vai falar com você sobre o sinal" in CLINIC_WILL_CALL


async def test_the_agenda_view_carries_the_pending_difference_mode(db, monkeypatch):
    async with db() as session:
        tenant, _p, appointment, deposit = await _seed(
            session, deposit_status=PixDepositStatus.PAID
        )

        async def _totals(session, tenant_id, deposit_ids):
            return {
                deposit.id: SimpleNamespace(
                    held_delta_cents=0, pending_difference_cents=3000,
                    pending_difference_mode="suspend",
                )
            }

        monkeypatch.setattr(adjustment_queries, "load_adjustment_totals", _totals)
        views = await deposit_lifecycle.load_deposit_views(session, tenant.id, [appointment.id])
    assert views[appointment.id].pending_difference_mode == "suspend"
    assert views[appointment.id].pending_difference_cents == 3000
