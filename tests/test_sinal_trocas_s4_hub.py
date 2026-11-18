"""TASK-085 R / S4 - the hub's agenda routes and the sinal (api/hub/calendar.py).

Contract: tasks/TASK-085/sinal-trocas/contrato.md §3.9. The paid world of
tests/test_hub_edit_appointment.py (R$ 100,00 at 30 % on Consulta; Avaliação R$ 50,00 dearer). What
is pinned here:

* D-R10 (3rd round, replaces D-R9): a SUSPENDED visit (a "before" clinic, a dearer change waiting
  for the Pix) left the agenda - it answers 409 `appointment_suspended` to edit, reschedule, cancel,
  "Liberar horário" and every status change (falta, compareceu, confirmar, cancelar) - nothing
  touched (the `old_slot_lost` exception is pinned in tests/test_sinal_trocas_uh_hub.py);
* the edit's answer and the patient's ONE money message for `suspended`; the OLD event is deleted
  right after the commit (`old_slot.vacate_old_slot`);
* `GET /awaiting-payment` lists the held NEW window of a suspended change (`kind: "difference"`,
  `appointment_id`, `change_summary`); `GET /events` says `suspended` /
  `deposit.pending_difference_mode` while the old event is still there;
* D-R6 / OQ-10: the new sinal is sent after the commit (`recharge_after_edit`), never before.
"""

# ruff: noqa: F811

from datetime import datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub import calendar as hub_calendar
from secretaria.models import AppointmentStatus, BookingHold, PixDeposit
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.models.pix_deposit import PixDepositStatus
from secretaria.services.payments import (
    adjustment_suspension,
    deposit_lifecycle,
    old_slot,
    service_change,
)
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeArqPool,
    FakeCalendar,
    acting,
    install_pool,
    patch_status,
    r7,
    sent,
    set_tenant,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import WA_ID, reload_appointment
from tests.test_hub_edit_appointment import (  # noqa: F401
    _iso,
    _paid_world,
    _post,
    provider,
)

SUSPENDED_MESSAGE = (
    "Essa consulta saiu da agenda enquanto o paciente paga a diferença de uma troca; não há o "
    "que fazer agora."
)


@pytest.fixture(autouse=True)
def _old_slot_reaches_the_test_agenda(monkeypatch, db):
    """TASK-085 R D-R10: `old_slot.vacate_old_slot` (run by the edit route after the commit) opens
    its own session on the test DB and finds the consultation's agenda where the hub does."""
    monkeypatch.setattr(old_slot, "async_session_factory", db)

    async def _calendar(session, tenant, appointment):
        pid = getattr(appointment, "professional_id", None)
        return FakeCalendar.get(str(pid) if pid else "tenant")

    monkeypatch.setattr(service_change, "calendar_for_appointment", _calendar)


async def _adjustments(db) -> list[DepositAdjustment]:
    async with db() as session:
        return list(await session.scalars(select(DepositAdjustment)))


async def _holds(db) -> list[BookingHold]:
    async with db() as session:
        return list(await session.scalars(select(BookingHold)))


async def _suspended_world(db, acting, client, **kwargs):
    """A "before" clinic whose staff asked for a dearer service: the visit is suspended."""
    world = await _paid_world(db, acting, **kwargs)
    await set_tenant(db, world.tenant.id, deposit_timing="before")
    response = await _post(client, world.appointment.id, service="Avaliação")
    assert response.status_code == 200, response.text
    assert response.json()["service_change"]["status"] == "suspended"
    FakeCalendar.reset()
    return world


def _assert_suspended_409(response) -> None:
    assert response.status_code == 409, response.text
    assert response.json()["detail"] == {
        "code": "appointment_suspended",
        "message": SUSPENDED_MESSAGE,
    }


# --------------------------------------------------------------------------
# the edit's answer for a suspended change
# --------------------------------------------------------------------------


