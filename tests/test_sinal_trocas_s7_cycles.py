"""TASK-085 R / S7a - whole cycles across the slices, through the public entry points only.

The patient's "Alterar Dados" confirmation (`tasks._apply_flow_result`, S3a/S3b), the payments
sweep (`service_change.expire_adjustments`, S1), the provider's webhook
(`event_processor.process_payment_event_row` + `run_post_commit`, S1), the patient's taps
(`tasks._handle_action_button`, S3b), the hub's routes (S4) and the patient's lists / reminders
(S5). The world of tests/test_hub_edit_appointment.py: a PAID sinal of R$ 100,00 at 30 % on
"Consulta" (R$ 200,00); "Avaliação" R$ 500,00 (sinal R$ 150,00: R$ 50,00 dearer, 50 min).

(a) "before" clinic, the patient asks a dearer change (another doctor, another day): suspended -
    hidden from the patient's lists and reminders, the hub still sees it WITH the summary; its Pix
    expires -> the three buttons; keep / resend (paid -> the new visit applied, the doctor's
    agenda moved) / cancel both; silence -> kept_old.
(b) "after" clinic, dearer: applied now + Pix; unpaid -> the clinic's collect item.
(c) an UNPAID sinal + a change (patient and staff): old Pix voided, a new one sent.
(d) the clinic on a visit the PATIENT suspended: 409 everywhere, "Liberar horário" settles it.
(e) a clinic without sinal: nothing of the money changes.
"""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update

