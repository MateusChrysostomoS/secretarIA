"""TASK-085 R / S1 - the money engine of a change on a PAID sinal (`request_paid_change`).

Contract: tasks/TASK-085/sinal-trocas/contrato.md §1 (decision table), §3.6. The seeded clinic of
tests/test_service_change.py (sinal of 30 %: Consulta R$ 200 -> R$ 60 paid; Avaliação R$ 300 ->
+R$ 30). What is pinned here:

* characterization: a cheaper change and a small increase are what they always were; a legacy
  deferred row (`mode` NULL) is born only in the "before" fallback without room;
* "after" (D-R5): a dearer change is WRITTEN NOW and a Pix of only the difference follows
  (`applied_awaiting_payment`, `mode="applied"`);
* "before" (D-R4): a dearer change writes nothing, suspends the old visit and HOLDS the new window
  (`suspended`, `mode="suspend"`); a new window that is not free is `blocked/slot_taken` and
  nothing is charged;
* doctor, unit and convênio are priced in (D-R1/D-R3); a convênio without sinal = new sinal 0;
* the patient's texts (D-R7 labels are S0's).
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    PixDeposit,
    Professional,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    KIND_DECREASE,
    KIND_INCREASE,
    MODE_APPLIED,
    MODE_SUSPEND,
    STATUS_ASKED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    DepositAdjustment,
)
from secretaria.models.insurance import TenantInsurancePlan
from secretaria.models.professional_edit_notice import ProfessionalEditNotice
from secretaria.services import booking_hold
from secretaria.services.payments import adjustment_suspension, service_change
from secretaria.services.payments.service_change import ChoiceOutcome
from tests import test_service_change as base
from tests._legacy_insurance_flag import set_legacy_flag

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks


@pytest.fixture(autouse=True)
def hold_clock(monkeypatch):
    """The holds read the wall clock; the engine reads `now`. Keep them on the test's clock."""
    monkeypatch.setattr(booking_hold, "_now", lambda: NOW)


async def _seed(db, *, timing="after", days_ahead: float = 5, **kw):
    async with db() as session:
        seeded = await base._seed(session, days_ahead=days_ahead, **kw)
        tenant = await session.get(Tenant, seeded[0].id)
        tenant.deposit_timing = timing
        await session.commit()
    return seeded


def _edit(appointment, service, minutes=30, **extra):
    return {**base._edit(appointment, service, minutes), **extra}


async def _request(db, seeded, service, *, minutes=30, now=NOW, **extra):
    tenant, _patient, appointment, deposit = seeded
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        appointment = await session.get(Appointment, appointment.id)
        deposit = await session.get(PixDeposit, deposit.id)
        result = await service_change.request_paid_change(
            session,
            tenant=tenant,
            appointment=appointment,
            deposit=deposit,
            new_service=service,
            pending_edit=_edit(appointment, service, minutes, **extra),
            now=now,
        )
        await session.commit()
        return result


async def _row(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _adjustments(db) -> list[DepositAdjustment]:
    async with db() as session:
        query = select(DepositAdjustment).order_by(DepositAdjustment.created_at)
        return list((await session.scalars(query)).all())


async def _holds(db) -> list[BookingHold]:
    async with db() as session:
        return list((await session.scalars(select(BookingHold))).all())


async def _notices(db) -> int:
    async with db() as session:
        return await session.scalar(select(func.count()).select_from(ProfessionalEditNotice))


async def _suspended(db, seeded) -> bool:
    async with db() as session:
        return await adjustment_suspension.is_suspended(
            session, tenant_id=seeded[0].id, appointment_id=seeded[2].id
        )


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=NOW.tzinfo)


# --------------------------------------------------------------------------
# characterization: what the owner did NOT change
# --------------------------------------------------------------------------