async def test_a_before_dearer_edit_answers_suspended_and_sends_one_pix_message(
    client: AsyncClient, db, acting, provider
):
    pool = FakeArqPool()
    install_pool(pool)
    world = await _paid_world(db, acting)
    await set_tenant(db, world.tenant.id, deposit_timing="before")

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["appointment_type"] == "Consulta"  # nothing changed yet
    [adjustment] = await _adjustments(db)
    change = body["service_change"]
    assert change == {
        "status": "suspended",
        "diff_cents": 5000,
        "old_service": "Consulta",
        "new_service": "Avaliação",
        "adjustment_id": str(adjustment.id),
        "expires_at": change["expires_at"],
        "patient_notice": "whatsapp_sent",
    }
    assert "000201pix" not in response.text
    [message] = sent()  # ONE message, no "A clínica alterou" card (nothing changed now)
    assert message[2] == service_change.difference_pix_text(
        "Avaliação", 5000, "000201pix", 30, source="hub", held_new_slot=True
    )
    assert message[2].rstrip().endswith("```000201pix```")
    assert pool.calls == []  # no doctor e-mail: nothing of his agenda changed
    assert FakeCalendar.get("tenant").updated == []
    # D-R10: after the commit the OLD event leaves the agenda (its slot is free for others).
    assert FakeCalendar.get("tenant").cancelled == ["evt-1"]
    assert adjustment.old_slot == "released"


# --------------------------------------------------------------------------
# D-R10 (replaces D-R9): nothing for the clinic to do on a suspended visit
# --------------------------------------------------------------------------


async def test_a_suspended_visit_refuses_edit_reschedule_cancel_and_release(
    client: AsyncClient, db, acting, provider
):
    world = await _suspended_world(db, acting, client)
    sent_before = list(sent())
    new = world.start_at + timedelta(days=1)

    _assert_suspended_409(await _post(client, world.appointment.id, phone="5511999990000"))
    _assert_suspended_409(
        await client.post(
            f"{CALENDAR}/appointments/{world.appointment.id}/reschedule",
            json={"new_start": _iso(new), "new_end": _iso(new + timedelta(minutes=30))},
        )
    )
    _assert_suspended_409(
        await client.post(
            f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True}
        )
    )
    _assert_suspended_409(
        await client.post(
            f"{CALENDAR}/appointments/{world.appointment.id}/release",
            json={"acknowledge_retention": True},
        )
    )

    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED and row.appointment_type == "Consulta"
    assert FakeCalendar.instances == {} or all(
        not (cal.updated or cal.cancelled or cal.created) for cal in FakeCalendar.instances.values()
    )
    assert sent() == sent_before
    [adjustment] = await _adjustments(db)
    assert adjustment.status == "awaiting_payment"
    assert provider.called("cancel_charge") == 0


@pytest.mark.parametrize("status_value", ["attended", "no_show", "cancelled", "confirmed"])
async def test_a_suspended_visit_refuses_every_status_change(
    client: AsyncClient, db, acting, provider, status_value
):
    world = await _suspended_world(db, acting, client)

    _assert_suspended_409(await patch_status(client, world.appointment.id, status_value))

    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED and row.confirmation_count == 0
    [adjustment] = await _adjustments(db)
    assert adjustment.status == "awaiting_payment"


async def test_liberar_horario_no_longer_ends_a_suspended_change(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    """D-R10 (replaces D-R9's "Liberar horário settles it"): the visit is out of the agenda - there
    is nothing to free; the change, its Pix and its held window stay as they were."""

    async def no_secretary(tenant_id):
        return None

    monkeypatch.setattr(hub_calendar.brain_staff, "fetch_has_secretary", no_secretary)
    world = await _suspended_world(db, acting, client)

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/release",
        json={"acknowledge_retention": True},
    )

    _assert_suspended_409(response)
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED
    assert FakeCalendar.instances == {} or all(
        not cal.cancelled for cal in FakeCalendar.instances.values()
    )
    [adjustment] = await _adjustments(db)
    assert adjustment.status == "awaiting_payment"
    assert provider.called("cancel_charge") == 0
    assert len(await _holds(db)) == 1  # the new window stays held
    async with db() as session:
        assert await adjustment_suspension.is_suspended(
            session, tenant_id=world.tenant.id, appointment_id=world.appointment.id
        )


