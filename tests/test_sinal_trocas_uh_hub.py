"""TASK-085 R / UH - the hub after D-R10/D-R11/D-R14 (mapa-u §1.6, §4.5, §4.6).

The paid world of tests/test_hub_edit_appointment.py (R$ 100,00 at 30 % on Consulta; Avaliação
R$ 50,00 dearer), a "before" clinic whose staff asked the dearer change: the visit is SUSPENDED.
What is pinned here:

* characterization: a visit without a change, and a legacy change (`mode` NULL), behave as today;
* D-R10: right after the commit of a `suspended` edit the OLD event leaves the agenda
  (`old_slot.vacate_old_slot`; `old_slot` -> released); every mutation of a suspended visit,
  "Liberar horário" included, is 409 with the new text; nothing touched;
* D-R11 (`old_slot_lost`): POST /reschedule books the same row on a new slot - in the order of the
  UA1 contract (`ensure_old_event_gone` -> window -> `difhub` event -> `finish_rebook` in one
  transaction, compensated on a loss) - and POST /cancel cancels it; edit/status/release still 409
  with a text that says what to do;
* `GET /awaiting-payment`: `change_summary` on "difference", the new "rebook" kind;
* `GET /events`: the `price_change` notice row is never shown as a reminder (D-R14).

Engine functions still being written by UA2 are not called here: the `old_slot_lost` state is
reached through UA1's own `old_slot.mark_old_slot_lost` (the engine's silence path will call it).
"""

# ruff: noqa: F811

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy import select

from secretaria.api.hub import calendar as hub_calendar
from secretaria.models import (
    AppointmentReminder,
    AppointmentStatus,
    PaymentAttentionItem,
    PixDeposit,
    Professional,
    Tenant,
)
from secretaria.models.deposit_adjustment import DepositAdjustment
from secretaria.services import reminder_hooks
from secretaria.services.agenda_visibility import AgendaViewer
from secretaria.services.calendar import GoogleTokenRevokedError
from secretaria.services.payments import old_slot, service_change
from tests._agenda_viewer import view_as
from tests._r7_support import (  # noqa: F401
    CALENDAR,
    FakeArqPool,
    FakeCalendar,
    acting,
    install_pool,
    patch_status,
    r7,
    sent,
    set_appointment,
    set_tenant,
)
from tests._reminder_fixtures import db  # noqa: F401
from tests._reminders_v2 import reload_appointment
from tests.test_hub_edit_appointment import (  # noqa: F401
    _iso,
    _paid_world,
    _post,
    provider,
)
from tests.test_sinal_trocas_s4_hub import _events, _suspended_world, _window

SUSPENDED = {
    "code": "appointment_suspended",
    "message": (
        "Essa consulta saiu da agenda enquanto o paciente paga a diferença de uma troca; não há "
        "o que fazer agora."
    ),
}
REBOOK_DUE = {
    "code": "appointment_suspended",
    "message": (
        "O horário antigo desta consulta foi ocupado enquanto o paciente pagava a diferença de "
        "uma troca: remarque com o paciente ou cancele a consulta."
    ),
}


# --------------------------------------------------------------------------
# wiring: the old_slot module reaches the in-memory DB and the hub's agenda double
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def agenda(monkeypatch, db) -> list[tuple]:
    """`old_slot` opens its own sessions (vacate, discard) on the test DB and finds the
    consultation's agenda where the hub finds it (`FakeCalendar`, one per label). The double
    learns idempotent ids (`event_id=`), `get_event`, and canned answers per id. Returns every
    agenda write, in order: (label, "create"|"cancel", event id)."""
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
        self._gate()
        self.created.append((start, end, summary, description))
        eid = event_id or f"evt-new-{self.label}-{len(self.created)}"
        calls.append((self.label, "create", eid))
        answer = getattr(self, "answers", {}).get(eid)
        if answer is not None:
            return answer
        return {"id": eid, "htmlLink": f"https://calendar.example/{eid}"}

    async def cancel_event(self, event_id):
        self._gate()
        if getattr(self, "cancel_fails", False):
            raise RuntimeError("google down")
        self.cancelled.append(event_id)
        calls.append((self.label, "cancel", event_id))

    async def get_event(self, event_id):
        return None

    monkeypatch.setattr(FakeCalendar, "create_event", create_event)
    monkeypatch.setattr(FakeCalendar, "cancel_event", cancel_event)
    monkeypatch.setattr(FakeCalendar, "get_event", get_event, raising=False)
    return calls