from secretaria.ai.formatter import TextBubble
from secretaria.api.hub import calendar as hub_calendar
from secretaria.models import (
    AppointmentStatus,
    BookingHold,
    FlowState,
    PaymentAttentionItem,
    PaymentWebhookEvent,
    PixDeposit,
    Professional,
    Tenant,
)
from secretaria.models.deposit_adjustment import (
    MODE_APPLIED,
    MODE_SUSPEND,
    OLD_SLOT_RELEASED,
    OLD_SLOT_REPLACED,
    OLD_SLOT_RESTORED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_COLLECT_DUE,
    STATUS_EXPIRED,
    STATUS_KEPT_OLD,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import REASON_SERVICE_DIFF_UNPAID
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services import appointment_edit as ae, flow_router as fr, reminder_opening
from secretaria.services.patient_context import load_upcoming_appointments
from secretaria.services.payments import (
    adjustment_suspension,
    deposit_lifecycle,
    event_processor,
    notify,
    old_slot,
    service_change,
)
from secretaria.services.payments.provider import ChargeState, CreatedCharge, PaymentStatus
from secretaria.services.payments.webhook_payload import event_to_dict
from secretaria.workers import tasks
from tests._edit_flow_support import EditCalendar
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeCalendar,
    acting,
    r7,
    sent,
    set_tenant,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, FakeWhatsAppClient, reload_appointment
from tests.payments_support import make_event
from tests.test_hub_edit_appointment import _iso, _paid_world, _post, _world, provider  # noqa: F401
from tests.test_sinal_trocas_s4_hub import _events

SUSPENDED_409 = {
    "code": "appointment_suspended",
    "message": (
        "Essa consulta aguarda o Pix da diferença de uma troca; enquanto isso, só é possível "
        "liberar o horário."
    ),
}


@pytest.fixture(autouse=True)
def _cycle_doubles(monkeypatch, db):
    """Every message the engine sends after a commit is recorded (`notify.deliver`); the sinal's
    own module reaches the same in-memory database and WhatsApp double; the sweep starts at its
    head; the reminders' re-plan is recorded."""
    engine_messages: list = []

    async def _deliver(session, tenant, patient, messages, **kw):
        engine_messages.append(list(messages))

    monkeypatch.setattr(notify, "deliver", _deliver)
    monkeypatch.setattr(deposit_lifecycle, "async_session_factory", db)
    monkeypatch.setattr(deposit_lifecycle, "WhatsAppClient", FakeWhatsAppClient)
    from secretaria.services import tenant_config

    async def _token(session, tenant_id):
        return "waba-token"  # the clinic's stored token (the hub path reads it itself)

    monkeypatch.setattr(tenant_config, "get_waba_token", _token)
    monkeypatch.setattr(service_change, "_SWEEP_OFFSET", 0)
    monkeypatch.setattr(service_change, "_VOID_OFFSET", 0)
    replans: list = []

    async def _replan(appointment_id, **kw):
        replans.append(appointment_id)
        return 0

    from secretaria.services import reminder_hooks

    monkeypatch.setattr(reminder_hooks, "after_appointment_rescheduled", _replan)

    async def no_secretary(tenant_id):
        return None

    monkeypatch.setattr(hub_calendar.brain_staff, "fetch_has_secretary", no_secretary)
    return engine_messages


@pytest.fixture
def engine_messages(_cycle_doubles) -> list:
    return _cycle_doubles


@pytest.fixture
def charges(provider):
    """One provider charge per creation, in order: the first difference Pix, then a re-sent one
    (or the new sinal)."""
    provider.created_charge = [
        CreatedCharge(provider_charge_id="pay_diff_1", copy_paste="000201first"),
        CreatedCharge(provider_charge_id="pay_diff_2", copy_paste="000201second"),
    ]
    return provider


# --------------------------------------------------------------------------------------------
# helpers - the public entry points
# --------------------------------------------------------------------------------------------


async def _reload_tenant(db, world) -> None:
    async with db() as session:
        world.tenant = await session.get(Tenant, world.tenant.id)


async def _clinic(db, acting, *, timing: str, paid: bool = True):
    world = await (_paid_world(db, acting) if paid else _world(db, acting))
    await set_tenant(db, world.tenant.id, deposit_timing=timing)
    if not paid:
        from tests.test_hub_edit_appointment import PRICED

        await set_tenant(db, world.tenant.id, appointment_types=PRICED, pix_deposit_enabled=False)
    await _reload_tenant(db, world)
    FakeCalendar.reset()
    return world


async def _doctor(db, world, name="Dr. Bruno"):
    async with db() as session:
        doctor = Professional(tenant_id=world.tenant.id, name=name)
        session.add(doctor)
        await session.commit()
        return doctor.id


def _patient_edit(world, *, service="Avaliação", minutes=50, start=None, doctor=None) -> dict:
    """What the router's Confirmar hands the worker for a money change (the agenda deferred)."""
    start = start or world.start_at
    return {
        "appointment_id": world.appointment.id,
        "old_google_event_id": "evt-1",
        "google_event_id": "evt-1",
        "google_event_link": None,
        "appointment_type": service,
        "professional_id": doctor,
        "old_professional_id": None,
        "old_google_calendar_source": None,
        "insurance": None,
        "attendee_name": None,
        "start_at": start,
        "end_at": start + timedelta(minutes=minutes),
        "time_changed": start != world.start_at,
        "doctor_changed": doctor is not None,
        "calendar_changed": doctor is not None,
        "calendar": EditCalendar(),
        "patient_name": "Maria",
        "calendar_deferred": True,
    }


def _reply(world):
    return tasks._ReplyContext(
        conversation_id=world.conversation.id, patient_ref=WA_ID, inbound_body="x"
    )


async def _patient_confirms(world, edit) -> None:
    result = fr.FlowRouterResult(
        action="reply",
        bubbles=[
            TextBubble(body=f"{ae.EDIT_APPLIED}\n\nServiço: {edit['appointment_type']}"),
            fr.MenuBubble(body="menu", labels=["Agendar", "Outro"]),
        ],
        flow_state=FlowState.MENU,
        appointment_edit=edit,
    )
    await tasks._apply_flow_result(
        _reply(world), result, WA_ID, tenant=world.tenant, waba_token="t"
    )


async def _tap(world, action, adjustment_id) -> None:
    await tasks._handle_action_button(_reply(world), action, str(adjustment_id))


async def _sweep(db, at: datetime) -> None:
    async with db() as session:
        await service_change.expire_adjustments(session, now=at)


async def _webhook_paid(db, world, provider, adjustment_id, charge_id, *, at) -> object:
    provider.state = ChargeState(
        status=PaymentStatus.PAID,
        paid_amount_cents=5000,
        external_reference=str(adjustment_id),
        paid_at=at,
    )
    event = make_event(
        PaymentStatus.PAID,
        event_id=f"evt_{charge_id}",
        charge_id=charge_id,
        amount=5000,
        reference=str(adjustment_id),
    )
    async with db() as session:
        row = PaymentWebhookEvent(
            tenant_id=world.tenant.id,
            provider="asaas",
            event_id=event.event_id,
            provider_charge_id=event.provider_charge_id,
            event_type=event.event_type,
            payload={"normalized": event_to_dict(event)},
        )
        session.add(row)
        await session.commit()
        result = await event_processor.process_payment_event_row(session, row.id, now=at)
        tenant = await session.get(Tenant, world.tenant.id)
        await event_processor.run_post_commit(session, tenant, result)
    return result


async def _adjustments(db) -> list[DepositAdjustment]:
    async with db() as session:
        query = select(DepositAdjustment).order_by(DepositAdjustment.created_at)
        return list((await session.scalars(query)).all())


async def _rows(db, model) -> list:
    async with db() as session:
        return list((await session.scalars(select(model))).all())


async def _patient_sees(db, world) -> tuple[list, object]:
    """What the patient side reads: the upcoming list (greeting, AI, menus) and the reminders'
    nearest visit."""
    async with db() as session:
        upcoming = await load_upcoming_appointments(session, world.tenant.id, world.patient.id)
        nearest = await reminder_opening.nearest_live_appointment(
            session, world.tenant.id, world.patient.id, now=datetime.now(UTC)
        )
    return upcoming, nearest


async def _suspended(db, world) -> bool:
    async with db() as session:
        return await adjustment_suspension.is_suspended(
            session, tenant_id=world.tenant.id, appointment_id=world.appointment.id
        )


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _buttons(message) -> list[str]:
    return [button_id.split("|", 1)[0] for button_id, _label in message.buttons]


# --------------------------------------------------------------------------------------------
# (a) "before": suspended -> Pix expires -> three buttons
# --------------------------------------------------------------------------------------------


async def _suspended_by_the_patient(db, acting, monkeypatch, client):
    """The patient asks Avaliação with Dr. Bruno, a day later; the clinic books after the sinal."""
    world = await _clinic(db, acting, timing="before")
    bruno = await _doctor(db, world)
    new_start = world.start_at + timedelta(days=1)

    await _patient_confirms(world, _patient_edit(world, start=new_start, doctor=bruno))

    [adjustment] = await _adjustments(db)
    assert (adjustment.mode, adjustment.status, adjustment.diff_cents) == (
        MODE_SUSPEND,
        STATUS_AWAITING_PAYMENT,
        5000,
    )
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.professional_id) == ("Consulta", None)  # nothing written
    [hold] = await _rows(db, BookingHold)
    assert (hold.adjustment_id, hold.professional_id) == (adjustment.id, bruno)
    # The patient no longer sees it - lists and reminders (S5) ...
    upcoming, nearest = await _patient_sees(db, world)
    assert upcoming == [] and nearest is None
    # ... the clinic does, with what the change would do (S4 + S7a).
    event = await _events(client, world, monkeypatch)
    assert event["suspended"] is True
    old_local = world.start_at.astimezone(hub_calendar.ZoneInfo("America/Sao_Paulo"))
    new_local = new_start.astimezone(hub_calendar.ZoneInfo("America/Sao_Paulo"))
    assert event["pending_change_summary"] == (
        "Consulta → Avaliação; Agenda da clínica → Dr. Bruno; "
        f"{old_local:%d/%m %H:%M} → {new_local:%d/%m %H:%M}"
    )
    return world, adjustment, bruno, new_start