async def test_an_applied_change_waiting_for_its_pix_is_not_suspended(
    client: AsyncClient, db, acting, provider
):
    """D-R5 (an "after" clinic): the change already stands - the clinic acts on it as on any
    visit (here, a time move)."""
    world = await _paid_world(db, acting)
    edited = await _post(client, world.appointment.id, service="Avaliação")
    assert edited.json()["service_change"]["status"] == "applied_awaiting_payment"

    new = world.start_at + timedelta(days=1)
    response = await _post(client, world.appointment.id, start_at=_iso(new))

    assert response.status_code == 200, response.text
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"


# --------------------------------------------------------------------------
# what the agenda reads
# --------------------------------------------------------------------------


def _window(world) -> dict:
    return {
        "start": (world.start_at - timedelta(days=1)).isoformat(),
        "end": (world.start_at + timedelta(days=3)).isoformat(),
    }


async def test_awaiting_payment_lists_the_held_window_of_a_suspended_change(
    client: AsyncClient, db, acting, provider
):
    world = await _suspended_world(db, acting, client)

    body = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    [row] = body
    [hold] = await _holds(db)
    assert row["id"] == str(hold.id)
    assert (row["kind"], row["appointment_id"]) == ("difference", str(world.appointment.id))
    assert row["service"] == "Avaliação"
    assert row["change_summary"] == "Consulta → Avaliação"  # mapa-u §1.6
    start = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
    assert start.replace(tzinfo=None) == world.start_at.replace(tzinfo=None)  # SQLite: naive
    text = str(body)
    assert "000201pix" not in text and "pay_diff" not in text and "Maria" not in text


async def test_awaiting_payment_drops_the_window_once_the_change_is_closed(
    client: AsyncClient, db, acting, provider
):
    world = await _suspended_world(db, acting, client)
    async with db() as session:
        adjustment = (await session.scalars(select(DepositAdjustment))).one()
        adjustment.status = "kept_old"  # closed, its hold not yet swept
        await session.commit()

    body = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert body == []


async def _events(client, world, monkeypatch):
    calendar = FakeCalendar.get("tenant")

    async def listing(start, end):
        return [
            {
                "id": "evt-1",
                "summary": "Consulta - Maria",
                "start": world.start_at.isoformat(),
                "end": (world.start_at + timedelta(minutes=30)).isoformat(),
            }
        ]

    monkeypatch.setattr(calendar, "check_availability", listing)
    response = await client.get(f"{CALENDAR}/events", params=_window(world))
    assert response.status_code == 200, response.text
    [event] = response.json()
    return event