@pytest.fixture
def replans(monkeypatch) -> list:
    moved: list = []

    async def _replan(appointment_id, **kw):
        moved.append(appointment_id)
        return 0

    monkeypatch.setattr(reminder_hooks, "after_appointment_rescheduled", _replan)
    return moved


async def _adjustment(db) -> DepositAdjustment:
    async with db() as session:
        return (await session.scalars(select(DepositAdjustment))).one()


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list(await session.scalars(select(PaymentAttentionItem)))


async def _deposit(db) -> PixDeposit:
    async with db() as session:
        return (await session.scalars(select(PixDeposit))).one()


async def _set_status(db, status_value: str) -> None:
    async with db() as session:
        row = (await session.scalars(select(DepositAdjustment))).one()
        row.status = status_value
        await session.commit()


async def _lost_world(db, acting, client, **kwargs):
    """The suspended visit whose Pix expired and whose old slot was taken meanwhile: UA1's
    `mark_old_slot_lost` (what the engine's silence path calls) -> `old_slot_lost`, the new hold
    released, the clinic's `service_diff_rebook_due` item open."""
    world = await _suspended_world(db, acting, client, **kwargs)
    async with db() as session:
        adjustment = (await session.scalars(select(DepositAdjustment))).one()
        assert adjustment.old_slot == "released"  # the edit's vacate already ran
        adjustment.status = "awaiting_choice"
        await session.commit()
        tenant = await session.get(Tenant, world.tenant.id)
        assert await old_slot.mark_old_slot_lost(
            session, tenant=tenant, adjustment=adjustment, now=datetime.now(UTC)
        )
        await session.commit()
    FakeCalendar.reset()
    return world


def _reschedule_body(world, *, days: int = 2, minutes: int = 30) -> dict:
    new = world.start_at + timedelta(days=days)
    return {"new_start": _iso(new), "new_end": _iso(new + timedelta(minutes=minutes))}


async def _reschedule(client, world, body: dict):
    return await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/reschedule", json=body
    )


def _naive(value):
    return value.replace(tzinfo=None) if value is not None else None


# --------------------------------------------------------------------------
# characterization: no change / a legacy change
# --------------------------------------------------------------------------


async def test_a_visit_without_a_change_is_rescheduled_as_today(
    client: AsyncClient, db, acting, provider, replans
):
    world = await _paid_world(db, acting)
    body = _reschedule_body(world)

    response = await _reschedule(client, world, body)

    assert response.status_code == 200, response.text
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.RESCHEDULED and row.google_event_id == "evt-1"
    assert FakeCalendar.get("tenant").created == []  # moved, never re-created


async def test_a_legacy_change_without_a_mode_never_suspends(
    client: AsyncClient, db, acting, provider, replans, agenda
):
    """A legacy `awaiting_payment` (mode NULL, before TASK-085 R) is not a suspension: the hub
    acts on the visit as today and lists nothing for it."""
    world = await _paid_world(db, acting)
    async with db() as session:
        deposit = (await session.scalars(select(PixDeposit))).one()
        session.add(
            DepositAdjustment(
                id=uuid4(),
                tenant_id=world.tenant.id,
                deposit_id=deposit.id,
                appointment_id=world.appointment.id,
                kind="increase",
                status="awaiting_payment",
                old_service="Consulta",
                new_service="Avaliação",
                diff_cents=5000,
                expires_at=datetime.now(UTC) + timedelta(minutes=20),
            )
        )
        await session.commit()

    listed = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()
    response = await _reschedule(client, world, _reschedule_body(world))

    assert listed == []
    assert response.status_code == 200, response.text
    assert [c for c in agenda if c[1] == "create"] == []


