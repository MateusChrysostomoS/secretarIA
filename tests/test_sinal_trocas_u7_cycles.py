"""TASK-085 R / U7 - whole cycles of the 3rd round (SPEC §5, D-R10..D-R14), across the merged
slices, through their public entry points.

1. "before", dearer: the old consultation LEAVES the agenda right after the patient's commit
   (its slot is free for others), the Pix expires with the slot still free -> "Manter a antiga"
   -> the old consultation comes back under a new event id (D-R10/D-R8).
2. the old slot taken by another patient -> "Manter a antiga" answers "Outro horário" -> the
   patient picks a day and a time -> the SAME consultation moves there, the paid sinal still
   counts (same deposit, no new charge, no reschedule counted) (D-R11).
3. silence with the old slot taken -> `old_slot_lost` (+ the clinic's item and the patient's
   notice) -> the hub reschedules it from the "rebook" row (D-R11).
4. paid -> the new event on the new doctor's agenda (`difpaid`), nothing left of the old one.
5. the clinic raises a price -> e-mail with the Portal link -> opening = Pix of only the
   difference -> opened again: no second charge -> unpaid -> "cobrar diferença", the
   consultation stays (D-R14).
6. the clinic lowers a price -> e-mail -> opening asks "quer a diferença de volta?" -> Sim -> the
   clinic's refund item; a later edit never e-mails the patient again for the same difference
   (UB1 gap: the sweep ignores what was already settled).
7. convênio: the rules button only with a clinic note; a convênio never exempts the sinal (D-R12).
Plus the U7 integration checks: a patient's "Outro horário" confirmed AFTER the clinic cancelled
is refused (UH review M4).
"""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    FlowState,
    PaymentAttentionItem,
    PixDeposit,
    Tenant,
)
from secretaria.models.appointment_reminder import (
    REMINDER_KIND_PRICE_CHANGE,
    AppointmentReminder,
)
from secretaria.models.deposit_adjustment import (
    KIND_DECREASE,
    KIND_INCREASE,
    MODE_APPLIED,
    OLD_SLOT_REBOOKED,
    OLD_SLOT_RELEASED,
    OLD_SLOT_REPLACED,
    OLD_SLOT_RESTORED,
    STATUS_ASKED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_COLLECT_DUE,
    STATUS_KEPT_OLD,
    STATUS_OLD_SLOT_LOST,
    STATUS_REBOOKED,
    STATUS_REFUND_REQUESTED,
    DepositAdjustment,
)
from secretaria.models.insurance import TenantInsurancePlan
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_RESOLVED,
    REASON_SERVICE_DIFF_REBOOK_DUE,
    REASON_SERVICE_DIFF_REFUND_DUE,
    REASON_SERVICE_DIFF_UNPAID,
)
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import flow_router as fr
from secretaria.services.calendar import CalendarUnavailableError
from secretaria.services.insurance_catalog import ensure_catalog_seeded, load_tenant_insurance
from secretaria.services.patient_insurance_overview import patient_insurance_overview
from secretaria.services.payments import (
    accounts,
    adjustment_queries,
    deposit_gate,
    deposit_lifecycle,
    old_slot,
    price_change,
    service_change,
)
from secretaria.services.payments.provider import CreatedCharge
from secretaria.services.payments.service_change import ChoiceOutcome
from secretaria.workers import tasks
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeCalendar,
    acting,
    r7,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment
from tests.payments_support import FakeRedis
from tests.test_hub_edit_appointment import _iso, provider  # noqa: F401
from tests.test_sinal_trocas_s4_hub import _suspended_world
from tests.test_sinal_trocas_s7_cycles import (  # noqa: F401 - fixtures + the patient's path
    _adjustments,
    _buttons,
    _cycle_doubles,
    _expired,
    _patient_confirms,
    _patient_edit,
    _patient_sees,
    _rows,
    _suspended,
    _suspended_by_the_patient,
    _sweep,
    _tap,
    _utc,
    _webhook_paid,
    _wire_agendas,
    charges,
    engine_messages,
)
from tests.test_sinal_trocas_ua3_patient import (  # noqa: F401 - the patient's rebook doubles
    adjustment_of,
    agenda,
    reply_of,
    ua2,
    ua3_world,
    wa_messages,
)
from tests.test_sinal_trocas_ua3_rebook import (  # noqa: F401
    _assert_rebooked,
    spies,
    wa_confirm,
    wa_rebook_to_first_slot,
)
from tests.test_sinal_trocas_ub1_sweep import (
    CHANGED_AT,
    LINK,
    NOW,
    START,
    mail,  # noqa: F401
    seed_tenant,
    seed_visit,
)