async def test_the_agenda_says_a_visit_is_suspended(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    """While its old event is still on the agenda (before the vacate's delete lands, or a delete
    that failed and waits for the retry) the visit says it is suspended."""
    world = await _suspended_world(db, acting, client)

    event = await _events(client, world, monkeypatch)

    assert event["suspended"] is True
    assert event["deposit"]["pending_difference_mode"] == "suspend"
    assert event["deposit"]["pending_difference_cents"] == 5000


async def test_the_agenda_says_an_applied_change_waits_for_its_pix_without_suspending(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    world = await _paid_world(db, acting)
    await _post(client, world.appointment.id, service="Avaliação")
    FakeCalendar.reset()

    event = await _events(client, world, monkeypatch)

    assert event["suspended"] is False
    assert event["deposit"]["pending_difference_mode"] == "applied"


async def test_a_plain_visit_is_neither(client: AsyncClient, db, acting, provider, monkeypatch):
    world = await _paid_world(db, acting)

    event = await _events(client, world, monkeypatch)

    assert event["suspended"] is False and event["deposit"]["pending_difference_mode"] is None


# --------------------------------------------------------------------------
# D-R6 / OQ-10: the new sinal after the commit
# --------------------------------------------------------------------------


def _record_recharge(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def recharge(*, tenant_id, appointment_id, old_amount_cents, waba_token=None, redis=None):
        calls.append((tenant_id, appointment_id, old_amount_cents, len(sent())))
        return None

    monkeypatch.setattr(deposit_lifecycle, "recharge_after_edit", recharge)
    return calls


async def test_an_unpaid_sinal_requoted_sends_the_new_one_after_the_card(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    calls = _record_recharge(monkeypatch)
    world = await _paid_world(db, acting)
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
        deposit.status = PixDepositStatus.AWAITING
        deposit.amount_cents = 6000
        deposit.provider = "asaas"
        deposit.provider_charge_id = "pay_old"
        deposit.deposit_timing = "after"
        await session.commit()

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 200, response.text
    assert response.json()["service_change"] is None
    [(tenant_id, appointment_id, old_cents, sent_at_call)] = calls
    assert (tenant_id, appointment_id, old_cents) == (world.tenant.id, world.appointment.id, 6000)
    assert sent_at_call == 1  # "A clínica alterou" went first
    assert sent()[0][2].startswith("A clínica alterou")


async def test_an_unpaid_sinal_being_paid_refuses_the_edit_with_its_code(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    from secretaria.services.payments.provider import ChargeState, PaymentStatus

    calls = _record_recharge(monkeypatch)
    provider.state = ChargeState(status=PaymentStatus.PAID)
    world = await _paid_world(db, acting)
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
        deposit.status = PixDepositStatus.AWAITING
        deposit.amount_cents = 6000
        deposit.provider = "asaas"
        deposit.provider_charge_id = "pay_old"
        await session.commit()

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert (detail["code"], detail["reason"]) == ("sinal_requote_blocked", "payment_in_flight")
    assert calls == [] and sent() == []
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Consulta"


async def test_a_patient_booking_without_a_sinal_is_charged_after_a_money_edit(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    calls = _record_recharge(monkeypatch)
    world = await _paid_world(db, acting)
    async with db() as session:
        await session.delete(await session.scalar(select(PixDeposit)))
        await session.commit()

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 200, response.text
    [(_tenant, _appointment, old_cents, _sent)] = calls
    assert old_cents is None


async def test_no_recharge_without_a_money_change(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    calls = _record_recharge(monkeypatch)
    world = await _paid_world(db, acting)

    response = await _post(
        client, world.appointment.id, start_at=_iso(world.start_at + timedelta(days=1))
    )

    assert response.status_code == 200 and calls == []
    assert sent()[0][1] == WA_ID




async def test_a_failing_notice_never_skips_the_new_sinal(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    """Fix round 1: the old Pix was voided at the commit; a notice that raises afterwards must
    not leave the patient without the new one (`recharge_after_edit` runs in a `finally`)."""
    from secretaria.services import clinic_action_notice

    calls = _record_recharge(monkeypatch)

    async def broken(*args, **kwargs):
        raise RuntimeError("notice down")

    monkeypatch.setattr(clinic_action_notice, "notify_staff_edit", broken)
    world = await _paid_world(db, acting)
    async with db() as session:
        deposit = await session.scalar(select(PixDeposit))
        deposit.status = PixDepositStatus.AWAITING
        deposit.amount_cents = 6000
        deposit.provider = "asaas"
        deposit.provider_charge_id = "pay_old"
        deposit.deposit_timing = "after"
        await session.commit()

    with pytest.raises(RuntimeError):
        await _post(client, world.appointment.id, service="Avaliação")

    [(_tenant, appointment_id, old_cents, _sent)] = calls
    assert (appointment_id, old_cents) == (world.appointment.id, 6000)
    assert (await reload_appointment(db, world.appointment.id)).appointment_type == "Avaliação"