@pytest.mark.parametrize("timing", ["after", "before"])
async def test_cheaper_and_small_increase_are_todays_in_both_timings(db, provider, timing):
    seeded = await _seed(db, timing=timing)
    cheaper = await _request(db, seeded, "Retorno", minutes=20)
    assert (cheaper.kind, cheaper.diff_cents) == ("asked", 3000)
    [adjustment] = await _adjustments(db)
    assert (adjustment.kind, adjustment.status, adjustment.mode) == (
        KIND_DECREASE, STATUS_ASKED, None,
    )
    assert (await _row(db, seeded[2].id)).appointment_type == "Retorno"
    assert await _holds(db) == []
    assert provider.called("create_pix_charge") == 0


@pytest.mark.parametrize("timing", ["after", "before"])
async def test_a_small_increase_changes_now_without_a_pix_in_both_timings(db, provider, timing):
    seeded = await _seed(db, timing=timing)
    result = await _request(db, seeded, "Consulta plus")
    assert (result.kind, result.diff_cents) == ("applied_now", 900)
    assert await _adjustments(db) == []
    assert await _holds(db) == []


# --------------------------------------------------------------------------
# "after" (D-R5): written now + Pix of the difference
# --------------------------------------------------------------------------


async def test_after_a_dearer_change_is_written_now_and_the_difference_pix_follows(db, provider):
    seeded = await _seed(db, timing="after")
    result = await _request(db, seeded, "Avaliação", minutes=60)

    assert result.kind == "applied_awaiting_payment"
    assert (result.diff_cents, result.pix_copy_paste) == (3000, "000201pix")
    assert result.changed_fields == ("serviço",)
    assert result.notice_id is not None
    assert _utc(result.expires_at) == NOW + timedelta(minutes=30)
    [adjustment] = await _adjustments(db)
    assert (adjustment.kind, adjustment.status, adjustment.mode) == (
        KIND_INCREASE, STATUS_AWAITING_PAYMENT, MODE_APPLIED,
    )
    assert result.adjustment_id == adjustment.id
    [(_name, request)] = [c for c in provider.calls if c[0] == "create_pix_charge"]
    assert (request.amount_cents, request.external_reference) == (3000, str(adjustment.id))
    row = await _row(db, seeded[2].id)
    assert row.appointment_type == "Avaliação"
    assert (row.end_at - row.start_at) == timedelta(minutes=60)
    assert await _notices(db) == 1
    assert await _holds(db) == []
    assert not await _suspended(db, seeded)


async def test_after_a_permanent_refusal_writes_nothing(db, provider):
    from secretaria.services.payments.provider import ProviderError

    provider.create_error = ProviderError("bad", status_code=400, retryable=False)
    seeded = await _seed(db, timing="after")
    result = await _request(db, seeded, "Avaliação", minutes=60)
    assert (result.kind, result.reason) == ("blocked", "charge_failed")
    assert (await _row(db, seeded[2].id)).appointment_type == "Consulta"
    assert await _adjustments(db) == []
    assert await _notices(db) == 0


# --------------------------------------------------------------------------
# "before" (D-R4): suspended + the new window held
# --------------------------------------------------------------------------


async def test_before_a_dearer_change_suspends_and_holds_the_new_window(db, provider):
    seeded = await _seed(db, timing="before")
    appointment = seeded[2]
    new_start = appointment.start_at + timedelta(days=1)
    result = await _request(
        db, seeded, "Avaliação", minutes=60,
        start_at=new_start, end_at=new_start + timedelta(minutes=60), time_changed=True,
    )

    assert result.kind == "suspended"
    assert (result.diff_cents, result.pix_copy_paste) == (3000, "000201pix")
    assert result.changed_fields == ()
    assert _utc(result.expires_at) == NOW + timedelta(minutes=30)
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.mode) == (STATUS_AWAITING_PAYMENT, MODE_SUSPEND)
    # Nothing written on the consultation, nobody told.
    row = await _row(db, appointment.id)
    assert (row.appointment_type, _utc(row.start_at)) == ("Consulta", _utc(appointment.start_at))
    assert await _notices(db) == 0
    assert await _suspended(db, seeded)
    [hold] = await _holds(db)
    assert hold.adjustment_id == adjustment.id and hold.conversation_id is None
    assert (_utc(hold.start_at), _utc(hold.end_at)) == (new_start, new_start + timedelta(hours=1))
    # The hold outlives the Pix by the grace (the sweep always finds it alive).
    assert _utc(hold.expires_at) == NOW + timedelta(minutes=45)
    assert (hold.phase, hold.appointment_type) == ("payment", "Avaliação")