def _wire_agendas(monkeypatch, world, bruno):
    """UA2 (D-R10): the agendas the old event leaves and comes back to - the clinic's (the old
    consultation, `evt-1`) and Dr. Bruno's - as the UA1 Google double (ids kept; a re-sent id
    answers the same event)."""
    from tests.test_sinal_trocas_ua1_old_slot import _Calendar

    clinic, doctor = _Calendar(), _Calendar()
    clinic.add("evt-1", world.start_at, world.start_at + timedelta(minutes=30))

    async def _agenda(session, tenant, appointment):
        return doctor if appointment.professional_id == bruno else clinic

    monkeypatch.setattr(service_change, "calendar_for_appointment", _agenda)
    return clinic, doctor


async def _expired(db, world, adjustment, engine_messages):
    """The sweep after the Pix's deadline: voided, the patient gets the three buttons."""
    engine_messages.clear()
    await _sweep(db, _utc(adjustment.expires_at) + timedelta(minutes=1))
    async with db() as session:
        current = await session.get(DepositAdjustment, adjustment.id)
    assert current.status == STATUS_AWAITING_CHOICE
    [[card]] = engine_messages
    assert _buttons(card) == ["svcdiffkeep", "svcdiffresend", "svcdiffboth"]
    assert await _suspended(db, world)  # still hidden while the patient chooses
    return current


