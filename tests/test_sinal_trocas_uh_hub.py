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
from secretaria.services.calendar import CalendarUnavailableError, GoogleTokenRevokedError
from secretaria.services.local_calendar import LocalCalendarService
from secretaria.services.payments import adjustment_queries, old_slot, service_change
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
    learns ids chosen by the caller EXACTLY as the Google adapter treats them
    (`CalendarService.create_event`): an id already live is handed back (idempotent), an id
    deleted before raises `CalendarUnavailableError("event id is taken")`; `get_event` answers
    what it holds (cancelled ones too). Returns every agenda write, in order:
    (label, "create"|"cancel", event id)."""
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
        events = self.__dict__.setdefault("events", {})
        if event_id is not None and event_id in events:
            existing = events[event_id]
            if existing["status"] == "cancelled":
                raise CalendarUnavailableError("event id is taken")
            calls.append((self.label, "create", event_id))
            return dict(existing)
        self.created.append((start, end, summary, description))
        eid = event_id or f"evt-new-{self.label}-{len(self.created)}"
        calls.append((self.label, "create", eid))
        events[eid] = {
            "id": eid,
            "status": "confirmed",
            "htmlLink": f"https://calendar.example/{eid}",
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        return dict(events[eid])

    async def cancel_event(self, event_id):
        self._gate()
        if getattr(self, "cancel_fails", False):
            raise RuntimeError("google down")
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


def _parse(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _hub_id(adjustment, body: dict, calendar=None) -> str:
    """The `difhub` id of a rebook of `adjustment` at the body's window (review I1)."""
    return hub_calendar._hub_rebook_event_id(
        adjustment.id,
        calendar=calendar if calendar is not None else FakeCalendar.get("tenant"),
        start=_parse(body["new_start"]),
        end=_parse(body["new_end"]),
    )


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
    expected_id = _hub_id(adjustment, body)
    assert expected_id.startswith(f"difhub{adjustment.id.hex}")
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
    body = _reschedule_body(world)
    assert (await _reschedule(client, world, body)).status_code == 200
    agenda.clear()

    again = await _reschedule(client, world, _reschedule_body(world, days=3))

    assert again.status_code == 200, again.text
    assert agenda == []  # moved with update_event, as any visit
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == _hub_id(adjustment, body)


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
    body = _reschedule_body(world)

    response = await _reschedule(client, world, body)

    assert response.status_code == 200, response.text
    assert agenda[0] == ("tenant", "cancel", "evt-1")
    assert agenda[-1] == ("tenant", "create", _hub_id(adjustment, body))
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
    body = _reschedule_body(world)

    response = await _reschedule(client, world, body)

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "appointment_changed"
    eid = _hub_id(adjustment, body)
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
    body = _reschedule_body(world)
    eid = _hub_id(adjustment, body)

    async def _twin_won(session, **kwargs):
        await set_appointment(db, world.appointment.id, google_event_id=eid)
        return False

    monkeypatch.setattr(old_slot, "finish_rebook", _twin_won)
    agenda.clear()

    response = await _reschedule(client, world, body)

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
    body = _reschedule_body(world)

    response = await _reschedule(client, world, body)

    assert response.status_code == 503, response.text
    assert "tente de novo" in response.json()["detail"]["message"]
    eid = _hub_id(adjustment, body)
    assert agenda == [("tenant", "create", eid), ("tenant", "cancel", eid)]
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == "evt-1"
    assert (await _adjustment(db)).status == "old_slot_lost"


def test_the_hub_rebook_id_is_one_per_window():
    """Review I1: UA1's `difhub` id + 16 hex digits of the window - the same window is the same id
    (a double click is idempotent), another window is another id; base32hex only; `loc_` on the
    own agenda; short enough for Google and for `calendar_events.id` (64)."""
    import re

    adjustment_id = uuid4()
    a = datetime(2026, 12, 1, 13, 0, tzinfo=UTC)
    b = a + timedelta(hours=1)
    google = FakeCalendar("tenant")
    own = LocalCalendarService.__new__(LocalCalendarService)

    def make(calendar, start, end=None):
        return hub_calendar._hub_rebook_event_id(
            adjustment_id, calendar=calendar, start=start, end=end or start + timedelta(minutes=30)
        )

    first = make(google, a)
    assert first == make(google, a.astimezone(hub_calendar.ZoneInfo("America/Sao_Paulo")))
    assert first != make(google, b) and first != make(google, a, a + timedelta(minutes=40))
    assert first.startswith(f"difhub{adjustment_id.hex}")
    assert re.fullmatch(r"difhub[0-9a-v]+", first) and len(first) <= 64
    local = make(own, a)
    assert re.fullmatch(r"loc_difhub[0-9a-v]+", local) and len(local) <= 64