async def test_before_without_room_for_a_useful_pix_falls_back_to_the_legacy_mode(db, provider):
    # The visit in 1h20: the cutoff (visit - 1 h - 5 min) leaves 15 min < 30 min.
    seeded = await _seed(db, timing="before", days_ahead=80 / (24 * 60))
    result = await _request(db, seeded, "Avaliação", minutes=60)

    assert result.kind == "awaiting_payment"
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.mode) == (STATUS_AWAITING_PAYMENT, None)
    assert await _holds(db) == []
    assert not await _suspended(db, seeded)
    assert (await _row(db, seeded[2].id)).appointment_type == "Consulta"


async def test_before_the_pix_never_outlives_the_cutoff(db, provider):
    # The visit in 2 h: cutoff = +55 min; the clinic's TTL is 30 min -> Pix until +30 min; but a
    # TTL of 60 min is capped at the cutoff.
    seeded = await _seed(db, timing="before", days_ahead=2 / 24)
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        tenant.deposit_hold_ttl_minutes = 60
        await session.commit()
    result = await _request(db, seeded, "Avaliação", minutes=60)
    assert result.kind == "suspended"
    assert _utc(result.expires_at) == NOW + timedelta(minutes=55)


async def test_before_a_new_window_taken_by_another_visit_is_slot_taken_and_charges_nothing(
    db, provider
):
    seeded = await _seed(db, timing="before")
    tenant, patient, appointment, _deposit = seeded
    new_start = appointment.start_at + timedelta(days=1)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(), tenant_id=tenant.id, patient_id=patient.id, google_event_id="evt-x",
                appointment_type="Consulta", start_at=new_start,
                end_at=new_start + timedelta(minutes=30), status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    result = await _request(
        db, seeded, "Avaliação", minutes=60,
        start_at=new_start, end_at=new_start + timedelta(minutes=60), time_changed=True,
    )
    assert (result.kind, result.reason) == ("blocked", "slot_taken")
    assert provider.called("create_pix_charge") == 0
    assert await _adjustments(db) == []
    assert await _holds(db) == []
    assert not await _suspended(db, seeded)


async def test_before_a_new_window_held_by_another_pix_is_slot_taken(db, provider):
    seeded = await _seed(db, timing="before")
    tenant, _patient, appointment, _deposit = seeded
    new_start = appointment.start_at + timedelta(days=1)
    async with db() as session:
        session.add(
            BookingHold(
                tenant_id=tenant.id, conversation_id=None, professional_id=None,
                start_at=new_start, end_at=new_start + timedelta(minutes=30),
                expires_at=NOW + timedelta(minutes=20), phase="payment",
            )
        )
        await session.commit()
    result = await _request(
        db, seeded, "Avaliação", minutes=60,
        start_at=new_start, end_at=new_start + timedelta(minutes=60), time_changed=True,
    )
    assert (result.kind, result.reason) == ("blocked", "slot_taken")
    assert provider.called("create_pix_charge") == 0


async def test_an_open_choice_blocks_a_new_change(db, provider):
    seeded = await _seed(db, timing="before")
    first = await _request(db, seeded, "Avaliação", minutes=60)
    async with db() as session:
        row = await session.get(DepositAdjustment, first.adjustment_id)
        row.status = STATUS_AWAITING_CHOICE
        await session.commit()
    second = await _request(db, seeded, "Retorno", minutes=20)
    assert (second.kind, second.reason) == ("blocked", "choice_pending")