# --------------------------------------------------------------------------
# D-R10: the old event leaves the agenda after a suspended edit
# --------------------------------------------------------------------------


async def test_a_suspended_edit_takes_the_old_event_off_the_agenda_after_the_commit(
    client: AsyncClient, db, acting, provider, agenda, monkeypatch
):
    world = await _paid_world(db, acting)
    await set_tenant(db, world.tenant.id, deposit_timing="before")
    seen: list = []
    real_vacate = old_slot.vacate_old_slot

    async def _vacate(tenant_id, adjustment_id):
        # After the commit: the change is already there for the vacate's own session.
        async with db() as session:
            seen.append(await session.get(DepositAdjustment, adjustment_id))
        return await real_vacate(tenant_id, adjustment_id)

    monkeypatch.setattr(old_slot, "vacate_old_slot", _vacate)

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 200, response.text
    assert response.json()["service_change"]["status"] == "suspended"
    assert seen and seen[0] is not None and seen[0].status == "awaiting_payment"
    assert ("tenant", "cancel", "evt-1") in agenda
    adjustment = await _adjustment(db)
    assert adjustment.old_slot == "released"
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-1"  # the row is never written during the suspension
    assert row.appointment_type == "Consulta"


@pytest.mark.parametrize("timing", ["after", "before"])
async def test_an_edit_that_does_not_suspend_never_vacates(
    client: AsyncClient, db, acting, provider, monkeypatch, timing
):
    """"after" + dearer = applied now (D-R5); "before" + a time-only move = no money change:
    neither suspends, so nothing leaves the agenda."""
    world = await _paid_world(db, acting)
    await set_tenant(db, world.tenant.id, deposit_timing=timing)
    calls: list = []

    async def _vacate(tenant_id, adjustment_id):
        calls.append(adjustment_id)
        return "not_applicable"

    monkeypatch.setattr(old_slot, "vacate_old_slot", _vacate)
    if timing == "after":
        response = await _post(client, world.appointment.id, service="Avaliação")
    else:
        new = world.start_at + timedelta(days=1)
        response = await _post(client, world.appointment.id, start_at=_iso(new))

    assert response.status_code == 200, response.text
    assert calls == []


async def test_a_failing_vacate_never_fails_the_edit(
    client: AsyncClient, db, acting, provider
):
    """The agenda refuses the delete: the edit stands, the change stays `releasing` for the
    payments sweep's retry (`retry_vacates`), the patient got the Pix message."""
    world = await _paid_world(db, acting)
    await set_tenant(db, world.tenant.id, deposit_timing="before")
    FakeCalendar.get("tenant").cancel_fails = True  # only the delete fails

    response = await _post(client, world.appointment.id, service="Avaliação")

    assert response.status_code == 200, response.text
    assert response.json()["service_change"]["status"] == "suspended"
    assert (await _adjustment(db)).old_slot == "releasing"
    assert len(sent()) == 1


# --------------------------------------------------------------------------
# D-R10: nothing for the clinic to do on a suspended visit (409, "Liberar" included)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status_value", ["awaiting_payment", "awaiting_choice", "restoring"]
)
async def test_every_mutation_of_a_suspended_visit_is_refused(
    client: AsyncClient, db, acting, provider, agenda, status_value
):
    world = await _suspended_world(db, acting, client)
    await _set_status(db, status_value)
    agenda.clear()
    sent_before = list(sent())
    new = world.start_at + timedelta(days=1)

    responses = [
        await _post(client, world.appointment.id, phone="5511999990000"),
        await _reschedule(client, world, _reschedule_body(world)),
        await client.post(
            f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True}
        ),
        await client.post(
            f"{CALENDAR}/appointments/{world.appointment.id}/release",
            json={"acknowledge_retention": True},
        ),
        await patch_status(client, world.appointment.id, "attended"),
    ]

    for response in responses:
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == SUSPENDED
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.SCHEDULED and row.start_at != new
    assert agenda == [] and sent() == sent_before
    assert (await _adjustment(db)).status == status_value
    assert provider.called("cancel_charge") == 0