# --------------------------------------------------------------------------------------------
# shared bits
# --------------------------------------------------------------------------------------------


async def _another_patient_takes(db, tenant_id, start: datetime, *, minutes: int = 30) -> None:
    """Another patient books the old slot (same agenda: the clinic's, no doctor) while the
    consultation is out of the agenda - the row the occupancy reads (`occupies_slot_clause`)."""
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant_id,
                patient_id=None,
                google_event_id="evt-other-patient",
                appointment_type="Consulta",
                start_at=start,
                end_at=start + timedelta(minutes=minutes),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()


async def _items(db, reason: str | None = None) -> list[PaymentAttentionItem]:
    async with db() as session:
        rows = list(await session.scalars(select(PaymentAttentionItem)))
    return [row for row in rows if reason is None or row.reason == reason]


async def _deposit_of(db, appointment_id) -> PixDeposit:
    async with db() as session:
        return await session.scalar(
            select(PixDeposit).where(PixDeposit.appointment_id == appointment_id)
        )


async def _vacated(db, adjustment) -> None:
    """The old event left the agenda. `_suspended_by_the_patient` confirms before the agenda
    doubles are wired, so its post-commit `vacate_old_slot` claimed the slot (`releasing`) and
    could not reach an agenda; the payments sweep's `retry_vacates` (~2 min later) finishes
    it, exactly as in production after a failed first delete."""
    current = await adjustment_of(db, adjustment.id)
    if current.old_slot != OLD_SLOT_RELEASED:
        async with db() as session:
            await old_slot.retry_vacates(
                session, now=datetime.now(UTC) + old_slot.RETRY_AFTER + timedelta(seconds=1)
            )
        current = await adjustment_of(db, adjustment.id)
    assert current.old_slot == OLD_SLOT_RELEASED


@pytest.fixture
def hub_agenda(monkeypatch, db):
    """The hub and `old_slot` read the SAME agenda double (`FakeCalendar`, one per label) and
    `old_slot` opens its own sessions on the test DB. `create_event(event_id=)` behaves like the
    Google adapter (a live id is handed back; a deleted one is "taken"). Returns every write."""
    calls: list[tuple] = []
    monkeypatch.setattr(old_slot, "async_session_factory", db)

    async def _calendar(session, tenant, appointment):
        pid = getattr(appointment, "professional_id", None)
        return FakeCalendar.get(str(pid) if pid else "tenant")

    monkeypatch.setattr(service_change, "calendar_for_appointment", _calendar)

    async def create_event(
        self, start, end, summary, description="", reminders=None, *, exclusive=False,
        event_id=None,
    ):
        events = self.__dict__.setdefault("events", {})
        if event_id is not None and event_id in events:
            if events[event_id]["status"] == "cancelled":
                raise CalendarUnavailableError("event id is taken")
            return dict(events[event_id])
        self.created.append((start, end, summary, description))
        eid = event_id or f"evt-new-{self.label}-{len(self.created)}"
        calls.append((self.label, "create", eid))
        events[eid] = {"id": eid, "status": "confirmed", "htmlLink": f"https://cal/{eid}"}
        return dict(events[eid])

    async def cancel_event(self, event_id):
        self.cancelled.append(event_id)
        calls.append((self.label, "cancel", event_id))
        events = self.__dict__.setdefault("events", {})
        if event_id in events:
            events[event_id]["status"] = "cancelled"

    async def get_event(self, event_id):
        found = self.__dict__.get("events", {}).get(event_id)
        return dict(found) if found is not None else None

    monkeypatch.setattr(FakeCalendar, "create_event", create_event)
    monkeypatch.setattr(FakeCalendar, "cancel_event", cancel_event)
    monkeypatch.setattr(FakeCalendar, "get_event", get_event, raising=False)
    return calls