# --------------------------------------------------------------------------
# doctor, unit and convênio are priced in (D-R1/D-R3)
# --------------------------------------------------------------------------


async def _doctors(db, seeded):
    """Two doctors of the clinic: Dra. Ana (Consulta R$ 200) owns the visit; Dr. Bruno charges
    R$ 300 for the same Consulta."""
    tenant, _patient, appointment, _deposit = seeded
    ana = Professional(
        id=uuid4(), tenant_id=tenant.id, name="Dra. Ana",
        appointment_types=[{"name": "Consulta", "price": "R$ 200,00", "duration_min": 30}],
    )
    bruno = Professional(
        id=uuid4(), tenant_id=tenant.id, name="Dr. Bruno",
        appointment_types=[{"name": "Consulta", "price": "R$ 300,00", "duration_min": 30}],
    )
    async with db() as session:
        session.add_all([ana, bruno])
        await session.flush()
        row = await session.get(Appointment, appointment.id)
        row.professional_id = ana.id
        await session.commit()
    return ana.id, bruno.id


async def test_a_doctor_change_is_priced_at_the_new_doctor_and_named(db, provider):
    seeded = await _seed(db, timing="after")
    ana, bruno = await _doctors(db, seeded)
    result = await _request(
        db, seeded, "Consulta", professional_id=bruno, doctor_changed=True,
        old_professional_id=ana,
    )
    assert (result.kind, result.diff_cents) == ("applied_awaiting_payment", 3000)
    assert result.label == "Consulta com Dr. Bruno"
    [adjustment] = await _adjustments(db)
    assert (adjustment.old_service, adjustment.new_service) == (
        "Consulta com Dra. Ana", "Consulta com Dr. Bruno",
    )
    assert (await _row(db, seeded[2].id)).professional_id == bruno


async def test_before_a_doctor_change_is_checked_on_the_new_doctors_agenda(db, provider):
    seeded = await _seed(db, timing="before")
    ana, bruno = await _doctors(db, seeded)
    tenant, patient, appointment, _deposit = seeded
    # Dr. Bruno is busy at the SAME time the visit is (with Dra. Ana) today.
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(), tenant_id=tenant.id, patient_id=patient.id, google_event_id="evt-b",
                appointment_type="Consulta", professional_id=bruno, start_at=appointment.start_at,
                end_at=appointment.end_at, status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    result = await _request(
        db, seeded, "Consulta", professional_id=bruno, doctor_changed=True,
        old_professional_id=ana,
    )
    assert (result.kind, result.reason) == ("blocked", "slot_taken")
    assert provider.called("create_pix_charge") == 0


async def test_a_legacy_no_sinal_convenio_no_longer_moves_the_sinal(db, provider):
    """TASK-085 R D-R12 revoked D-R3's "convênio sem sinal = sinal novo 0": switching to a
    convênio saved as "não cobra sinal" is just a change of convênio - same price, same sinal."""
    seeded = await _seed(db, timing="before")
    async with db() as session:
        plan = TenantInsurancePlan(id=uuid4(), tenant_id=seeded[0].id, custom_name="Unimed")
        session.add(plan)
        await session.flush()
        await set_legacy_flag(session, "tenant_insurance_plans", plan.id, False)
        await session.commit()
    result = await _request(db, seeded, "Consulta", insurance="Unimed")
    assert (result.kind, result.decision, result.diff_cents) == ("applied_now", "no_change", 0)
    assert result.label == "Consulta pelo Unimed"
    assert provider.called("create_pix_charge") == 0
    row = await _row(db, seeded[2].id)
    assert row.insurance == "Unimed" and row.insurance_plan_id is not None