async def test_a_retry_after_a_compensated_failure_books_the_visit(
    client: AsyncClient, db, acting, provider, agenda, replans, monkeypatch
):
    """Review I1 (the reviewer's probe): an attempt created the event, the write failed and the
    event was deleted - Google never re-issues that id ("event id is taken"). The clinic tries
    the SAME window again: the spent id is detected and a plain create books the visit."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    body = _reschedule_body(world)
    eid = _hub_id(adjustment, body)
    real_finish = old_slot.finish_rebook

    async def _boom(session, **kwargs):
        raise RuntimeError("db blip")

    monkeypatch.setattr(old_slot, "finish_rebook", _boom)
    assert (await _reschedule(client, world, body)).status_code == 503
    assert ("tenant", "cancel", eid) in agenda
    monkeypatch.setattr(old_slot, "finish_rebook", real_finish)
    agenda.clear()

    response = await _reschedule(client, world, body)

    assert response.status_code == 200, response.text
    [(_, _, fresh)] = agenda  # the refused id is not a write; one plain create
    assert fresh != eid and fresh.startswith("evt-new-tenant")
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == fresh and row.status == AppointmentStatus.RESCHEDULED
    assert (await _adjustment(db)).status == "rebooked"


async def test_an_orphan_left_by_a_dead_attempt_never_blocks_another_window(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    """Review I1: a process died after creating the event at window A (never written on the
    row). The clinic now books window B: another id, booked; the orphan is not the visit's."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    first, other = _reschedule_body(world, days=2), _reschedule_body(world, days=4)
    calendar = FakeCalendar.get("tenant")
    orphan = _hub_id(adjustment, first)
    await calendar.create_event(  # the dead attempt's event
        _parse(first["new_start"]), _parse(first["new_end"]), "Consulta - Maria",
        event_id=orphan,
    )
    agenda.clear()

    response = await _reschedule(client, world, other)

    assert response.status_code == 200, response.text
    booked = _hub_id(adjustment, other)
    assert booked != orphan and agenda == [("tenant", "create", booked)]
    row = await reload_appointment(db, world.appointment.id)
    assert row.google_event_id == booked
    assert _naive(row.start_at) == _naive(_parse(other["new_start"]))
    assert (await _adjustment(db)).status == "rebooked"


async def test_a_double_click_on_the_same_window_books_once(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    """The same window is the same id: if the first request's event already exists, the second
    gets it back (no second event); after the first committed, the visit is an ordinary one."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    body = _reschedule_body(world)
    eid = _hub_id(adjustment, body)
    calendar = FakeCalendar.get("tenant")
    await calendar.create_event(  # the twin request's event, its transaction not yet in
        _parse(body["new_start"]), _parse(body["new_end"]), "Consulta - Maria", event_id=eid
    )
    agenda.clear()

    response = await _reschedule(client, world, body)

    assert response.status_code == 200, response.text
    assert len(calendar.created) == 1  # handed back, never created twice
    assert (await reload_appointment(db, world.appointment.id)).google_event_id == eid


async def test_a_window_without_an_offset_is_read_on_the_clinics_clock(
    client: AsyncClient, db, acting, provider, agenda, replans
):
    """Review M3: like every agenda read, a naive time is the clinic's (São Paulo, UTC-3): the
    event, the row and the id all use the same instant."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    local = (world.start_at + timedelta(days=2)).astimezone(
        hub_calendar.ZoneInfo("America/Sao_Paulo")
    )
    naive = {
        "new_start": local.replace(tzinfo=None).isoformat(),
        "new_end": (local + timedelta(minutes=30)).replace(tzinfo=None).isoformat(),
    }
    aware = {
        "new_start": local.astimezone(UTC).isoformat(),
        "new_end": (local + timedelta(minutes=30)).astimezone(UTC).isoformat(),
    }
    agenda.clear()

    response = await _reschedule(client, world, naive)

    assert response.status_code == 200, response.text
    [(start, end, _summary, _description)] = FakeCalendar.get("tenant").created
    assert start == local.astimezone(UTC) and end == local.astimezone(UTC) + timedelta(minutes=30)
    row = await reload_appointment(db, world.appointment.id)
    assert _naive(row.start_at) == _naive(local.astimezone(UTC))
    assert row.google_event_id == _hub_id(adjustment, aware)


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
    assert [item.reason for item in await _items(db) if item.status == "open"] == [
        "service_diff_rebook_due"
    ]
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
    # U7: the restore-event discard runs ONCE, from the post-commit hook every cancel path shares
    # (`settle_open_adjustments` -> `discard_restore_after_commit`); the route no longer calls it.
    await adjustment_queries.drain_restore_discards()

    assert response.status_code == 200, response.text
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.CANCELLED
    assert agenda == [("tenant", "cancel", "evt-1")]  # idempotent: it already left
    assert discarded == [(world.tenant.id, adjustment.id)]
    assert order == [AppointmentStatus.CANCELLED]
    # The paid sinal's cancellation rules of today ran (the clinic cancelled).
    assert response.json()["deposit_outcome"] is not None
    # UH review M4: the change is closed and the clinic's "remarque" item with it.
    assert (await _adjustment(db)).status == "cancelled"
    rebook_items = [item for item in await _items(db) if item.reason == "service_diff_rebook_due"]
    assert len(rebook_items) == 1 and rebook_items[0].status == "resolved"
    assert rebook_items[0].resolved_at is not None


