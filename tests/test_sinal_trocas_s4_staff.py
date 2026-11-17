"""TASK-085 R / S4 - the clinic's Editar/Remarcar and the sinal (`staff_appointment_edit`).

Contract: tasks/TASK-085/sinal-trocas/contrato.md §3.7 (D-R6 caller obligations), §3.9 (the
clinic's side). The world of tests/test_staff_appointment_edit.py: a PAID sinal of R$ 100,00 at
30 % on "Consulta" (R$ 200,00); "Avaliação" R$ 500,00 (sinal R$ 150,00: R$ 50,00 dearer, 50 min);
"Retorno" R$ 100,00 (cheaper). What is pinned here:

* characterization: a clinic without a sinal, and an edit that touches no money field, are today's;
* "before" + dearer (D-R4): only the contact phone is written now; the visit is suspended, the
  new window held, the agenda untouched (its window checked free first); a window that is not
  free is refused before any Pix; the provider down = the change stands now and the agenda
  follows; the legacy fallback (no room for a useful Pix) refuses what it cannot carry;
* a SUSPENDED visit cannot be edited (D-R9);
* "after" + doctor / convênio (D-R3): priced through the engine;
* an UNPAID sinal (D-R6): `requote_awaiting` is the last step - voided = a new Pix after the commit,
  a Pix being paid or that cannot be voided undoes everything; a consultation without a sinal
  may owe one (OQ-10) - never a booking the clinic made itself.
"""

# ruff: noqa: F811

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.models import Appointment, BookingHold, PixDeposit, Tenant
from secretaria.models.insurance import TenantInsurancePlan
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import staff_appointment_edit as sae
from secretaria.services.payments import adjustment_suspension, deposit_lifecycle, service_change
from secretaria.services.payments.provider import ChargeState, PaymentStatus, ProviderError
from secretaria.services.staff_appointment_edit import StaffEditError
from tests._r7_support import FakeCalendar, add_professional, set_appointment, set_tenant
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment
from tests.test_staff_appointment_edit import (  # noqa: F401 - autouse + fixtures
    _adjustments,
    _calendars,
    _edit,
    _notices,
    _paid_world,
    _req,
    _utc,
    _world,
    provider,
)


async def _before(db, world) -> None:
    await set_tenant(db, world.tenant.id, deposit_timing="before")


async def _holds(db) -> list[BookingHold]:
    async with db() as session:
        return list(await session.scalars(select(BookingHold)))


async def _suspended(db, world) -> bool:
    async with db() as session:
        return await adjustment_suspension.is_suspended(
            session, tenant_id=world.tenant.id, appointment_id=world.appointment.id
        )


async def _deposit(db) -> PixDeposit:
    async with db() as session:
        return await session.scalar(select(PixDeposit))


def _spy(monkeypatch) -> list[dict]:
    calls: list[dict] = []
    real = service_change.request_paid_change

    async def _recording(session, **kwargs):
        calls.append(kwargs)
        return await real(session, **kwargs)

    monkeypatch.setattr(service_change, "request_paid_change", _recording)
    return calls


# --------------------------------------------------------------------------
# characterization
# --------------------------------------------------------------------------


async def test_a_clinic_without_a_sinal_edits_exactly_as_today(db, monkeypatch):
    world = await _world(db)
    calls = _spy(monkeypatch)

    outcome = await _edit(db, world, _req(service="Retorno"))

    assert calls == [] and outcome.service_change is None and outcome.sinal_recharge is None
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Retorno"
    assert await _adjustments(db) == []


async def test_a_paid_sinal_and_no_money_field_never_asks_the_engine(db, provider, monkeypatch):
    world = await _paid_world(db)
    await _before(db, world)
    calls = _spy(monkeypatch)
    new = world.start_at + timedelta(days=1)

    outcome = await _edit(db, world, _req(start_at=new, attendee_name="João"))

    assert calls == [] and outcome.service_change is None
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), row.attendee_name) == (new, "João")
    assert provider.calls == [] and await _holds(db) == []


# --------------------------------------------------------------------------
# "before" + dearer: D-R4 (suspended)
# --------------------------------------------------------------------------