# --------------------------------------------------------------------------------------------
# 1. "before" dearer -> the old one leaves -> expires -> slot free -> "Manter a antiga"
# --------------------------------------------------------------------------------------------


async def test_cycle_1_the_old_visit_leaves_the_agenda_and_keep_brings_it_back(
    client: AsyncClient, db, acting, charges, monkeypatch, engine_messages
):
    # The agendas are wired BEFORE the patient confirms, so the post-commit vacate runs for
    # real (`AppliedEdit.vacate_adjustment_id` -> `old_slot.vacate_old_slot`, own session).
    monkeypatch.setattr(old_slot, "async_session_factory", db)
    world, adjustment, bruno, _new = await _suspended_by_the_patient(
        db, acting, monkeypatch, client
    )
    clinic_agenda, _bruno_agenda = _wire_agendas(monkeypatch, world, bruno)
    await _vacated(db, adjustment)
    assert clinic_agenda.cancelled == ["evt-1"]  # D-R10: out of the doctor's agenda
    # ... and the slot is free for anybody else while the Pix waits.
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        row = await session.get(Appointment, world.appointment.id)
        change = await session.get(DepositAdjustment, adjustment.id)
        assert await old_slot.old_slot_free(
            session, tenant=tenant, appointment=row, adjustment=change
        ) is True
    assert await clinic_agenda.check_availability(
        world.start_at, world.start_at + timedelta(minutes=30)
    ) == []
    assert await _suspended(db, world)

    await _expired(db, world, adjustment, engine_messages)  # "Manter a antiga" offered
    await _tap(world, "svcdiffkeep", adjustment.id)

    kept = await adjustment_of(db, adjustment.id)
    assert (kept.status, kept.old_slot) == (STATUS_KEPT_OLD, OLD_SLOT_RESTORED)
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.status) == ("Consulta", AppointmentStatus.SCHEDULED)
    restored = old_slot.restore_event_id(adjustment.id, calendar=clinic_agenda)
    assert row.google_event_id == restored  # recreated, never the deleted id
    [created] = clinic_agenda.created
    assert created["id"] == restored and _utc(created["start"]) == _utc(world.start_at)
    assert await _rows(db, BookingHold) == []  # the new window is free again
    upcoming, _nearest = await _patient_sees(db, world)
    assert [item["id"] for item in upcoming] == [str(world.appointment.id)]
    assert not await _suspended(db, world)


# --------------------------------------------------------------------------------------------
# 2. the old slot taken -> "Outro horário" -> the same consultation, the paid sinal counts
# --------------------------------------------------------------------------------------------


async def test_cycle_2_a_taken_slot_rebooks_the_same_visit_with_the_paid_sinal(
    db, agenda, ua2, spies
):
    world, adjustment_id = await ua3_world(db)
    paid_before = await _deposit_of(db, world.appointment.id)
    agenda.add("evt-someone", world.start_at, world.start_at + timedelta(minutes=30))

    # "Manter a antiga" with the slot taken: nothing changes, "Outro horário" is offered.
    await tasks._handle_action_button(reply_of(world), "svcdiffkeep", str(adjustment_id))
    [(kind, _to, body, buttons)] = wa_messages()
    assert body == service_change.choice_answer_text(
        ChoiceOutcome(kind="slot_taken", adjustment_id=adjustment_id)
    )
    assert buttons[0][1] == "Outro horário"

    new_start = await wa_rebook_to_first_slot(db, world, adjustment_id)
    await wa_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    adjustment = await adjustment_of(db, adjustment_id)
    _assert_rebooked(row, adjustment, agenda, new_start, adjustment_id)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_REBOOKED, OLD_SLOT_REBOOKED)
    # The paid sinal is the same row, untouched, still on this consultation.
    deposit = await _deposit_of(db, world.appointment.id)
    assert deposit.id == paid_before.id
    assert deposit.status == PixDepositStatus.PAID
    assert deposit.amount_cents == paid_before.amount_cents
    assert (deposit.reschedule_count or 0) == (paid_before.reschedule_count or 0)
    assert (await get_flow_state(db, world)) == FlowState.IDLE