async def test_a_rebook_intent_whose_change_moved_on_between_the_two_reads_is_refused(
    client: AsyncClient, db, acting, provider, agenda, monkeypatch
):
    """U7 (UF re-review N1): the stale-intent guard sees the `old_slot_lost` change, then the
    patient's "Outro horário" commits before the suspension read. With `rebook_adjustment_id`
    the second read must find the SAME change - else 409, nothing moved or cancelled (never the
    ordinary path on the patient's new slot)."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    real = hub_calendar.adjustment_queries.open_suspension
    calls = {"n": 0}

    async def first_then_gone(session, *, tenant_id, appointment_id):
        calls["n"] += 1
        if calls["n"] % 2 == 1:  # the stale-intent guard
            return await real(session, tenant_id=tenant_id, appointment_id=appointment_id)
        return None  # the patient's rebook committed in between

    monkeypatch.setattr(hub_calendar.adjustment_queries, "open_suspension", first_then_gone)
    agenda.clear()

    moved = await _reschedule(
        client, world, {**_reschedule_body(world), "rebook_adjustment_id": str(adjustment.id)}
    )
    cancelled = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/cancel",
        json={"confirm": True, "rebook_adjustment_id": str(adjustment.id)},
    )

    for response in (moved, cancelled):
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["code"] == "appointment_changed"
    assert agenda == []  # no agenda call at all
    row = await reload_appointment(db, world.appointment.id)
    assert row.status != AppointmentStatus.CANCELLED
    assert row.start_at == world.appointment.start_at
    assert (await _adjustment(db)).status == "old_slot_lost"


async def test_a_patient_rebook_during_the_cancel_wins(
    client: AsyncClient, db, acting, provider, agenda, monkeypatch
):
    """Review M1: the patient's "Outro horário" commits while the hub deletes the old event - the
    row now points at the patient's NEW event. Under the row lock the cancel sees it: 409,
    nothing cancelled, the patient's event untouched."""
    world = await _lost_world(db, acting, client)
    adjustment = await _adjustment(db)
    moved_to = world.start_at + timedelta(days=3)
    patient_event = f"difmove{adjustment.id.hex}"
    discarded: list = []

    async def _discard(tenant_id, adjustment_id):
        discarded.append(adjustment_id)

    monkeypatch.setattr(old_slot, "discard_restore_event", _discard)
    calendar = FakeCalendar.get("tenant")
    plain_cancel = FakeCalendar.cancel_event

    async def cancel_while_the_patient_rebooks(self, event_id):
        await plain_cancel(self, event_id)
        if event_id == "evt-1":  # the patient's rebook commits right now (UA3's finish_rebook)
            async with db() as session:
                row = (await session.scalars(select(DepositAdjustment))).one()
                row.status, row.old_slot = "rebooked", "rebooked"
                await session.commit()
            await set_appointment(
                db,
                world.appointment.id,
                google_event_id=patient_event,
                start_at=moved_to,
                end_at=moved_to + timedelta(minutes=30),
                status=AppointmentStatus.RESCHEDULED,
            )

    monkeypatch.setattr(FakeCalendar, "cancel_event", cancel_while_the_patient_rebooks)
    agenda.clear()

    response = await client.post(
        f"{CALENDAR}/appointments/{world.appointment.id}/cancel", json={"confirm": True}
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "appointment_changed"
    row = await reload_appointment(db, world.appointment.id)
    assert row.status == AppointmentStatus.RESCHEDULED and row.google_event_id == patient_event
    assert patient_event not in calendar.cancelled
    assert (await _adjustment(db)).status == "rebooked"
    assert discarded == []
    assert (await _deposit(db)).status.value == "confirmado_pago"


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


async def test_another_doctors_change_is_not_described_to_a_restricted_viewer(
    client: AsyncClient, db, acting, provider
):
    """Review M2: a doctor who sees only his own agenda still sees the held window (as before) but
    not what another doctor's consultation would change - the rule of the "rebook" rows."""
    world = await _suspended_world(db, acting, client)
    async with db() as session:
        doctor = Professional(id=uuid4(), tenant_id=world.tenant.id, name="Dra. Outra")
        session.add(doctor)
        await session.commit()
    view_as(AgendaViewer("own", doctor.id))

    [row] = (await client.get(f"{CALENDAR}/awaiting-payment", params=_window(world))).json()

    assert row["kind"] == "difference" and row["change_summary"] is None


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
    # UH2 (UF review I3, Orchestrator 2026-11-18): the rebook row now names the patient and the
    # channel (the same facts staff see on GET /events); still nothing of the money.
    text = str(body)
    assert "000201pix" not in text and "pay_diff" not in text


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