async def test_before_dearer_service_writes_only_the_phone_and_suspends_the_visit(
    db, provider
):
    world = await _paid_world(db)
    await _before(db, world)
    new = world.start_at + timedelta(days=1)

    outcome = await _edit(
        db, world, _req(service="Avaliação", start_at=new, phone="5511999990000")
    )

    change = outcome.service_change
    assert (change.status, change.diff_cents) == ("suspended", 5000)
    assert change.pix_copy_paste == "000201pix" and change.expires_at is not None
    assert [c.label for c in outcome.changes] == ["Telefone de contato"]
    assert outcome.time_changed is False and outcome.notice_id is None
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), row.appointment_type, row.phone) == (
        world.start_at,
        "Consulta",
        "5511999990000",
    )
    calendar = FakeCalendar.get("tenant")
    assert calendar.updated == [] and calendar.created == [] and calendar.cancelled == []
    assert calendar.checked == [(new, new + timedelta(minutes=50), "evt-1")]
    [adjustment] = await _adjustments(db)
    assert (adjustment.mode, adjustment.status) == ("suspend", "awaiting_payment")
    pending = adjustment.pending_edit
    assert pending["appointment_type"] == "Avaliação" and pending["source"] == "hub"
    assert pending["start_at"] == new.isoformat() and pending["time_changed"] is True
    [hold] = await _holds(db)
    assert (hold.adjustment_id, hold.conversation_id) == (adjustment.id, None)
    assert (_utc(hold.start_at), _utc(hold.end_at)) == (new, new + timedelta(minutes=50))
    assert await _suspended(db, world)
    assert await _notices(db, world.appointment.id) == []


async def test_before_dearer_change_to_another_doctor_checks_his_agenda_and_moves_nothing(
    db, provider
):
    world = await _paid_world(db, professional_name="Dra. Ana")
    await _before(db, world)
    old_pro = world.appointment.professional_id
    bruno = await add_professional(db, world.tenant.id, "Dr. Bruno")

    outcome = await _edit(db, world, _req(service="Avaliação", professional_id=bruno))

    assert outcome.service_change.status == "suspended"
    assert outcome.service_change.new_service == "Avaliação com Dr. Bruno"
    assert outcome.changes == ()
    new_calendar = FakeCalendar.get(str(bruno))
    assert new_calendar.checked == [
        (world.start_at, world.start_at + timedelta(minutes=50), None)
    ]
    assert new_calendar.created == [] and FakeCalendar.get(str(old_pro)).cancelled == []
    row = await reload_appointment(db, world.appointment.id)
    assert row.professional_id == old_pro and row.appointment_type == "Consulta"
    [adjustment] = await _adjustments(db)
    assert adjustment.pending_edit["professional_id"] == str(bruno)
    assert adjustment.pending_edit["doctor_changed"] is True
    [hold] = await _holds(db)
    assert hold.professional_id == bruno


async def test_before_dearer_change_whose_window_is_busy_is_refused_before_any_pix(
    db, provider
):
    world = await _paid_world(db)
    await _before(db, world)
    FakeCalendar.get("tenant").free = False

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Avaliação"))

    assert (err.value.status_code, err.value.code) == (409, "slot_unavailable")
    assert provider.called("create_pix_charge") == 0
    assert await _adjustments(db) == [] and await _holds(db) == []
    assert not await _suspended(db, world)


async def test_before_dearer_change_with_the_provider_down_stands_now_and_moves_the_agenda(
    db, provider
):
    """Contrato §1/OQ-7: the provider down never blocks - the change stands now (the clinic
    collects) - and the agenda, untouched until the engine answered, follows inside the same
    transaction (no Pix exists, so a refusal could still undo everything)."""
    provider.create_error = ProviderError("down", status_code=503, retryable=True)
    world = await _paid_world(db)
    await _before(db, world)
    new = world.start_at + timedelta(days=1)

    outcome = await _edit(db, world, _req(service="Avaliação", start_at=new))

    change = outcome.service_change
    assert (change.status, change.decision) == ("applied_now", service_change.DECISION_COLLECT)
    assert [c.label for c in outcome.changes] == ["Data", "Serviço"]
    assert outcome.time_changed is True
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), row.appointment_type) == (new, "Avaliação")
    [moved] = FakeCalendar.get("tenant").updated
    assert moved[:4] == ("evt-1", new, new + timedelta(minutes=50), "Avaliação - Maria")
    assert await _holds(db) == [] and not await _suspended(db, world)