async def get_flow_state(db, world):
    from tests._reminders_r3 import get_conversation

    return (await get_conversation(db, world)).flow_state


# --------------------------------------------------------------------------------------------
# 3. silence with the old slot taken -> old_slot_lost -> the hub reschedules it
# --------------------------------------------------------------------------------------------


async def test_cycle_3_silence_with_the_slot_taken_then_the_clinic_reschedules(
    client: AsyncClient, db, acting, provider, hub_agenda, engine_messages
):
    world = await _suspended_world(db, acting, client)  # staff asked Avaliação; "before"
    [adjustment] = await _adjustments(db)
    assert adjustment.old_slot == OLD_SLOT_RELEASED  # the edit's post-commit vacate ran
    await _another_patient_takes(db, world.tenant.id, world.start_at)

    # The Pix expires: the old slot is taken, so "Outro horário" replaces "Manter a antiga".
    engine_messages.clear()
    await _sweep(db, _utc(adjustment.expires_at) + timedelta(minutes=1))
    choosing = await adjustment_of(db, adjustment.id)
    assert choosing.status == STATUS_AWAITING_CHOICE
    [[card]] = engine_messages
    assert _buttons(card) == ["svcdiffother", "svcdiffresend", "svcdiffboth"]

    # No answer in time: the old consultation cannot come back -> old_slot_lost.
    engine_messages.clear()
    await _sweep(db, _utc(choosing.expires_at) + timedelta(minutes=1))
    lost = await adjustment_of(db, adjustment.id)
    assert lost.status == STATUS_OLD_SLOT_LOST
    [item] = await _items(db, REASON_SERVICE_DIFF_REBOOK_DUE)
    assert item.status == ATTENTION_OPEN
    [[notice]] = engine_messages
    assert notice.body == service_change.old_slot_lost_text("Consulta")
    assert _buttons(notice) == ["svcdiffother", "svcdiffboth"]
    assert (await _deposit_of(db, world.appointment.id)).status == PixDepositStatus.PAID

    # The hub lists it as "rebook" (with the patient) and reschedules it from that row.
    window = {
        "start": _iso(world.start_at - timedelta(days=1)),
        "end": _iso(world.start_at + timedelta(days=5)),
    }
    listed = (await client.get(f"{CALENDAR}/awaiting-payment", params=window)).json()
    [rebook] = [row for row in listed if row["kind"] == "rebook"]
    assert rebook["id"] == str(adjustment.id)
    assert rebook["appointment_id"] == str(world.appointment.id)
    assert rebook["patient_name"]
    new = world.start_at + timedelta(days=2)
    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/reschedule",
        json={
            "new_start": _iso(new),
            "new_end": _iso(new + timedelta(minutes=30)),
            "rebook_adjustment_id": str(adjustment.id),
        },
    )

    assert response.status_code == 200, response.text
    row = await reload_appointment(db, world.appointment.id)
    assert _utc(row.start_at) == _utc(new)
    assert row.status == AppointmentStatus.RESCHEDULED
    assert row.google_event_id.startswith("difhub")
    done = await adjustment_of(db, adjustment.id)
    assert (done.status, done.old_slot) == (STATUS_REBOOKED, OLD_SLOT_REBOOKED)
    [item] = await _items(db, REASON_SERVICE_DIFF_REBOOK_DUE)
    assert (item.status, item.resolution_action) == (ATTENTION_RESOLVED, "rebooked")
    deposit = await _deposit_of(db, world.appointment.id)
    assert deposit.status == PixDepositStatus.PAID  # the paid sinal still counts
    listed = (await client.get(f"{CALENDAR}/awaiting-payment", params=window)).json()
    assert [row for row in listed if row["kind"] == "rebook"] == []


# --------------------------------------------------------------------------------------------
# 4. paid -> a new event, nothing of the old one
# --------------------------------------------------------------------------------------------