async def test_a_before_change_kept_by_the_patient(
    client: AsyncClient, db, acting, charges, monkeypatch, engine_messages
):
    world, adjustment, bruno, _new = await _suspended_by_the_patient(
        db, acting, monkeypatch, client
    )
    # UA2: the old event leaves the agenda (the sweep) and the tap brings it back; the tap
    # itself is the patient's worker (UA3: `old_slot.restore_old_slot(trigger="tap")`).
    _wire_agendas(monkeypatch, world, bruno)
    await _expired(db, world, adjustment, engine_messages)

    await _tap(world, "svcdiffkeep", adjustment.id)

    [current] = await _adjustments(db)
    assert current.status == STATUS_KEPT_OLD
    assert await _rows(db, BookingHold) == []
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.status) == ("Consulta", AppointmentStatus.SCHEDULED)
    upcoming, _nearest = await _patient_sees(db, world)
    assert [item["id"] for item in upcoming] == [str(world.appointment.id)]  # visible again
    assert sent()[-1][2] == service_change.kept_old_notice_text("Consulta")


async def test_a_before_change_resent_and_paid_moves_the_visit_to_the_new_doctor(
    client: AsyncClient, db, acting, charges, monkeypatch, engine_messages
):
    world, adjustment, bruno, new_start = await _suspended_by_the_patient(
        db, acting, monkeypatch, client
    )
    # UA2: the sweep takes the old event off the clinic's agenda (`retry_vacates`, standing in
    # for the post-commit vacate of the patient's path) before the Pix expires.
    clinic_agenda, bruno_agenda = _wire_agendas(monkeypatch, world, bruno)
    await _expired(db, world, adjustment, engine_messages)
    assert clinic_agenda.cancelled == ["evt-1"]

    await _tap(world, "svcdiffresend", adjustment.id)

    old, resent = await _adjustments(db)
    assert (old.status, resent.status, resent.mode) == (
        STATUS_EXPIRED,
        STATUS_AWAITING_PAYMENT,
        MODE_SUSPEND,
    )
    assert resent.old_slot == OLD_SLOT_RELEASED  # UA2: the new change owns the vacated slot
    assert resent.provider_charge_id == "pay_diff_2"
    assert sent()[-1][2].rstrip().endswith("```000201second```")
    [hold] = await _rows(db, BookingHold)
    assert hold.adjustment_id == resent.id  # the new window stayed reserved

    result = await _webhook_paid(
        db, world, charges, resent.id, "pay_diff_2", at=datetime.now(UTC) + timedelta(minutes=5)
    )

    assert result.outcome == "adjustment_applied"
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.professional_id) == ("Avaliação", bruno)
    assert _utc(row.start_at) == _utc(new_start)
    # UA2 (D-R10): a NEW event on the new doctor's agenda; the old one left at the suspension.
    assert row.google_event_id == old_slot.paid_event_id(resent.id, calendar=bruno_agenda)
    assert len(bruno_agenda.created) == 1 and clinic_agenda.cancelled == ["evt-1"]
    async with db() as session:
        assert (await session.get(DepositAdjustment, resent.id)).old_slot == OLD_SLOT_REPLACED
    assert await _rows(db, BookingHold) == []
    assert not await _suspended(db, world)
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
    assert deposit.reschedule_count == 1  # the patient's own move counts
    upcoming, _nearest = await _patient_sees(db, world)
    assert [item["id"] for item in upcoming] == [str(world.appointment.id)]
    assert await _rows(db, PaymentAttentionItem) == []