async def test_before_without_room_for_a_useful_pix_waits_the_legacy_way(
    db, provider, monkeypatch
):
    """Contrato §1: no room for a useful Pix before the visits -> the legacy deferred mode (no
    hold, no suspension); the staff writes nothing of the change now."""
    monkeypatch.setattr(service_change, "suspend_pix_expiry", lambda *args, **kwargs: None)
    world = await _paid_world(db)
    await _before(db, world)

    outcome = await _edit(db, world, _req(service="Avaliação"))

    assert outcome.service_change.status == "awaiting_payment"
    assert outcome.changes == ()
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    [adjustment] = await _adjustments(db)
    assert adjustment.mode is None
    assert await _holds(db) == [] and not await _suspended(db, world)
    assert FakeCalendar.get("tenant").updated == []


async def test_before_without_room_refuses_a_doctor_change_it_could_not_carry(
    db, provider, monkeypatch
):
    monkeypatch.setattr(service_change, "suspend_pix_expiry", lambda *args, **kwargs: None)
    world = await _paid_world(db, professional_name="Dra. Ana")
    await _before(db, world)
    bruno = await add_professional(db, world.tenant.id, "Dr. Bruno")

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Avaliação", professional_id=bruno))

    assert (err.value.status_code, err.value.code) == (409, "service_change_blocked")
    assert err.value.extra["reason"] == "no_room"
    assert "Nada foi alterado" in err.value.message
    assert provider.calls == [] and await _adjustments(db) == []
    assert FakeCalendar.get("tenant").updated == []


async def test_before_cheaper_change_is_written_now_as_today(db, provider):
    world = await _paid_world(db)
    await _before(db, world)

    outcome = await _edit(db, world, _req(service="Retorno"))

    assert outcome.service_change.status == "asked"
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Retorno"
    assert FakeCalendar.get("tenant").updated[-1][3] == "Retorno - Maria"
    assert await _holds(db) == []


# --------------------------------------------------------------------------
# D-R9: a suspended visit
# --------------------------------------------------------------------------


async def test_a_suspended_visit_cannot_be_edited_at_all(db, provider):
    world = await _paid_world(db)
    await _before(db, world)
    await _edit(db, world, _req(service="Avaliação"))
    world.appointment = await reload_appointment(db, world.appointment.id)
    calls_before = list(provider.calls)

    for request in (
        _req(phone="5511999990000"),
        _req(start_at=world.start_at + timedelta(days=2)),
        _req(service="Retorno"),
    ):
        with pytest.raises(StaffEditError) as err:
            await _edit(db, world, request)
        assert (err.value.status_code, err.value.code) == (409, "appointment_suspended")
        assert "só é possível liberar o horário" in err.value.message

    assert provider.calls == calls_before
    row = await reload_appointment(db, world.appointment.id)
    assert (row.phone, row.appointment_type) == (world.appointment.phone, "Consulta")
    assert FakeCalendar.get("tenant").updated == []


async def test_a_visit_suspended_while_the_clinic_edits_wins_under_the_row_lock(db, provider):
    """The patient's change suspended the visit between the clinic's read and its write."""
    world = await _paid_world(db)
    other = await _paid_world(db, phone_number_id="pnid-2")  # a second clinic, untouched

    async def suspend_meanwhile():
        from secretaria.models.deposit_adjustment import DepositAdjustment

        async with db() as session:
            deposit = await session.scalar(
                select(PixDeposit).where(PixDeposit.appointment_id == world.appointment.id)
            )
            session.add(
                DepositAdjustment(
                    id=uuid4(),
                    tenant_id=world.tenant.id,
                    deposit_id=deposit.id,
                    appointment_id=world.appointment.id,
                    kind="increase",
                    mode="suspend",
                    status="awaiting_payment",
                    old_service="Consulta",
                    new_service="Avaliação",
                    diff_cents=5000,
                )
            )
            await session.commit()

    FakeCalendar.get("tenant").during = suspend_meanwhile

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(start_at=world.start_at + timedelta(hours=2)))

    assert err.value.code == "appointment_suspended"
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == world.start_at
    assert FakeCalendar.get("tenant").updated[-1][1] == world.start_at  # Google put back
    assert not await _suspended(db, other)


# --------------------------------------------------------------------------
# "after": D-R3 doctor / convênio through the engine
# --------------------------------------------------------------------------