async def test_cycle_4_paid_after_the_old_visit_left_creates_the_new_event(
    client: AsyncClient, db, acting, charges, monkeypatch, engine_messages
):
    monkeypatch.setattr(old_slot, "async_session_factory", db)
    world, adjustment, bruno, new_start = await _suspended_by_the_patient(
        db, acting, monkeypatch, client
    )
    clinic_agenda, bruno_agenda = _wire_agendas(monkeypatch, world, bruno)
    await _vacated(db, adjustment)

    result = await _webhook_paid(
        db, world, charges, adjustment.id, "pay_diff_1", at=datetime.now(UTC) + timedelta(minutes=5)
    )

    assert result.outcome == "adjustment_applied"
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.professional_id) == ("Avaliação", bruno)
    assert _utc(row.start_at) == _utc(new_start)
    assert row.google_event_id == old_slot.paid_event_id(adjustment.id, calendar=bruno_agenda)
    assert [event["id"] for event in bruno_agenda.created] == [row.google_event_id]
    assert clinic_agenda.cancelled == ["evt-1"] and clinic_agenda.created == []
    assert (await adjustment_of(db, adjustment.id)).old_slot == OLD_SLOT_REPLACED
    assert await _rows(db, BookingHold) == []
    assert not await _suspended(db, world)


# --------------------------------------------------------------------------------------------
# U7 integration check (UH review M4): "Outro horário" confirmed after the clinic cancelled
# --------------------------------------------------------------------------------------------


async def _clinic_cancels(db, world) -> None:
    """What the hub's /cancel commits: the row CANCELLED + the paid sinal's cancellation rules
    (`on_appointment_cancelled` -> `settle_open_adjustments` closes the change and its item)."""
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant.id)
        row = await session.get(Appointment, world.appointment.id)
        row.status = AppointmentStatus.CANCELLED
        await deposit_lifecycle.on_appointment_cancelled(
            session,
            tenant=tenant,
            appointment=row,
            waba_token=None,
            cancelled_by=deposit_lifecycle.CANCELLED_BY_CLINIC,
        )
        await session.commit()
    await adjustment_queries.drain_restore_discards()


@pytest.mark.parametrize("settled", [True, False])
async def test_other_time_confirmed_after_the_clinic_cancelled_is_refused(
    db, agenda, ua2, spies, settled
):
    """The clinic cancels while the patient is between "Outro horário" and Confirmar (the
    event is being created). `settled`: the hub's real cancel (the change closes); not settled:
    only the row went CANCELLED (defence in depth, U7 - the patient's write checks the locked
    row is still live). Either way nothing of the move stands and the created event goes."""
    world, adjustment_id = await ua3_world(db, status=STATUS_OLD_SLOT_LOST)
    await wa_rebook_to_first_slot(db, world, adjustment_id)

    async def _cancelled_meanwhile():
        if settled:
            await _clinic_cancels(db, world)
        else:
            async with db() as session:
                await session.execute(
                    update(Appointment)
                    .where(Appointment.id == world.appointment.id)
                    .values(status=AppointmentStatus.CANCELLED)
                )
                await session.commit()

    agenda.on_create = _cancelled_meanwhile

    await wa_confirm(db, world)

    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.CANCELLED
    assert row.google_event_id == "evt-old"
    [created] = agenda.created
    assert created["id"] not in agenda.live()  # discarded
    adjustment = await adjustment_of(db, adjustment_id)
    assert adjustment.status == (STATUS_CANCELLED if settled else STATUS_OLD_SLOT_LOST)
    assert wa_messages()[-1][2] == service_change.choice_answer_text(
        ChoiceOutcome(kind="already_decided")
    )
    assert spies.changes == [] and spies.replans == []


# --------------------------------------------------------------------------------------------
# 5-6. D-R14: the clinic changes a price (sweep -> e-mail -> opening -> action)
# --------------------------------------------------------------------------------------------


@pytest.fixture
def price_world(monkeypatch, db, mail, provider):
    """The UB1 clinic (Consulta R$ 200,00, sinal 30 % = R$ 60,00 PAID, a Portal patient with
    e-mail, the visit 12 days ahead) on the test DB, the provider double for the difference."""
    monkeypatch.setattr(price_change, "async_session_factory", db)
    provider.created_charge = CreatedCharge(
        provider_charge_id="pay_price_diff", copy_paste="000201pricediff"
    )
    return mail