async def test_preview_takes_the_convenio_and_the_doctor(db, provider):
    seeded = await _seed(db, timing="after")
    ana, bruno = await _doctors(db, seeded)
    async with db() as session:
        plan = TenantInsurancePlan(id=uuid4(), tenant_id=seeded[0].id, custom_name="Unimed")
        session.add(plan)
        await session.flush()
        await set_legacy_flag(session, "tenant_insurance_plans", plan.id, False)
        await session.commit()
        tenant = await session.get(Tenant, seeded[0].id)
        appointment = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        common = {
            "tenant": tenant, "appointment": appointment, "deposit": deposit,
            "new_service": "Consulta", "start_at": appointment.start_at, "now": NOW,
        }
        same = await service_change.preview_service_change(
            session, professional_id=ana, **common
        )
        dearer = await service_change.preview_service_change(
            session, professional_id=bruno, **common
        )
        legacy_no_sinal = await service_change.preview_service_change(
            session, professional_id=ana, insurance="Unimed", **common
        )
        particular = await service_change.preview_service_change(
            session, professional_id=ana, insurance=None, **common
        )
    assert same.action == "no_change"
    assert (dearer.action, dearer.diff_cents) == ("increase_charge", 3000)
    # D-R12: the convênio's legacy "no sinal" flag is ignored.
    assert legacy_no_sinal.action == "no_change"
    assert particular.action == "no_change"


async def test_a_unit_change_at_the_same_price_is_no_change_through_the_engine(db, provider):
    from secretaria.models import Unit

    seeded = await _seed(db, timing="before")
    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=seeded[0].id, name="Leste")
        session.add(unit)
        await session.commit()
    result = await _request(db, seeded, "Consulta", unit_id=unit.id)
    assert (result.kind, result.decision) == ("applied_now", "no_change")
    assert result.label == "Consulta na Unidade Leste"
    assert (await _row(db, seeded[2].id)).unit_id == unit.id


# --------------------------------------------------------------------------
# texts (contrato.md §5)
# --------------------------------------------------------------------------


def test_the_card_line_depends_on_the_timing():
    before = service_change.confirm_card_line_text("increase_charge", 3000, timing="before")
    after = service_change.confirm_card_line_text("increase_charge", 3000, timing="after")
    assert "R$ 30,00" in before and "R$ 30,00" in after
    # UA2 (D-R10): the current consultation LEAVES the agenda; only the new time is reserved.
    assert "sai da agenda" in before and "reservad" in before and "guardad" not in before
    assert "só muda depois do pagamento" in before
    assert "na hora" in after and "só muda" not in after
    assert service_change.confirm_card_line_text("increase_charge", 3000) == after
    unit = service_change.confirm_card_line_text(
        "increase_charge", 3000, unit_only=True, timing="before"
    )
    assert "a unidade só muda" in unit


def test_the_suspension_pix_says_the_old_visit_leaves_the_agenda():
    """UA2 (D-R10, mapa-u §4.6): the old consultation leaves the agenda; only the new time is
    reserved - until the Pix's deadline in the clinic's zone when given."""
    text = service_change.difference_pix_text(
        "Avaliação", 3000, "000201pix", 30, held_new_slot=True
    )
    assert "R$ 30,00" in text and "só muda" in text
    assert "sai da agenda" in text and "fica reservado até o fim do prazo do Pix" in text
    assert "reservados" not in text  # never "os dois horários reservados"
    for option in ("voltar para a consulta antiga", "outro horário", "reenviar o Pix", "cancelar"):
        assert option in text
    assert "continua como está" not in text  # unpaid, the patient is ASKED (D-R4)
    timed = service_change.difference_pix_text(
        "Avaliação", 3000, "000201pix", 30, held_new_slot=True,
        expires_at=datetime(2026, 10, 26, 17, 30, tzinfo=UTC), tz="America/Sao_Paulo",
    )
    assert "fica reservado até 14:30." in timed
    assert text.rstrip().endswith("```000201pix```")
    # Fix round 1 (M5): the default never promises a reserved slot (the legacy fallback).
    legacy = service_change.difference_pix_text("Avaliação", 3000, "000201pix", 30)
    assert "reservad" not in legacy and "só muda" in legacy
    assert "continua como está" in legacy