async def test_liberar_horario_no_longer_closes_a_suspended_change(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    """D-R9 is gone (D-R10): the change and its held window stay as they were."""

    async def no_secretary(tenant_id):
        raise AssertionError("never asked: the release is refused before")

    monkeypatch.setattr(hub_calendar.brain_staff, "fetch_has_secretary", no_secretary)
    world = await _suspended_world(db, acting, client)

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/release",
        json={"acknowledge_retention": True},
    )

    assert response.status_code == 409 and response.json()["detail"] == SUSPENDED
    adjustment = await _adjustment(db)
    assert adjustment.status == "awaiting_payment"
    async with db() as session:
        from secretaria.models import BookingHold

        assert len(list(await session.scalars(select(BookingHold)))) == 1


# --------------------------------------------------------------------------
# D-R11: old_slot_lost - edit / status / release still refused, with what to do
# --------------------------------------------------------------------------


async def test_a_visit_whose_old_slot_was_taken_refuses_edit_status_and_release(
    client: AsyncClient, db, acting, provider, agenda
):
    world = await _lost_world(db, acting, client)
    agenda.clear()

    for response in (
        await _post(client, world.appointment.id, phone="5511999990000"),
        await patch_status(client, world.appointment.id, "no_show"),
        await client.post(
            f"{CALENDAR}/appointments/{world.appointment.id}/release",
            json={"acknowledge_retention": True},
        ),
    ):
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == REBOOK_DUE
    assert agenda == []
    assert (await _adjustment(db)).status == "old_slot_lost"


# --------------------------------------------------------------------------
# D-R11: POST /reschedule of an old_slot_lost visit
# --------------------------------------------------------------------------