async def test_after_convenio_without_sinal_is_the_whole_sinal_cheaper(db, provider):
    world = await _paid_world(db)
    async with db() as session:
        session.add(
            TenantInsurancePlan(
                id=uuid4(), tenant_id=world.tenant.id, custom_name="Unimed", charge_deposit=False
            )
        )
        await session.commit()

    outcome = await _edit(db, world, _req(insurance="Unimed"))

    change = outcome.service_change
    assert (change.status, change.diff_cents) == ("asked", 10000)
    assert change.new_service == "Consulta pelo Unimed"
    row = await reload_appointment(db, world.appointment.id)
    assert row.insurance == "Unimed" and row.insurance_plan_id is not None
    assert [c.label for c in outcome.changes] == ["Convênio"]


async def test_after_dearer_service_with_a_new_doctor_moves_to_his_agenda_now(db, provider):
    world = await _paid_world(db, professional_name="Dra. Ana")
    old_pro = world.appointment.professional_id
    bruno = await add_professional(db, world.tenant.id, "Dr. Bruno")

    outcome = await _edit(db, world, _req(service="Avaliação", professional_id=bruno))

    assert outcome.service_change.status == "applied_awaiting_payment"
    assert len(FakeCalendar.get(str(bruno)).created) == 1
    assert FakeCalendar.get(str(old_pro)).cancelled == ["evt-1"]
    row = await reload_appointment(db, world.appointment.id)
    assert (row.professional_id, row.appointment_type) == (bruno, "Avaliação")
    assert row.google_event_id == f"evt-new-{bruno}"
    assert row.google_calendar_source == "professional"
    [notice] = await _notices(db, world.appointment.id)
    assert {"serviço", "médico"} <= set(notice.changed_fields)


async def test_after_a_blocked_change_puts_the_new_doctors_event_back(db, provider):
    world = await _paid_world(db, professional_name="Dra. Ana")
    old_pro = world.appointment.professional_id
    bruno = await add_professional(db, world.tenant.id, "Dr. Bruno")

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Sem preço", professional_id=bruno))

    assert err.value.extra["reason"] == "price_unavailable"
    assert FakeCalendar.get(str(bruno)).cancelled == [f"evt-new-{bruno}"]
    assert FakeCalendar.get(str(old_pro)).cancelled == []
    row = await reload_appointment(db, world.appointment.id)
    assert (row.professional_id, row.google_event_id) == (old_pro, "evt-1")


# --------------------------------------------------------------------------
# D-R6: an UNPAID sinal
# --------------------------------------------------------------------------


async def _awaiting_world(db, **kwargs):
    """A clinic "after" with an AWAITING sinal of R$ 60,00 (30 % of Consulta R$ 200,00)."""
    world = await _paid_world(db, **kwargs)
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
        deposit.status = PixDepositStatus.AWAITING
        deposit.amount_cents = 6000
        deposit.provider = "asaas"
        deposit.provider_charge_id = "pay_old"
        deposit.deposit_timing = "after"
        await session.commit()
    return world


async def test_unpaid_sinal_voided_last_and_a_new_one_after_the_commit(db, provider):
    world = await _awaiting_world(db)

    outcome = await _edit(db, world, _req(service="Avaliação"))

    assert outcome.service_change is None
    assert outcome.sinal_recharge == sae.SinalRecharge(old_amount_cents=6000)
    assert provider.called("cancel_charge") == 1 and provider.called("create_pix_charge") == 0
    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Avaliação"
    deposit = await _deposit(db)
    assert (deposit.status, deposit.appointment_id) == (PixDepositStatus.EXPIRED, None)


async def test_unpaid_sinal_same_amount_and_time_only_edits_never_touch_the_provider(
    db, provider
):
    world = await _awaiting_world(db)

    outcome = await _edit(db, world, _req(start_at=world.start_at + timedelta(days=1)))
    assert outcome.sinal_recharge is None

    world.appointment = await reload_appointment(db, world.appointment.id)
    outcome = await _edit(db, world, _req(attendee_name="João"))
    assert outcome.sinal_recharge is None
    assert provider.calls == []
    assert (await _deposit(db)).status == PixDepositStatus.AWAITING


