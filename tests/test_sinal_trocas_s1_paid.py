"""TASK-085 R / S1 - the difference Pix PAID: the full edit, the hold, the agenda.

Contract: tasks/TASK-085/sinal-trocas/contrato.md §2.2 / §3.6. Pinned here:

* `suspend` paid -> `applied`, the WHOLE pending edit written in the webhook's transaction
  (service, doctor, convênio + plans, unit, time), the hold released, its own hold never refusing
  it; a window taken meanwhile -> `cancelled` + ORPHAN, hold released, nothing written;
* `applied` paid -> only the status (the edit was written at the request), a thank-you notice,
  no agenda work;
* after the commit, a change of DOCTOR moves the event to the new doctor's agenda (created there,
  deleted on the old one); a failure opens `service_diff_agenda_pending`;
* the old visit was hidden while suspended: its reminders are re-planned once it is applied.
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
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    STATUS_APPLIED,
    STATUS_CANCELLED,
    STATUS_KEPT_OLD,
    DepositAdjustment,
)
from secretaria.models.insurance import TenantInsurancePlan
from secretaria.models.payment_attention_item import (
    REASON_ORPHAN_PAYMENT,
    REASON_SERVICE_DIFF_AGENDA_PENDING,
)
from secretaria.services import booking_hold
from secretaria.services.payments import adjustment_suspension, notify, service_change
from secretaria.services.payments.provider import ChargeState, PaymentStatus
from tests import test_service_change as base
from tests.test_sinal_trocas_s1_engine import _doctors, _seed
from tests.test_sinal_trocas_s1_unpaid import CLOCK, _webhook

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks


@pytest.fixture(autouse=True)
def hold_clock(monkeypatch):
    CLOCK["now"] = NOW
    monkeypatch.setattr(booking_hold, "_now", lambda: CLOCK["now"])


@pytest.fixture
def sent(monkeypatch):
    out: list = []

    async def fake_deliver(session, tenant, patient, messages, **kw):
        out.append(list(messages))

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


async def _request(db, seeded, service, *, minutes=60, **extra):
    async with db() as session:
        tenant = await session.get(Tenant, seeded[0].id)
        row = await session.get(Appointment, seeded[2].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        result = await service_change.request_paid_change(
            session, tenant=tenant, appointment=row, deposit=deposit, new_service=service,
            pending_edit={**base._edit(row, service, minutes), **extra}, now=NOW,
        )
        await session.commit()
    return result


def _paid(provider, adjustment_id, amount=3000):
    provider.state = ChargeState(
        status=PaymentStatus.PAID, paid_amount_cents=amount,
        external_reference=str(adjustment_id), paid_at=NOW + timedelta(minutes=5),
    )


async def _pay(db, seeded, adjustment_id):
    _paid_at = NOW + timedelta(minutes=5)
    return await _webhook(db, seeded[0].id, PaymentStatus.PAID, at=_paid_at)


async def _row(db, appointment_id) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, appointment_id)


async def _all(db, model):
    async with db() as session:
        return list((await session.scalars(select(model))).all())


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=NOW.tzinfo)


async def test_a_paid_suspended_change_writes_the_whole_edit_and_frees_the_hold(
    db, provider, sent, replans, monkeypatch
):
    monkeypatch.setattr(service_change, "ADJUSTMENT_APPLIED_HOOKS", [])
    seeded = await _seed(db, timing="before")
    ana, bruno = await _doctors(db, seeded)
    from secretaria.models import Unit

    async with db() as session:
        unit = Unit(id=uuid4(), tenant_id=seeded[0].id, name="Leste")
        session.add_all(
            [unit, TenantInsurancePlan(id=uuid4(), tenant_id=seeded[0].id, custom_name="Amil")]
        )
        await session.commit()
    new_start = seeded[2].start_at + timedelta(days=1)
    created = await _request(
        db, seeded, "Consulta", minutes=30, professional_id=bruno, old_professional_id=ana,
        doctor_changed=True, insurance="Amil", unit_id=unit.id, start_at=new_start,
        end_at=new_start + timedelta(minutes=30), time_changed=True,
    )
    assert created.kind == "suspended"
    _paid(provider, created.adjustment_id)

    result = await _pay(db, seeded, created.adjustment_id)

    assert result.outcome == "adjustment_applied"
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
        assert adjustment.status == STATUS_APPLIED
        assert not await adjustment_suspension.is_suspended(
            session, tenant_id=seeded[0].id, appointment_id=seeded[2].id
        )
    row = await _row(db, seeded[2].id)
    assert (row.professional_id, row.insurance, row.unit_id) == (bruno, "Amil", unit.id)
    assert row.insurance_plan_id is not None
    assert _utc(row.start_at) == new_start
    assert row.status == AppointmentStatus.RESCHEDULED
    async with db() as session:  # the move counts now, on the sinal
        assert (await session.get(PixDeposit, seeded[3].id)).reschedule_count == 1
    assert await _all(db, BookingHold) == []


async def test_a_paid_suspended_change_whose_window_was_taken_is_an_orphan(db, provider, sent):
    seeded = await _seed(db, timing="before")
    new_start = seeded[2].start_at + timedelta(days=1)
    created = await _request(
        db, seeded, "Avaliação", start_at=new_start, end_at=new_start + timedelta(minutes=60),
        time_changed=True,
    )
    # Somebody booked the window anyway (e.g. the clinic on Google, mirrored as a row).
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(), tenant_id=seeded[0].id, patient_id=seeded[1].id,
                google_event_id="evt-q", appointment_type="Consulta", start_at=new_start,
                end_at=new_start + timedelta(minutes=30), status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()
    _paid(provider, created.adjustment_id)

    result = await _pay(db, seeded, created.adjustment_id)

    assert result.outcome == "adjustment_not_applicable"
    # UA2 (mapa-u §7 item 9): the change goes to `restoring` and, after the commit, the old visit
    # is brought back (its event never left in this test: only the transition) -> `kept_old`.
    assert result.restore_adjustment_id == created.adjustment_id
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
    assert adjustment.status == STATUS_KEPT_OLD
    assert sent[-1][0].body == service_change.kept_old_notice_text("Consulta")
    [item] = await _all(db, PaymentAttentionItem)
    assert item.reason == REASON_ORPHAN_PAYMENT
    assert (await _row(db, seeded[2].id)).appointment_type == "Consulta"
    assert await _all(db, BookingHold) == []


async def test_a_paid_applied_change_only_records_the_payment(
    db, provider, sent, replans, monkeypatch
):
    monkeypatch.setattr(
        service_change,
        "ADJUSTMENT_APPLIED_HOOKS",
        [
            service_change.update_agenda_after_payment,
            service_change.notify_patient_adjustment_applied,
        ],
    )
    seeded = await _seed(db, timing="after")
    created = await _request(db, seeded, "Avaliação")
    assert created.kind == "applied_awaiting_payment"
    _paid(provider, created.adjustment_id)

    result = await _pay(db, seeded, created.adjustment_id)

    assert result.outcome == "adjustment_applied"
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        assert adjustment.status == STATUS_APPLIED
        assert await service_change.held_deposit_cents(session, deposit) == 9000
    [[message]] = sent
    assert "Recebemos o Pix da diferença" in message.body
    assert replans == []


async def test_a_suspended_change_without_a_new_time_still_replans_the_reminders(
    db, provider, sent, replans
):
    seeded = await _seed(db, timing="before")
    created = await _request(db, seeded, "Avaliação")
    assert created.kind == "suspended"
    _paid(provider, created.adjustment_id)
    await _pay(db, seeded, created.adjustment_id)
    assert replans == [seeded[2].id]


class FakeCalendar:
    def __init__(self, name, *, down=False):
        self.name = name
        self.down = down
        self.created: list = []
        self.cancelled: list = []
        self.updates: list = []

    async def is_slot_free(self, start, end, *, ignore_event_id=None):
        return True

    async def create_event(self, *, start, end, summary, description="", **kw):
        if self.down:
            raise RuntimeError("down")
        self.created.append((start, end, summary))
        return {"id": f"{self.name}-new", "htmlLink": f"https://cal/{self.name}"}

    async def cancel_event(self, event_id):
        self.cancelled.append(event_id)

    async def update_event_details(self, event_id, start, end, summary, description=""):
        self.updates.append(event_id)
        return {"id": event_id}


async def _paid_doctor_change(db, provider, monkeypatch, *, new_down=False):
    seeded = await _seed(db, timing="before")
    ana, bruno = await _doctors(db, seeded)
    calendars = {ana: FakeCalendar("ana"), bruno: FakeCalendar("bruno", down=new_down)}

    async def resolve(session, tenant, appointment):
        return calendars.get(appointment.professional_id)

    monkeypatch.setattr(service_change, "calendar_for_appointment", resolve)
    monkeypatch.setattr(
        service_change, "ADJUSTMENT_APPLIED_HOOKS", [service_change.update_agenda_after_payment]
    )
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        row.google_calendar_source = "professional"
        await session.commit()
    created = await _request(
        db, seeded, "Consulta", minutes=30, professional_id=bruno, old_professional_id=ana,
        doctor_changed=True, old_google_calendar_source="professional",
    )
    assert created.kind == "suspended"
    _paid(provider, created.adjustment_id)
    await _pay(db, seeded, created.adjustment_id)
    return seeded, calendars[ana], calendars[bruno], bruno


async def test_a_paid_doctor_change_moves_the_event_to_the_new_doctors_agenda(
    db, provider, sent, replans, monkeypatch
):
    seeded, ana_cal, bruno_cal, bruno = await _paid_doctor_change(db, provider, monkeypatch)
    assert len(bruno_cal.created) == 1
    assert ana_cal.cancelled == ["evt-1"]
    row = await _row(db, seeded[2].id)
    assert (row.professional_id, row.google_event_id) == (bruno, "bruno-new")
    assert row.google_event_link == "https://cal/bruno"
    assert row.google_calendar_source == "professional"
    assert await _all(db, PaymentAttentionItem) == []


async def test_a_doctor_change_whose_new_agenda_fails_opens_the_agenda_item(
    db, provider, sent, replans, monkeypatch
):
    seeded, ana_cal, _bruno_cal, _bruno = await _paid_doctor_change(
        db, provider, monkeypatch, new_down=True
    )
    assert ana_cal.cancelled == []  # the old event is never removed without a new one
    [item] = await _all(db, PaymentAttentionItem)
    assert item.reason == REASON_SERVICE_DIFF_AGENDA_PENDING
    assert (await _row(db, seeded[2].id)).google_event_id == "evt-1"


# --------------------------------------------------------------------------
# fix round 1
# --------------------------------------------------------------------------


async def test_an_applied_change_paid_after_its_visit_was_cancelled_is_an_orphan(
    db, provider, sent
):
    """I1: an "after" clinic cancels the visit while the difference is being paid. The cancel's
    settle finds the charge PAID and leaves it to the webhook, which must not record a payment
    for a consultation that no longer exists."""
    from secretaria.services.payments import adjustment_queries

    seeded = await _seed(db, timing="after")
    created = await _request(db, seeded, "Avaliação")
    assert created.kind == "applied_awaiting_payment"
    _paid(provider, created.adjustment_id)
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        row.status = AppointmentStatus.CANCELLED
        tenant = await session.get(Tenant, seeded[0].id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        settled = await adjustment_queries.settle_open_adjustments(
            session, tenant=tenant, deposit=deposit, now=NOW + timedelta(minutes=4)
        )
        await session.commit()
    assert settled == {str(created.adjustment_id): "payment_in_flight"}

    result = await _pay(db, seeded, created.adjustment_id)

    assert result.outcome == "adjustment_not_applicable"
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
        deposit = await session.get(PixDeposit, seeded[3].id)
        assert adjustment.status == STATUS_CANCELLED
        assert await service_change.held_deposit_cents(session, deposit) == 6000
    [item] = await _all(db, PaymentAttentionItem)
    assert item.reason == REASON_ORPHAN_PAYMENT
    [[message]] = sent
    assert "cancelada" in message.body and "R$ 30,00" in message.body


async def test_an_applied_change_paid_after_the_visit_happened_keeps_the_payment(
    db, provider, sent
):
    """I1: a visit that already happened (attended) keeps the paid difference."""
    seeded = await _seed(db, timing="after")
    created = await _request(db, seeded, "Avaliação")
    async with db() as session:
        row = await session.get(Appointment, seeded[2].id)
        row.status = AppointmentStatus.ATTENDED
        await session.commit()
    _paid(provider, created.adjustment_id)
    result = await _pay(db, seeded, created.adjustment_id)
    assert result.outcome == "adjustment_applied"
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
    assert adjustment.status == STATUS_APPLIED
    assert await _all(db, PaymentAttentionItem) == []


async def test_an_underpaid_applied_change_is_collected_and_the_patient_told_neutrally(
    db, provider, sent
):
    """M3: the change stands; the patient reads what arrived and that the clinic settles the
    rest - never "a troca não foi feita"."""
    from secretaria.models.deposit_adjustment import STATUS_COLLECT_DUE

    seeded = await _seed(db, timing="after")
    created = await _request(db, seeded, "Avaliação")
    _paid(provider, created.adjustment_id, amount=2000)
    result = await _pay(db, seeded, created.adjustment_id)
    assert result.outcome == "adjustment_amount_under"
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, created.adjustment_id)
    assert adjustment.status == STATUS_COLLECT_DUE
    assert (await _row(db, seeded[2].id)).appointment_type == "Avaliação"
    [[message]] = sent
    assert "R$ 20,00" in message.body and "R$ 10,00" in message.body
    assert "combina o restante" in message.body and "não foi feita" not in message.body


@pytest.mark.parametrize("ending", ["underpaid", "not_applicable"])
async def test_a_suspended_change_that_ends_unapplied_replans_the_old_visit(
    db, provider, sent, replans, ending
):
    """M4: the old visit is visible again - its reminders are re-planned once."""
    seeded = await _seed(db, timing="before")
    new_start = seeded[2].start_at + timedelta(days=1)
    created = await _request(
        db, seeded, "Avaliação", start_at=new_start, end_at=new_start + timedelta(minutes=60),
        time_changed=True,
    )
    if ending == "not_applicable":
        async with db() as session:
            session.add(
                Appointment(
                    id=uuid4(), tenant_id=seeded[0].id, patient_id=seeded[1].id,
                    google_event_id="evt-r", appointment_type="Consulta", start_at=new_start,
                    end_at=new_start + timedelta(minutes=30),
                    status=AppointmentStatus.SCHEDULED,
                )
            )
            await session.commit()
    _paid(provider, created.adjustment_id, amount=2000 if ending == "underpaid" else 3000)
    await _pay(db, seeded, created.adjustment_id)
    assert replans == [seeded[2].id]
    assert await _all(db, BookingHold) == []