async def _seed_price_clinic(db):
    async with db() as session:
        tenant = await seed_tenant(session)
        patient, appointment, deposit = await seed_visit(session, tenant)
        await session.commit()
    return tenant, patient, appointment, deposit


async def _reprice(db, tenant_id, price: str) -> None:
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        tenant.appointment_types = [
            {"name": "Consulta", "duration_min": 30, "is_active": True, "price": price}
        ]
        await session.commit()


async def _sweep_prices(tenant_id, *, changed_at=CHANGED_AT, now=NOW) -> int:
    return await price_change.sweep_price_changes(
        tenant_id, changed_at=changed_at, now=now, redis=FakeRedis()
    )


def _reminder_of(mail_sent) -> str:
    (_to, template, variables) = mail_sent
    assert template == price_change.EMAIL_TEMPLATE
    link = variables["link_line"] if "link_line" in variables else str(variables)
    assert LINK in link  # a direct link into the Portal conversation
    return UUID(link.split(LINK, 1)[1].split()[0].strip("\"'}),"))


async def _open(db, tenant_id, reminder_id, patient_id, *, now=NOW):
    async with db() as session:
        tenant = await session.get(Tenant, tenant_id)
        opening = await price_change.open_price_notice(
            session, tenant=tenant, reminder_id=reminder_id, patient_id=patient_id, now=now
        )
        tenant = await session.get(Tenant, tenant_id)
        return opening, price_change.price_opening_messages(opening, tenant=tenant)