def test_the_applied_pix_says_the_change_already_happened():
    text = service_change.difference_pix_applied_text("Avaliação", 3000, "000201pix", 30)
    assert "já foi alterada" in text and "Avaliação" in text and "R$ 30,00" in text
    assert text.rstrip().endswith("```000201pix```")
    hub = service_change.difference_pix_applied_text(
        "Avaliação", 3000, "000201pix", 30, source="hub"
    )
    assert "clínica" in hub


def test_the_choice_message_spells_the_three_options_out():
    text = service_change.choice_message_text("Consulta", "Avaliação", 3000)
    assert "R$ 30,00" in text and "Consulta" in text and "Avaliação" in text
    for label in ("Manter a antiga", "Reenviar o Pix", "Cancelar ambas"):
        assert label in text
    # UA2 (D-R8/D-R11, mapa-u §4.6): no answer -> we TRY to bring the old one back.
    assert "não responder a tempo, tentamos voltar para a consulta antiga" in text
    taken = service_change.choice_message_text("Consulta", "Avaliação", 3000, old_slot_free=False)
    assert "Manter a antiga" not in taken and "Outro horário" in taken
    assert "O sinal pago continua valendo" in taken
    assert "a clínica vai falar com você para remarcar" in taken
    no_resend = service_change.choice_message_text("Consulta", "Avaliação", 3000, resend=False)
    assert "Reenviar o Pix" not in no_resend


@pytest.mark.parametrize(
    "kind",
    ["kept_old", "resent", "cancel_old", "no_window", "resend_failed", "already_decided",
     "not_found", "rebook", "slot_taken", "restore_failed"],
)
def test_every_choice_outcome_has_one_answer(kind):
    text = service_change.choice_answer_text(
        ChoiceOutcome(kind=kind, diff_cents=3000, old_service="Consulta", new_service="Avaliação")
    )
    assert text and text.strip() == text


def test_kept_collect_and_blocked_texts():
    kept = service_change.kept_old_notice_text("Consulta")
    assert kept == "Pronto! Sua consulta antiga voltou para a agenda, como era: Consulta."
    collect = service_change.unpaid_collect_text("Avaliação", 3000)
    assert "R$ 30,00" in collect and "Avaliação" in collect
    assert "Pix" in service_change.blocked_text("choice_pending")
    assert "horário" in service_change.blocked_text("slot_taken")
    assert service_change.blocked_text("slot_taken", unit_only=True) == service_change.blocked_text(
        "slot_taken"
    )


async def test_after_with_the_provider_down_applies_now_and_the_clinic_collects(db, provider):
    """Fix round 1: the provider down in an "after" clinic is today's `increase_collect` - the
    change stands, no Pix, ONE `service_diff_collect_due` item; no `mode` row is born."""
    from secretaria.models import PaymentAttentionItem
    from secretaria.models.deposit_adjustment import STATUS_COLLECT_DUE
    from secretaria.models.payment_attention_item import REASON_SERVICE_DIFF_COLLECT_DUE
    from secretaria.services.payments.provider import ProviderError

    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    seeded = await _seed(db, timing="after")
    result = await _request(db, seeded, "Avaliação", minutes=60)

    assert (result.kind, result.decision) == ("applied_now", service_change.DECISION_COLLECT)
    assert result.pix_copy_paste is None
    [adjustment] = await _adjustments(db)
    assert (adjustment.status, adjustment.mode) == (STATUS_COLLECT_DUE, None)
    assert (await _row(db, seeded[2].id)).appointment_type == "Avaliação"
    async with db() as session:
        [item] = (await session.scalars(select(PaymentAttentionItem))).all()
    assert (item.reason, item.amount_cents) == (REASON_SERVICE_DIFF_COLLECT_DUE, 3000)
    assert await _holds(db) == []