async def test_a_before_change_cancel_both_cancels_the_old_visit(
    client: AsyncClient, db, acting, charges, monkeypatch, engine_messages
):
    world, adjustment, _bruno, _new = await _suspended_by_the_patient(
        db, acting, monkeypatch, client
    )
    await _expired(db, world, adjustment, engine_messages)

    await _tap(world, "svcdiffboth", adjustment.id)

    [current] = await _adjustments(db)
    assert current.status == STATUS_CANCELLED
    assert await _rows(db, BookingHold) == []
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.CANCELLED
    assert "Consulta cancelada." in sent()[-1][2]


async def test_a_before_change_without_an_answer_keeps_the_old_visit(
    client: AsyncClient, db, acting, charges, monkeypatch, engine_messages
):
    world, adjustment, bruno, _new = await _suspended_by_the_patient(
        db, acting, monkeypatch, client
    )
    clinic_agenda, _doctor_agenda = _wire_agendas(monkeypatch, world, bruno)
    current = await _expired(db, world, adjustment, engine_messages)
    assert clinic_agenda.cancelled == ["evt-1"]  # UA2: the old event left (the sweep's retry)
    engine_messages.clear()

    await _sweep(db, _utc(current.expires_at) + timedelta(minutes=1))

    [kept] = await _adjustments(db)
    assert kept.status == STATUS_KEPT_OLD
    assert kept.old_slot == OLD_SLOT_RESTORED
    assert await _rows(db, BookingHold) == []
    [[notice]] = engine_messages
    assert notice.body == service_change.kept_old_notice_text("Consulta")
    row = await reload_appointment(db, world.appointment.id)
    assert (row.appointment_type, row.status) == ("Consulta", AppointmentStatus.SCHEDULED)
    # UA2 (D-R8/D-R11): the old visit came back under a NEW event id, on its old slot.
    restored = old_slot.restore_event_id(kept.id, calendar=clinic_agenda)
    assert row.google_event_id == restored
    assert [event["id"] for event in clinic_agenda.created] == [restored]
    assert not await _suspended(db, world)


# --------------------------------------------------------------------------------------------
# (b) "after": applied now + Pix; unpaid -> the clinic collects
# --------------------------------------------------------------------------------------------


async def test_an_after_change_stands_now_and_an_unpaid_pix_becomes_the_clinics_item(
    client: AsyncClient, db, acting, charges, monkeypatch, engine_messages
):
    world = await _clinic(db, acting, timing="after")

    await _patient_confirms(world, _patient_edit(world))

    row = await reload_appointment(db, world.appointment.id)
    assert row.appointment_type == "Avaliação"  # written at once (D-R5)
    [adjustment] = await _adjustments(db)
    assert (adjustment.mode, adjustment.status) == (MODE_APPLIED, STATUS_AWAITING_PAYMENT)
    assert any(text[2].rstrip().endswith("```000201first```") for text in sent())
    assert not await _suspended(db, world)
    event = await _events(client, world, monkeypatch)
    assert event["pending_change_summary"] == "Consulta → Avaliação"

    await _sweep(db, _utc(adjustment.expires_at) + timedelta(minutes=1))

    [current] = await _adjustments(db)
    assert current.status == STATUS_COLLECT_DUE
    [item] = await _rows(db, PaymentAttentionItem)
    assert (item.reason, item.amount_cents) == (REASON_SERVICE_DIFF_UNPAID, 5000)
    [[notice]] = engine_messages
    assert notice.body == service_change.unpaid_collect_text("Avaliação", 5000)
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"
    assert charges.called("cancel_charge") == 1  # the expired Pix is voided at the provider


# --------------------------------------------------------------------------------------------
# (c) an UNPAID sinal: the old Pix voided, a new one with the new amount
# --------------------------------------------------------------------------------------------