async def test_cycle_5_a_price_rise_mails_opens_with_the_pix_and_unpaid_is_collected(
    db, price_world, provider, engine_messages
):
    tenant, patient, appointment, _deposit = await _seed_price_clinic(db)
    await _reprice(db, tenant.id, "R$ 300,00")  # sinal R$ 60 -> R$ 90

    assert await _sweep_prices(tenant.id) == 1
    [mail_sent] = price_world["sent"]
    assert "R$" not in str(mail_sent[2].get("service", ""))  # no values in the e-mail
    reminder_id = _reminder_of(mail_sent)
    async with db() as session:
        notice = await session.get(AppointmentReminder, reminder_id)
    assert (notice.kind, notice.appointment_id) == (REMINDER_KIND_PRICE_CHANGE, appointment.id)

    opening, messages = await _open(db, tenant.id, notice.id, patient.id)

    assert (opening.kind, opening.diff_cents) == ("pix", 3000)
    assert "subiu" in messages[0].body and "000201pricediff" in "".join(m.body for m in messages)
    [rise] = await _adjustments(db)
    assert (rise.kind, rise.mode, rise.status) == (
        KIND_INCREASE, MODE_APPLIED, STATUS_AWAITING_PAYMENT
    )
    # Opened again: the same Pix is pointed at, never a second charge.
    again, _ = await _open(db, tenant.id, notice.id, patient.id)
    assert again.kind == "pending"
    assert provider.called("create_pix_charge") == 1

    engine_messages.clear()
    async with db() as session:
        await service_change.expire_adjustments(
            session, now=_utc(rise.expires_at) + timedelta(minutes=1)
        )

    unpaid = await adjustment_of(db, rise.id)
    assert unpaid.status == STATUS_COLLECT_DUE
    [item] = await _items(db, REASON_SERVICE_DIFF_UNPAID)
    assert (item.status, item.amount_cents) == (ATTENTION_OPEN, 3000)
    assert [m.body for batch in engine_messages for m in batch] == [
        service_change.price_unpaid_text(3000)
    ]
    row = await reload_appointment(db, appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED  # the consultation stays
    assert _utc(row.start_at) == _utc(START)


async def test_cycle_6_a_price_drop_asks_and_yes_opens_the_refund_item_once(
    db, price_world, provider
):
    tenant, patient, appointment, deposit = await _seed_price_clinic(db)
    await _reprice(db, tenant.id, "R$ 100,00")  # sinal R$ 60 -> R$ 30

    assert await _sweep_prices(tenant.id) == 1
    [first_mail] = price_world["sent"]
    reminder_id = _reminder_of(first_mail)

    opening, messages = await _open(db, tenant.id, reminder_id, patient.id)

    assert (opening.kind, opening.diff_cents) == ("refund_question", 3000)
    assert "Quer essa diferença de volta?" in messages[0].body
    assert [b[0].split("|")[0] for b in messages[0].buttons] == ["svcdiffyes", "svcdiffno"]
    [drop] = await _adjustments(db)
    assert (drop.kind, drop.status) == (KIND_DECREASE, STATUS_ASKED)
    async with db() as session:
        answer = await service_change.answer_refund_question(
            session, tenant_id=tenant.id, adjustment_id=drop.id, accept=True, now=NOW
        )
        await session.commit()
    assert answer == STATUS_REFUND_REQUESTED
    [item] = await _items(db, REASON_SERVICE_DIFF_REFUND_DUE)
    assert (item.status, item.suggested_refund_cents) == (ATTENTION_OPEN, 3000)
    assert provider.called("create_pix_charge") == 0  # never a refund by the system

    # UB1 gap: the clinic edits prices again later (same R$ 100,00 in force). The sweep nets
    # the settled refund out (`preview_price_requote`): nothing to tell, no second e-mail.
    later = NOW + timedelta(hours=2)
    await _reprice(db, tenant.id, "R$ 100,00")
    assert await _sweep_prices(tenant.id, changed_at=later - timedelta(minutes=1), now=later) == 0
    assert price_world["sent"] == [first_mail]
    async with db() as session:
        row = await session.get(Appointment, appointment.id)
        dep = await session.get(PixDeposit, deposit.id)
        assert not await price_change.price_notice_needed(
            session, tenant=await session.get(Tenant, tenant.id), appointment=row, deposit=dep,
            now=later,
        )
    assert len(await _adjustments(db)) == 1


def _card(snapshot):
    """The tenant config the booking's convênio card reads (`insurance_plans`)."""
    return SimpleNamespace(insurance_plans=snapshot.plans, insurances=None)


# --------------------------------------------------------------------------------------------
# 7. convênio: the rules button only with a note; a convênio never exempts the sinal
# --------------------------------------------------------------------------------------------


async def test_cycle_7_convenio_notes_drive_the_button_and_never_the_sinal(db, provider):
    async with db() as session:
        await ensure_catalog_seeded(session)
        tenant = await seed_tenant(session)
        tenant.deposit_timing = "before"
        tenant.insurance_mode = "shared"
        account = await accounts.get_or_create_account_row(
            session, tenant_id=tenant.id, provider="asaas"
        )
        account.last_verified_at = NOW  # verified by the connection flow ("before" gates on it)
        plan = TenantInsurancePlan(tenant_id=tenant.id, custom_name="Plano Sol")
        session.add(plan)
        patient, _appointment, _deposit = await seed_visit(session, tenant)
        await session.commit()
        snapshot = await load_tenant_insurance(session, tenant.id)
        overview = await patient_insurance_overview(session, tenant.id)
    # No clinic note anywhere: no button, no plan in the sheet (and no detail text at all).
    assert fr.insurance_plans_have_clinic_notes(snapshot.plans) is False
    assert overview["plans"] == []
    assert fr._enter_insurance(_card(snapshot)).bubbles[0].source.has_clinic_notes is False

    async with db() as session:
        session.add(TenantInsurancePlan(tenant_id=tenant.id, custom_name="GEAP"))
        await session.commit()
        snapshot = await load_tenant_insurance(session, tenant.id)
        overview = await patient_insurance_overview(session, tenant.id)
    # TASK-090: the clinic's "Outro" has no payment text either - still no button, no plan.
    assert fr.insurance_plans_have_clinic_notes(snapshot.plans) is False
    assert fr._enter_insurance(_card(snapshot)).bubbles[0].source.has_clinic_notes is False
    assert overview["plans"] == []

    # D-R12: a booking with ANY convênio pays the same sinal as a private one.
    async with db() as session:
        tenant = await session.get(Tenant, tenant.id)
        quotes = [
            await deposit_gate.quote_deposit(
                session,
                tenant,
                patient_id=patient.id,
                appointment_type="Consulta",
                professional_id=None,
                insurance=insurance,
            )
            for insurance in ("Plano Sol", "GEAP", None)
        ]
    assert {(q.outcome, q.amount_cents) for q in quotes} == {("required", 6000)}