async def test_unpaid_sinal_being_paid_undoes_the_whole_edit(db, provider):
    provider.state = ChargeState(status=PaymentStatus.PAID)
    world = await _awaiting_world(db)
    new = world.start_at + timedelta(hours=3)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Avaliação", start_at=new))

    assert (err.value.status_code, err.value.code) == (409, "sinal_requote_blocked")
    assert err.value.extra["reason"] == "payment_in_flight"
    assert "Nada foi alterado" in err.value.message
    row = await reload_appointment(db, world.appointment.id)
    assert (_utc(row.start_at), row.appointment_type) == (world.start_at, "Consulta")
    moved, restored = FakeCalendar.get("tenant").updated  # Google put back
    assert moved[1] == new and restored[1] == world.start_at
    assert (await _deposit(db)).status == PixDepositStatus.AWAITING
    assert await _notices(db, world.appointment.id) == []


async def test_unpaid_sinal_that_cannot_be_voided_undoes_the_edit(db, provider):
    provider.cancel_error = ProviderError("down", status_code=503, retryable=True)
    world = await _awaiting_world(db)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Avaliação"))

    assert (err.value.status_code, err.value.extra["reason"]) == (502, "void_failed")
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"
    assert (await _deposit(db)).status == PixDepositStatus.AWAITING


async def test_an_exception_in_the_requote_counts_as_void_failed(db, provider, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("price lookup")

    monkeypatch.setattr(deposit_lifecycle, "requote_awaiting", boom)
    world = await _awaiting_world(db)

    with pytest.raises(StaffEditError) as err:
        await _edit(db, world, _req(service="Avaliação"))

    assert (err.value.status_code, err.value.extra["reason"]) == (502, "void_failed")
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"


# --------------------------------------------------------------------------
# OQ-10: no sinal yet
# --------------------------------------------------------------------------


async def _no_sinal_world(db, **kwargs):
    world = await _paid_world(db, **kwargs)
    async with db() as session:
        await session.delete(await session.scalar(select(PixDeposit)))
        await session.commit()
    return world


async def test_a_patient_booking_without_a_sinal_may_owe_one_after_a_money_edit(db, provider):
    world = await _no_sinal_world(db)

    outcome = await _edit(db, world, _req(service="Avaliação"))

    assert outcome.sinal_recharge == sae.SinalRecharge(old_amount_cents=None)
    assert outcome.service_change is None and provider.calls == []


async def test_no_new_sinal_for_the_clinics_own_booking_or_a_waived_one_or_a_time_move(
    db, provider
):
    world = await _no_sinal_world(db)
    outcome = await _edit(db, world, _req(start_at=world.start_at + timedelta(days=1)))
    assert outcome.sinal_recharge is None  # no money field

    world.appointment = await reload_appointment(db, world.appointment.id)
    await set_appointment(db, world.appointment.id, deposit_waived_reason="provider_down")
    world.appointment = await reload_appointment(db, world.appointment.id)
    assert (await _edit(db, world, _req(service="Retorno"))).sinal_recharge is None

    await set_appointment(
        db, world.appointment.id, deposit_waived_reason=None, conversation_id=None
    )
    world.appointment = await reload_appointment(db, world.appointment.id)
    assert (await _edit(db, world, _req(service="Avaliação"))).sinal_recharge is None


async def test_a_clinic_with_the_sinal_off_never_asks_for_a_new_one(db, provider):
    world = await _no_sinal_world(db)
    await set_tenant(db, world.tenant.id, pix_deposit_enabled=False)

    outcome = await _edit(db, world, _req(service="Avaliação"))

    assert outcome.sinal_recharge is None


async def test_the_staff_edit_stays_inside_its_clinic(db, provider):
    """Isolation: another clinic's paid sinal is never read for this clinic's edit."""
    world = await _world(db)  # no sinal here
    other = await _paid_world(db, phone_number_id="pnid-2")

    outcome = await _edit(db, world, _req(service="Retorno"))

    assert outcome.service_change is None
    async with db() as session:
        deposit = await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == other.appointment.id)
        )
        other_row = await session.get(Appointment, other.appointment.id)
        tenant = await session.get(Tenant, other.tenant.id)
    assert deposit.status == PixDepositStatus.PAID and other_row.appointment_type == "Consulta"
    assert tenant.id != world.tenant.id and await _adjustments(db) == []


def test_the_suspended_answer_is_the_contracts():
    err = sae.suspended_error()
    assert (err.status_code, err.code) == (409, "appointment_suspended")
    assert err.message == (
        "Esta consulta aguarda o Pix da diferença de uma troca pedida pelo paciente. "
        "Enquanto isso, só é possível liberar o horário."
    )