async def _unpaid_sinal(db, acting):
    world = await _clinic(db, acting, timing="after")
    async with db() as session:
        await session.execute(
            update(PixDeposit).values(
                status=PixDepositStatus.AWAITING,
                amount_cents=6000,
                provider="asaas",
                provider_charge_id="pay_old_sinal",
                paid_amount_cents=None,
            )
        )
        await session.commit()
    return world


async def _assert_requoted(db, world, provider) -> None:
    assert ("cancel_charge", "pay_old_sinal") in provider.calls
    async with db() as session:
        deposits = list((await session.scalars(select(PixDeposit))).all())
    old = next(d for d in deposits if d.provider_charge_id == "pay_old_sinal")
    new = next(d for d in deposits if d.provider_charge_id != "pay_old_sinal")
    assert (old.status, old.appointment_id) == (PixDepositStatus.EXPIRED, None)
    assert (new.status, new.appointment_id) == (PixDepositStatus.AWAITING, world.appointment.id)
    assert new.amount_cents == 15000  # 30 % of R$ 500,00
    assert any("000201first" in item[2] for item in sent())
    assert await _adjustments(db) == []  # never a "difference": the sinal itself was re-quoted


async def test_a_patient_change_on_an_unpaid_sinal_replaces_its_pix(
    client: AsyncClient, db, acting, charges, monkeypatch
):
    world = await _unpaid_sinal(db, acting)

    await _patient_confirms(world, _patient_edit(world))

    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"
    await _assert_requoted(db, world, charges)


async def test_a_staff_change_on_an_unpaid_sinal_replaces_its_pix(
    client: AsyncClient, db, acting, charges
):
    world = await _unpaid_sinal(db, acting)

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 200, response.text
    await _assert_requoted(db, world, charges)


# --------------------------------------------------------------------------------------------
# (d) the clinic on a visit the PATIENT suspended (D-R9)
# --------------------------------------------------------------------------------------------


async def test_the_clinic_can_only_release_a_visit_the_patient_suspended(
    client: AsyncClient, db, acting, charges, monkeypatch
):
    world, adjustment, _bruno, _new = await _suspended_by_the_patient(
        db, acting, monkeypatch, client
    )
    new = world.start_at + timedelta(days=2)
    for response in (
        await _post(client, world.appointment.id, service="Retorno"),
        await client.post(
            f"{CALENDAR}/appointments/{world.appointment.id}/reschedule",
            json={"new_start": _iso(new), "new_end": _iso(new + timedelta(minutes=30))},
        ),
        await client.post(
            f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True}
        ),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == SUSPENDED_409
    assert charges.called("cancel_charge") == 0

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/release",
        json={"acknowledge_retention": True},
    )

    assert response.status_code == 200, response.text
    [current] = await _adjustments(db)
    assert current.status == STATUS_CANCELLED
    assert ("cancel_charge", "pay_diff_1") in charges.calls  # the difference Pix is voided
    assert await _rows(db, BookingHold) == []  # the new window is free again
    assert not await _suspended(db, world)
    assert (await reload_appointment(db, world.appointment.id)).status == (
        AppointmentStatus.CANCELLED
    )


# --------------------------------------------------------------------------------------------
# (e) a clinic without sinal: nothing of the money changes
# --------------------------------------------------------------------------------------------


async def test_a_clinic_without_sinal_changes_nothing_of_the_money(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    world = await _clinic(db, acting, timing="before", paid=False)
    recharged: list = []

    async def _recharge(**kwargs):
        recharged.append(kwargs)

    monkeypatch.setattr(deposit_lifecycle, "recharge_after_edit", _recharge)

    await _patient_confirms(world, _patient_edit(world))
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"

    response = await _post(client, world.appointment.id, service="Retorno")
    assert response.status_code == 200, response.text
    assert response.json().get("service_change") is None
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Retorno"

    assert provider.calls == [] and recharged == []
    assert await _adjustments(db) == []
    assert await _rows(db, PixDeposit) == []
    assert await _rows(db, BookingHold) == []
    assert await _rows(db, PaymentAttentionItem) == []
    assert not await _suspended(db, world)