async def test_the_clinic_books_a_visit_whose_old_slot_was_taken_on_a_new_slot(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    before = await _deposit(db)
    adjustment = await _adjustment(db)
    agenda.clear()
    body = _reschedule_body(world)

    response = await _reschedule(client, world, body)

    assert response.status_code == 200, response.text
    expected_id = f"difhub{adjustment.id.hex}"
    assert agenda == [("tenant", "create", expected_id)]  # nothing to move: created anew
    [created] = FakeCalendar.get("tenant").created
    assert created[2] == "Consulta - Maria"
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.RESCHEDULED
    assert _naive(row.start_at) == _naive(datetime.fromisoformat(body["new_start"]))
    assert row.google_event_id == expected_id
    assert row.google_event_link == f"https://calendar.example/{expected_id}"
    assert row.appointment_type == "Consulta"  # same service, doctor, unit (D-R11)
    adjustment = await _adjustment(db)
    assert (adjustment.status, adjustment.old_slot) == ("rebooked", "rebooked")
    [item] = await _items(db)
    assert (item.reason, item.status, item.resolution_action) == (
        "service_diff_rebook_due",
        "resolved",
        "rebooked",
    )
    assert item.resolution_note == old_slot.REBOOK_CLOSED_NOTE["hub"]
    after = await _deposit(db)
    assert after.status == before.status and after.reschedule_count == before.reschedule_count
    assert replans == [world.appointment.id]
    assert response.json()["google_event_id"] == expected_id
    async with db() as session:
        from secretaria.services.payments import adjustment_suspension

        assert not await adjustment_suspension.is_suspended(
            session, tenant_id=world.tenant.id, appointment_id=world.appointment.id
        )


async def test_after_the_rebook_the_visit_is_an_ordinary_one(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    assert (await _reschedule(client, world, _reschedule_body(world))).status_code == 200
    agenda.clear()

    again = await _reschedule(client, world, _reschedule_body(world, days=3))

    assert again.status_code == 200, again.text
    assert agenda == []  # moved with update_event, as any visit
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == f"difhub{adjustment.id.hex}"


async def test_the_old_event_is_gone_before_the_new_one_is_created(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    """The vacate never finished (`releasing`): `ensure_old_event_gone` deletes the old event
    first, then the new one is created."""
    world = await _lost_world(db, acting, client)
    async with db() as session:
        row = (await session.scalars(select(DepositAdjustment))).one()
        row.old_slot = "releasing"
        await session.commit()
    adjustment = await _adjustment(db)
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 200, response.text
    assert agenda[0] == ("tenant", "cancel", "evt-1")
    assert agenda[-1] == ("tenant", "create", f"difhub{adjustment.id.hex}")
    assert (await _adjustment(db)).status == "rebooked"


async def test_an_old_event_that_cannot_leave_creates_nothing(
    client: AsyncClient, db, acting, provider, agenda, replans, monkeypatch
):
    world = await _lost_world(db, acting, client)
    order: list = []

    async def _not_gone(session, *, tenant_id, adjustment_id):
        order.append("ensure")
        return False

    monkeypatch.setattr(old_slot, "ensure_old_event_gone", _not_gone)
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 503, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "calendar_unavailable" and "tente de novo" in detail["message"]
    assert order == ["ensure"] and agenda == []
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-1" and _naive(row.start_at) == _naive(world.start_at)
    assert (await _adjustment(db)).status == "old_slot_lost"
    assert replans == []


async def test_ensure_runs_before_anything_is_written(
    client: AsyncClient, db, acting, provider, agenda, replans, monkeypatch
):
    """`ensure_old_event_gone` gets a session with nothing pending (it commits it) and runs
    before the agenda is written and before `finish_rebook`."""
    world = await _lost_world(db, acting, client)
    order: list = []
    real_ensure, real_finish = old_slot.ensure_old_event_gone, old_slot.finish_rebook

    async def _ensure(session, *, tenant_id, adjustment_id):
        assert not (session.new or session.dirty or session.deleted)
        order.append(("ensure", len(agenda)))
        return await real_ensure(session, tenant_id=tenant_id, adjustment_id=adjustment_id)

    async def _finish(session, **kwargs):
        order.append(("finish", len(agenda), kwargs["source"]))
        return await real_finish(session, **kwargs)

    monkeypatch.setattr(old_slot, "ensure_old_event_gone", _ensure)
    monkeypatch.setattr(old_slot, "finish_rebook", _finish)
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 200, response.text
    assert order == [("ensure", 0), ("finish", 1, "hub")]


async def test_a_change_decided_meanwhile_rolls_back_and_deletes_the_new_event(
    client: AsyncClient, db, acting, provider, agenda, replans, monkeypatch
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)

    async def _lost_race(session, **kwargs):
        return False  # the patient's "Outro horário" (or a cancellation) won meanwhile

    monkeypatch.setattr(old_slot, "finish_rebook", _lost_race)
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "appointment_changed"
    eid = f"difhub{adjustment.id.hex}"
    assert agenda == [("tenant", "create", eid), ("tenant", "cancel", eid)]
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-1" and row.status == AppointmentStatus.SCHEDULED
    assert _naive(row.start_at) == _naive(world.start_at)
    assert replans == []


async def test_a_twin_that_won_with_the_same_event_keeps_it(
    client: AsyncClient, db, acting, provider, agenda, replans, monkeypatch
):
    """A double click: the twin request already pointed the row at the same deterministic
    event - the loser never deletes it."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    eid = f"difhub{adjustment.id.hex}"

    async def _twin_won(session, **kwargs):
        await set_appointment(db, world.appointment.id, google_event_id=eid)
        return False

    monkeypatch.setattr(old_slot, "finish_rebook", _twin_won)
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 409, response.text
    assert ("tenant", "cancel", eid) not in agenda


async def test_a_database_error_after_the_create_deletes_the_new_event(
    client: AsyncClient, db, acting, provider, agenda, replans, monkeypatch
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)

    async def _boom(session, **kwargs):
        raise RuntimeError("db blip")

    monkeypatch.setattr(old_slot, "finish_rebook", _boom)
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 503, response.text
    assert "tente de novo" in response.json()["detail"]["message"]
    eid = f"difhub{adjustment.id.hex}"
    assert agenda == [("tenant", "create", eid), ("tenant", "cancel", eid)]
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == "evt-1"
    assert (await _adjustment(db)).status == "old_slot_lost"


async def test_a_spent_event_id_falls_back_to_a_plain_create(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    """An earlier attempt created and deleted the deterministic id (an agenda never re-uses
    one): the agenda answers it `cancelled`, so a plain create - as any hub booking."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    eid = f"difhub{adjustment.id.hex}"
    FakeCalendar.get("tenant").answers = {eid: {"id": eid, "status": "cancelled"}}
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 200, response.text
    assert [c[2] for c in agenda] == [eid, "evt-new-tenant-2"]
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == (
        "evt-new-tenant-2"
    )


async def test_the_same_id_at_another_time_is_a_twin_in_flight(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    eid = f"difhub{adjustment.id.hex}"
    other = world.start_at + timedelta(days=5)
    FakeCalendar.get("tenant").answers = {
        eid: {
            "id": eid,
            "start": {"dateTime": other.isoformat()},
            "end": {"dateTime": (other + timedelta(minutes=30)).isoformat()},
        }
    }
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 409, response.text
    assert ("tenant", "cancel", eid) not in agenda  # never deleted: it may be the twin's
    assert (await _adjustment(db)).status == "old_slot_lost"


async def test_a_window_somebody_pays_for_is_refused_after_the_old_event_left(
    client: AsyncClient, db, acting, provider, agenda, replans, monkeypatch
):
    world = await _lost_world(db, acting, client)

    async def _held(*args, **kwargs):
        return True

    monkeypatch.setattr(hub_calendar.booking_hold, "live_hold_overlaps", _held)
    agenda.clear()

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "slot_held_awaiting_payment"
    assert [c for c in agenda if c[1] == "create"] == []
    assert (await _adjustment(db)).status == "old_slot_lost"


@pytest.mark.parametrize(
    ("failure", "code", "status_code"),
    [
        (GoogleTokenRevokedError("revoked"), "google_reconnect_required", 409),
        (RuntimeError("google down"), "calendar_unavailable", 502),
    ],
)
async def test_an_agenda_refusal_changes_nothing(
    client: AsyncClient, db, acting, provider, agenda, replans, failure, code, status_code
):
    world = await _lost_world(db, acting, client)
    FakeCalendar.get("tenant").fail = failure

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == status_code, response.text
    assert response.json()["detail"]["code"] == code
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == "evt-1" and row.status == AppointmentStatus.SCHEDULED
    assert (await _adjustment(db)).status == "old_slot_lost"


async def test_the_rebook_is_scoped_to_the_clinic_and_the_viewer(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    view_as(AgendaViewer("own", uuid4()))

    response = await _reschedule(client, world, _reschedule_body(world))

    assert response.status_code == 404
    assert (await _adjustment(db)).status == "old_slot_lost"


# --------------------------------------------------------------------------
# D-R11: POST /cancel of an old_slot_lost visit
# --------------------------------------------------------------------------


async def test_the_clinic_cancels_a_visit_whose_old_slot_was_taken(
    client: AsyncClient, db, acting, provider, agenda, monkeypatch
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    discarded: list = []
    order: list = []

    async def _discard(tenant_id, adjustment_id):
        row = await reload_appointment(db, world.appointment.id)  # after the commit
        order.append(row.status)
        discarded.append((tenant_id, adjustment_id))

    monkeypatch.setattr(old_slot, "discard_restore_event", _discard)
    agenda.clear()

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True}
    )

    assert response.status_code == 200, response.text
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.CANCELLED
    assert agenda == [("tenant", "cancel", "evt-1")]  # idempotent: it already left
    assert discarded == [(world.tenant.id, adjustment.id)]
    assert order == [AppointmentStatus.CANCELLED]
    # The paid sinal's cancellation rules of today ran (the clinic cancelled).
    assert response.json()["deposit_outcome"] is not None


async def test_a_plain_cancel_never_looks_for_a_restore_event(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    world = await _paid_world(db, acting)
    discarded: list = []

    async def _discard(tenant_id, adjustment_id):
        discarded.append(adjustment_id)

    monkeypatch.setattr(old_slot, "discard_restore_event", _discard)

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True}
    )

    assert response.status_code == 200, response.text
    assert discarded == []


# --------------------------------------------------------------------------
# GET /awaiting-payment
# --------------------------------------------------------------------------


async def test_the_held_window_of_a_suspended_change_says_what_it_would_do(
    client: AsyncClient, db, acting, provider
):
    world = await _suspended_world(db, acting, client)

    [row] = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert row["kind"] == "difference"
    assert row["appointment_id"] == str(world.appointment.id)
    assert row["change_summary"] == "Consulta → Avaliação"


async def test_a_visit_whose_old_slot_was_taken_is_listed_for_the_clinic(
    client: AsyncClient, db, acting, provider
):
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)

    body = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    [row] = body
    assert row["kind"] == "rebook"
    assert row["id"] == str(adjustment.id)
    assert row["appointment_id"] == str(world.appointment.id)
    assert row["service"] == "Consulta"
    assert row["change_summary"] == "Consulta → Avaliação"
    start = datetime.fromisoformat(row["start"].replace("Z", "+00:00"))
    expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
    assert _naive(start) == _naive(world.start_at) == _naive(expires)
    text = str(body)
    assert "Maria" not in text and "000201pix" not in text and "pay_diff" not in text


async def test_the_rebook_row_leaves_once_the_clinic_acts(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    world = await _lost_world(db, acting, client)
    assert (await _reschedule(client, world, _reschedule_body(world))).status_code == 200

    body = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert body == []


async def test_the_rebook_rows_are_this_clinics_in_the_window_and_the_viewers(
    client: AsyncClient, db, acting, provider
):
    world = await _lost_world(db, acting, client)
    window = _window(world)

    later = {
        "start": (world.start_at + timedelta(days=10)).isoformat(),
        "end": (world.start_at + timedelta(days=11)).isoformat(),
    }
    assert (await client.get(f"{CALENDAR}/awaiting-payment", params=later)).json() == []

    # Another clinic (its own world, acting as it) sees nothing of this one.
    from tests._r7_support import setup_world

    other = await setup_world(db, acting, phone_number_id="pnid-2")
    assert other.tenant.id != world.tenant.id
    assert (await client.get(f"{CALENDAR}/awaiting-payment", params=window)).json() == []

    acting["tenant_id"] = world.tenant.id
    async with db() as session:
        doctor = Professional(id=uuid4(), tenant_id=world.tenant.id, name="Dra. Outra")
        session.add(doctor)
        await session.commit()
    view_as(AgendaViewer("own", doctor.id))
    assert (await client.get(f"{CALENDAR}/awaiting-payment", params=window)).json() == []


async def test_a_cancelled_visit_is_never_listed_to_rebook(
    client: AsyncClient, db, acting, provider
):
    world = await _lost_world(db, acting, client)
    await set_appointment(db, world.appointment.id, status=AppointmentStatus.CANCELLED)

    body = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert body == []


# --------------------------------------------------------------------------
# GET /events: the price-change notice is never a reminder (D-R14)
# --------------------------------------------------------------------------


async def test_the_agenda_never_shows_the_price_change_notice(
    client: AsyncClient, db, acting, provider, monkeypatch
):
    world = await _paid_world(db, acting)
    now = datetime.now(UTC)
    async with db() as session:
        for kind in ("price_change", "day"):
            session.add(
                AppointmentReminder(
                    id=uuid4(),
                    tenant_id=world.tenant.id,
                    appointment_id=world.appointment.id,
                    patient_id=world.patient.id,
                    kind=kind,
                    appointment_start_at=world.start_at,
                    due_at=now - timedelta(minutes=5),
                    status="sent",
                    channel="email" if kind == "price_change" else "whatsapp",
                    with_prompt=kind != "price_change",
                    sent_at=now - timedelta(minutes=5),
                )
            )
        await session.commit()

    event = await _events(client, world, monkeypatch)

    assert [r["kind"] for r in event["reminders"]] == ["day"]
