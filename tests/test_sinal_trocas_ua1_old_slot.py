"""TASK-085 R / UA1 - the OLD consultation's event while a change waits for its Pix (D-R10/D-R11).

Contract: tasks/TASK-085/sinal-trocas/mapa-u.md §1.2-§1.5, §4.3 (`services/payments/old_slot.py`).
The seeded "before" clinic of tests/test_service_change.py: the old consultation (Consulta, +5 days,
30 min, event `evt-1`, the clinic's agenda) is changed to Avaliação one day later; the engine
suspends it (`mode="suspend"`, `awaiting_payment`) and holds the new window. Pinned here, on a fake
Google agenda (the `release_keep` tests' double):

* characterization: `old_slot` NULL / a legacy row / another mode or status -> nothing happens;
* `vacate_old_slot`: claim (`releasing`) -> delete -> `released`; a failure stays `releasing` and
  `retry_vacates` tries again; never raises; the row is never written;
* `old_slot_free`: agenda (own ids ignored), holds, other occupying consultations, shared agenda;
  None when the agenda cannot be read; no transaction open while the agenda answers;
* `restore_old_slot`: free -> a NEW `difkeep` event, the row re-pointed, `kept_old`/`restored`,
  the new hold released; taken / agenda down -> NOTHING changes; tap vs. deadline; lost race ->
  the created event is deleted; never raises;
* `mark_old_slot_lost`, `to_restoring`, `rebookable`, `finish_rebook`,
  `place_new_event_after_payment`, `discard_event`.

The secretarIA's own agenda (exclusive) and a person's agenda shared by two clinics are covered at
the end; the Postgres races live in tests/test_sinal_trocas_ua1_postgres.py.
"""

from datetime import UTC, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from secretaria.models import (
    Agenda,
    Appointment,
    AppointmentStatus,
    BookingHold,
    Patient,
    PaymentAttentionItem,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.models.calendar_event import CalendarEvent
from secretaria.models.deposit_adjustment import (
    MODE_APPLIED,
    OLD_SLOT_REBOOKED,
    OLD_SLOT_RELEASED,
    OLD_SLOT_RELEASING,
    OLD_SLOT_REPLACED,
    OLD_SLOT_RESTORED,
    STATUS_APPLIED,
    STATUS_AWAITING_CHOICE,
    STATUS_AWAITING_PAYMENT,
    STATUS_CANCELLED,
    STATUS_KEPT_OLD,
    STATUS_OLD_SLOT_LOST,
    STATUS_REBOOKED,
    STATUS_RESTORING,
    DepositAdjustment,
)
from secretaria.models.payment_attention_item import (
    ATTENTION_OPEN,
    ATTENTION_RESOLVED,
    REASON_SERVICE_DIFF_REBOOK_DUE,
)
from secretaria.models.pix_deposit import FULFILLMENT_NEEDS_ATTENTION
from secretaria.services import booking_hold
from secretaria.services.calendar import CalendarSlotTakenError, CalendarUnavailableError
from secretaria.services.payments import old_slot, service_change
from tests import test_service_change as base
from tests._local_agenda import agenda as local_agenda, wire_local_agenda
from tests.test_sinal_trocas_s1_unpaid import _requested

NOW = base.NOW
db = base.db
provider = base.provider
no_default_hooks = base.no_default_hooks
OLD_START = NOW + timedelta(days=5)
OLD_END = OLD_START + timedelta(minutes=30)


def _utc(value):
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


class _Calendar:
    """A Google agenda double: events by id; `create_event(event_id=)` with an id that exists
    answers the existing event (Google's 409 read as success)."""

    def __init__(self) -> None:
        self.events: dict[str, dict] = {}
        self.created: list[dict] = []
        self.cancelled: list[str] = []
        self.fail_read = False
        self.fail_cancel = False
        self.fail_create: Exception | None = None
        self.probe = None  # called on every agenda call (e.g. "is a transaction open?")
        self.on_create = None  # async hook run inside create_event (races)

    def add(self, event_id, start=OLD_START, end=OLD_END):
        self.events[event_id] = {"id": event_id, "start": start, "end": end, "status": "confirmed"}

    def _touch(self, what):
        if self.probe is not None:
            self.probe(what)

    async def check_availability(self, start, end, max_results=50):
        self._touch("check")
        if self.fail_read:
            raise CalendarUnavailableError("google down")
        return [
            dict(e) for e in self.events.values()
            if e["status"] != "cancelled" and e["start"] < end and e["end"] > start
        ]

    async def create_event(
        self, start, end, summary, description="", reminders=None, *, event_id=None
    ):
        self._touch("create")
        if self.fail_create is not None:
            raise self.fail_create
        if self.on_create is not None:
            await self.on_create()
        if event_id in self.events:
            return {**self.events[event_id], "htmlLink": f"https://cal/{event_id}"}
        self.add(event_id, start, end)
        self.created.append({"id": event_id, "start": start, "end": end, "summary": summary})
        return {"id": event_id, "status": "confirmed", "htmlLink": f"https://cal/{event_id}"}

    async def cancel_event(self, event_id):
        self._touch("cancel")
        if self.fail_cancel:
            raise CalendarUnavailableError("google down")
        self.cancelled.append(event_id)
        event = self.events.get(event_id)
        if event is None or event["status"] == "cancelled":
            return False
        event["status"] = "cancelled"
        return True

    async def get_event(self, event_id):
        return self.events.get(event_id)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(booking_hold, "_now", lambda: NOW)
    monkeypatch.setattr(old_slot, "_now", lambda: NOW)


@pytest.fixture(autouse=True)
def own_sessions(db, monkeypatch):
    """`vacate_old_slot` opens its own session (it runs after the caller's commit)."""
    monkeypatch.setattr(old_slot, "async_session_factory", db)


@pytest.fixture
def cal(monkeypatch) -> _Calendar:
    calendar = _Calendar()
    calendar.add("evt-1")  # the old consultation's event

    async def _resolve(session, tenant, appointment):
        return calendar

    monkeypatch.setattr(service_change, "calendar_for_appointment", _resolve)
    return calendar


async def _change(db, *, status=None, old_slot_state=None, expires_at=None, mode=None):
    """A suspended change of the seeded consultation; optionally moved to `status` /
    `old_slot_state` (what the engine and `vacate_old_slot` would have written)."""
    seeded, created, new_start = await _requested(db)
    assert created.kind == "suspended"
    values = {}
    if status is not None:
        values["status"] = status
    if old_slot_state is not None:
        values["old_slot"] = old_slot_state
        values["old_slot_at"] = NOW
    if expires_at is not None:
        values["expires_at"] = expires_at
    if mode is not None:
        values["mode"] = mode if mode != "legacy" else None
    if values:
        async with db() as session:
            await session.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == created.adjustment_id)
                .values(**values)
            )
            await session.commit()
    return SimpleNamespace(
        tenant_id=seeded[0].id,
        patient_id=seeded[1].id,
        appointment_id=seeded[2].id,
        deposit_id=seeded[3].id,
        adjustment_id=created.adjustment_id,
        new_start=new_start,
    )


async def _adj(db, world) -> DepositAdjustment:
    async with db() as session:
        return await session.get(DepositAdjustment, world.adjustment_id)


async def _row(db, world) -> Appointment:
    async with db() as session:
        return await session.get(Appointment, world.appointment_id)


async def _holds(db) -> list[BookingHold]:
    async with db() as session:
        return list((await session.scalars(select(BookingHold))).all())


async def _items(db) -> list[PaymentAttentionItem]:
    async with db() as session:
        return list((await session.scalars(select(PaymentAttentionItem))).all())


async def _restore(db, world, *, trigger="silence", now=None):
    async with db() as session:
        return await old_slot.restore_old_slot(
            session, tenant_id=world.tenant_id, adjustment_id=world.adjustment_id,
            now=now or NOW + timedelta(days=1), trigger=trigger,
        )


async def _free(db, world):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant_id)
        row = await session.get(Appointment, world.appointment_id)
        adjustment = await session.get(DepositAdjustment, world.adjustment_id)
        return await old_slot.old_slot_free(
            session, tenant=tenant, appointment=row, adjustment=adjustment
        )


async def _add(db, *rows):
    async with db() as session:
        session.add_all(rows)
        await session.commit()


def _other_visit(world, *, professional_id=None, start=OLD_START, end=OLD_END):
    return Appointment(
        id=uuid4(), tenant_id=world.tenant_id, google_event_id=f"evt{uuid4().hex[:6]}",
        appointment_type="Consulta", professional_id=professional_id, start_at=start,
        end_at=end, status=AppointmentStatus.SCHEDULED,
    )


# --------------------------------------------------------------------------
# vacate_old_slot / retry_vacates
# --------------------------------------------------------------------------


async def test_vacate_takes_the_old_event_off_the_agenda_and_keeps_the_row(db, provider, cal):
    world = await _change(db)

    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "released"

    assert cal.cancelled == ["evt-1"]
    adjustment = await _adj(db, world)
    assert (adjustment.old_slot, _utc(adjustment.old_slot_at)) == (OLD_SLOT_RELEASED, NOW)
    assert adjustment.status == STATUS_AWAITING_PAYMENT
    row = await _row(db, world)
    assert (row.google_event_id, row.status, _utc(row.start_at)) == (
        "evt-1", AppointmentStatus.SCHEDULED, OLD_START,
    )
    # The new window stays held.
    assert [h.adjustment_id for h in await _holds(db)] == [world.adjustment_id]


async def test_vacate_runs_once(db, provider, cal):
    world = await _change(db)
    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "released"

    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "not_applicable"
    assert cal.cancelled == ["evt-1"]


async def test_vacate_also_applies_while_the_patient_chooses(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE)
    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "released"


@pytest.mark.parametrize(
    "kw",
    [
        {"mode": "legacy"},  # a legacy deferred row: the old visit stands on the agenda
        {"mode": MODE_APPLIED},
        {"status": STATUS_KEPT_OLD},
        {"status": STATUS_APPLIED},
        {"status": STATUS_RESTORING},
        {"old_slot_state": OLD_SLOT_RESTORED},
    ],
)
async def test_vacate_leaves_everything_else_alone(db, provider, cal, kw):
    world = await _change(db, **kw)

    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "not_applicable"

    assert cal.cancelled == []
    assert (await _adj(db, world)).old_slot == kw.get("old_slot_state")


async def test_vacate_of_another_clinic_does_nothing(db, provider, cal):
    world = await _change(db)
    assert await old_slot.vacate_old_slot(uuid4(), world.adjustment_id) == "not_applicable"
    assert cal.cancelled == [] and (await _adj(db, world)).old_slot is None


async def test_vacate_of_a_cancelled_visit_does_nothing(db, provider, cal):
    world = await _change(db)
    async with db() as session:
        (await session.get(Appointment, world.appointment_id)).status = AppointmentStatus.CANCELLED
        await session.commit()
    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "not_applicable"
    assert cal.cancelled == []


async def test_vacate_with_the_agenda_down_stays_releasing_and_the_retry_finishes(
    db, provider, cal
):
    world = await _change(db)
    cal.fail_cancel = True

    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "releasing"
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASING

    cal.fail_cancel = False
    async with db() as session:
        # Too early: the claim is younger than RETRY_AFTER.
        assert await old_slot.retry_vacates(session, now=NOW + timedelta(minutes=1)) == 0
        assert cal.cancelled == []
        assert await old_slot.retry_vacates(session, now=NOW + old_slot.RETRY_AFTER) == 1
    assert cal.cancelled == ["evt-1"]
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASED


async def test_the_retry_skips_a_change_that_already_ended(db, provider, cal):
    world = await _change(db, status=STATUS_KEPT_OLD, old_slot_state=OLD_SLOT_RELEASING)
    async with db() as session:
        assert await old_slot.retry_vacates(session, now=NOW + timedelta(hours=1)) == 0
    assert cal.cancelled == [] and (await _adj(db, world)).old_slot == OLD_SLOT_RELEASING


@pytest.mark.parametrize("status", [STATUS_RESTORING, STATUS_OLD_SLOT_LOST])
async def test_the_retry_finishes_a_claim_whose_change_went_on_suspended(
    db, provider, cal, status
):
    """Decision: a `releasing` claim is finished in ANY suspended status - a change that went on
    to `restoring`/`old_slot_lost` must not keep the old event on the agenda either."""
    world = await _change(db, status=status, old_slot_state=OLD_SLOT_RELEASING)
    async with db() as session:
        assert await old_slot.retry_vacates(session, now=NOW + timedelta(hours=1)) == 1
    assert cal.cancelled == ["evt-1"]
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASED


async def test_the_retry_never_claims_a_change_that_is_no_longer_open(db, provider, cal):
    world = await _change(db, status=STATUS_RESTORING)
    async with db() as session:
        created = _utc((await session.get(DepositAdjustment, world.adjustment_id)).created_at)
        assert await old_slot.retry_vacates(session, now=created + timedelta(hours=1)) == 0
    assert cal.cancelled == [] and (await _adj(db, world)).old_slot is None


async def test_the_retry_also_claims_a_change_whose_vacate_never_ran(db, provider, cal):
    """Decision: the post-commit call is best-effort (a worker may die right after the commit);
    the sweep also claims an open suspended change still at `old_slot` NULL once it is older than
    RETRY_AFTER."""
    world = await _change(db)
    async with db() as session:
        created = _utc((await session.get(DepositAdjustment, world.adjustment_id)).created_at)
        assert await old_slot.retry_vacates(session, now=created + timedelta(seconds=30)) == 0
        assert await old_slot.retry_vacates(session, now=created + old_slot.RETRY_AFTER) == 1
    assert cal.cancelled == ["evt-1"]
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASED


async def test_the_retry_goes_on_after_one_bad_row(db, provider, cal, monkeypatch):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASING)
    calls = {"n": 0}
    original = old_slot._release_claimed

    async def flaky(session, adjustment_id, *, now):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")
        return await original(session, adjustment_id, now=now)

    monkeypatch.setattr(old_slot, "_release_claimed", flaky)
    async with db() as session:
        # One row, failing: never raises, nothing released.
        assert await old_slot.retry_vacates(session, now=NOW + timedelta(hours=1)) == 0
        assert await old_slot.retry_vacates(session, now=NOW + timedelta(hours=1)) == 1
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASED


async def test_vacate_never_raises(db, provider, cal, monkeypatch):
    world = await _change(db)

    def broken():
        raise RuntimeError("database down")

    monkeypatch.setattr(old_slot, "async_session_factory", broken)
    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "not_applicable"


async def test_vacate_holds_no_transaction_while_the_agenda_answers(db, provider, cal, monkeypatch):
    world = await _change(db)
    seen: list[tuple[str, bool]] = []
    sessions: list = []

    def factory():
        session = db()
        sessions.append(session)
        return session

    monkeypatch.setattr(old_slot, "async_session_factory", factory)
    cal.probe = lambda what: seen.append((what, sessions[-1].in_transaction()))

    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "released"
    assert seen == [("cancel", False)]


# --------------------------------------------------------------------------
# old_slot_free
# --------------------------------------------------------------------------


async def test_the_old_slot_is_free_once_the_event_left(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")
    assert await _free(db, world) is True


async def test_our_own_events_never_take_the_slot(db, provider, cal):
    """The old event itself (still there while `releasing`) and the restore's own id."""
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASING)
    cal.add(old_slot.restore_event_id(world.adjustment_id, calendar=cal))
    assert await _free(db, world) is True


async def test_someone_elses_event_takes_the_slot(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.add("someone-else", OLD_START + timedelta(minutes=15), OLD_END + timedelta(minutes=15))
    assert await _free(db, world) is False


async def test_a_sibling_window_without_id_takes_the_slot(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.events["sib"] = {"id": None, "start": OLD_START, "end": OLD_END, "status": "confirmed"}
    assert await _free(db, world) is False


async def test_an_unreadable_agenda_is_none(db, provider, cal, monkeypatch):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.fail_read = True
    assert await _free(db, world) is None

    async def no_calendar(session, tenant, appointment):
        return None

    monkeypatch.setattr(service_change, "calendar_for_appointment", no_calendar)
    assert await _free(db, world) is None


async def test_another_live_visit_of_the_agenda_takes_the_slot(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    await _add(db, _other_visit(world))
    assert await _free(db, world) is False


async def test_a_visit_of_another_agenda_or_a_cancelled_one_does_not(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    async with db() as session:
        doctor = Professional(id=uuid4(), tenant_id=world.tenant_id, name="Dr. B", is_active=True)
        session.add(doctor)
        await session.commit()
    cancelled = _other_visit(world)
    cancelled.status = AppointmentStatus.CANCELLED
    await _add(db, _other_visit(world, professional_id=doctor.id), cancelled)
    assert await _free(db, world) is True


async def test_a_suspended_visit_of_the_agenda_does_not_take_the_slot(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    other = _other_visit(world)
    await _add(db, other)
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(), tenant_id=world.tenant_id, appointment_id=other.id, provider="asaas",
            amount_cents=6000, percent_applied=30, status=PixDepositStatus.PAID,
        )
        session.add(deposit)
        await session.flush()
        session.add(DepositAdjustment(
            tenant_id=world.tenant_id, deposit_id=deposit.id, appointment_id=other.id,
            kind="increase", mode="suspend", status=STATUS_AWAITING_PAYMENT,
            old_service="Consulta", new_service="Avaliação", diff_cents=3000,
        ))
        await session.commit()
    assert await _free(db, world) is True


async def test_another_patients_payment_hold_takes_the_slot(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    await _add(db, BookingHold(
        tenant_id=world.tenant_id, conversation_id=uuid4(), professional_id=None,
        start_at=OLD_START, end_at=OLD_END, expires_at=NOW + timedelta(minutes=20),
        phase="payment",
    ))
    assert await _free(db, world) is False


async def test_the_changes_own_hold_never_takes_the_slot(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    async with db() as session:
        await session.execute(
            update(BookingHold)
            .where(BookingHold.adjustment_id == world.adjustment_id)
            .values(start_at=OLD_START, end_at=OLD_END)
        )
        await session.commit()
    assert await _free(db, world) is True


async def test_old_slot_free_holds_no_transaction_while_the_agenda_answers(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    seen = []
    async with db() as session:
        cal.probe = lambda what: seen.append((what, session.in_transaction()))
        tenant = await session.get(Tenant, world.tenant_id)
        row = await session.get(Appointment, world.appointment_id)
        adjustment = await session.get(DepositAdjustment, world.adjustment_id)
        assert await old_slot.old_slot_free(
            session, tenant=tenant, appointment=row, adjustment=adjustment
        ) is True
    assert seen == [("check", False)]


# --------------------------------------------------------------------------
# restore_old_slot
# --------------------------------------------------------------------------


async def test_a_free_slot_brings_the_old_visit_back_with_a_new_event(db, provider, cal):
    world = await _change(
        db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED,
        expires_at=NOW + timedelta(hours=1),
    )
    cal.events.pop("evt-1")
    at = NOW + timedelta(hours=2)

    assert await _restore(db, world, trigger="silence", now=at) == "restored"

    own = old_slot.restore_event_id(world.adjustment_id, calendar=cal)
    [created] = cal.created
    assert (created["id"], created["start"], created["end"]) == (own, OLD_START, OLD_END)
    assert created["summary"].startswith("Consulta")
    row = await _row(db, world)
    assert (row.google_event_id, row.google_event_link) == (own, f"https://cal/{own}")
    assert (row.status, row.appointment_type, _utc(row.start_at)) == (
        AppointmentStatus.SCHEDULED, "Consulta", OLD_START,
    )
    adjustment = await _adj(db, world)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_KEPT_OLD, OLD_SLOT_RESTORED)
    assert _utc(adjustment.resolved_at) == at and _utc(adjustment.old_slot_at) == at
    assert await _holds(db) == []


async def test_restore_holds_no_transaction_while_the_agenda_answers(db, provider, cal):
    world = await _change(db, status=STATUS_RESTORING, old_slot_state=OLD_SLOT_RELEASING)
    seen = []
    async with db() as session:
        cal.probe = lambda what: seen.append((what, session.in_transaction()))
        outcome = await old_slot.restore_old_slot(
            session, tenant_id=world.tenant_id, adjustment_id=world.adjustment_id,
            now=NOW + timedelta(hours=1), trigger="no_room",
        )
    assert outcome == "restored"
    assert seen == [("cancel", False), ("check", False), ("create", False)]


async def test_a_tap_counts_only_before_the_deadline(db, provider, cal):
    deadline = NOW + timedelta(hours=1)
    world = await _change(
        db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED, expires_at=deadline
    )
    assert await _restore(db, world, trigger="tap", now=deadline) == "already_decided"
    assert cal.created == [] and (await _adj(db, world)).status == STATUS_AWAITING_CHOICE

    assert await _restore(db, world, trigger="tap", now=deadline - timedelta(seconds=1)) == (
        "restored"
    )


async def test_a_tap_never_restores_a_change_that_is_already_restoring(db, provider, cal):
    world = await _change(db, status=STATUS_RESTORING, old_slot_state=OLD_SLOT_RELEASED)
    assert await _restore(db, world, trigger="tap") == "already_decided"
    assert await _restore(db, world, trigger="no_room") == "restored"


@pytest.mark.parametrize(
    "status", [STATUS_AWAITING_PAYMENT, STATUS_KEPT_OLD, STATUS_OLD_SLOT_LOST, STATUS_CANCELLED]
)
async def test_only_a_restorable_change_comes_back(db, provider, cal, status):
    world = await _change(db, status=status, old_slot_state=OLD_SLOT_RELEASED)
    assert await _restore(db, world, trigger="silence") == "already_decided"
    assert cal.created == [] and (await _adj(db, world)).status == status


async def test_a_taken_slot_changes_nothing(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")
    cal.add("another-patient")

    assert await _restore(db, world) == "slot_taken"

    assert cal.created == []
    adjustment = await _adj(db, world)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_AWAITING_CHOICE, OLD_SLOT_RELEASED)
    assert (await _row(db, world)).google_event_id == "evt-1"
    assert len(await _holds(db)) == 1


async def test_a_slot_taken_at_the_write_changes_nothing(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    cal.fail_create = CalendarSlotTakenError("taken")
    assert await _restore(db, world) == "slot_taken"
    assert (await _adj(db, world)).status == STATUS_AWAITING_CHOICE


@pytest.mark.parametrize("failure", ["read", "create", "cancel", "resolver"])
async def test_an_unreachable_agenda_changes_nothing(db, provider, cal, monkeypatch, failure):
    state = OLD_SLOT_RELEASING if failure == "cancel" else OLD_SLOT_RELEASED
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=state)
    if failure == "read":
        cal.fail_read = True
    elif failure == "create":
        cal.fail_create = CalendarUnavailableError("down")
    elif failure == "cancel":
        cal.fail_cancel = True
    else:

        async def broken(session, tenant, appointment):
            raise RuntimeError("cannot build the agenda")

        monkeypatch.setattr(service_change, "calendar_for_appointment", broken)

    assert await _restore(db, world) == "calendar_unavailable"
    adjustment = await _adj(db, world)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_AWAITING_CHOICE, state)
    assert len(await _holds(db)) == 1


async def test_a_dead_event_answer_is_never_pointed_at(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    own = old_slot.restore_event_id(world.adjustment_id, calendar=cal)
    cal.add(own, OLD_START, OLD_END)
    cal.events[own]["status"] = "cancelled"
    assert await _restore(db, world) == "calendar_unavailable"
    assert (await _row(db, world)).google_event_id == "evt-1"


async def test_an_event_that_never_left_only_needs_the_transition(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE)
    cal.fail_read = True  # never asked

    assert await _restore(db, world) == "restored"

    adjustment = await _adj(db, world)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_KEPT_OLD, None)
    assert (await _row(db, world)).google_event_id == "evt-1"
    assert cal.created == [] and cal.cancelled == [] and await _holds(db) == []


async def test_a_releasing_event_is_deleted_before_the_new_one_is_created(db, provider, cal):
    """Decision: a `releasing` row never takes the "the old event is still there" shortcut - a
    vacate may be deleting it right now. The old event is deleted (idempotent) and a new one is
    created, so the consultation never points at an event somebody else deletes."""
    world = await _change(db, status=STATUS_RESTORING, old_slot_state=OLD_SLOT_RELEASING)

    assert await _restore(db, world, trigger="no_room") == "restored"

    own = old_slot.restore_event_id(world.adjustment_id, calendar=cal)
    assert cal.cancelled == ["evt-1"] and [c["id"] for c in cal.created] == [own]
    assert (await _row(db, world)).google_event_id == own


async def test_a_cancelled_or_started_visit_is_not_applicable(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    assert await _restore(db, world, now=OLD_START) == "not_applicable"
    async with db() as session:
        (await session.get(Appointment, world.appointment_id)).status = AppointmentStatus.CANCELLED
        await session.commit()
    assert await _restore(db, world) == "not_applicable"
    assert cal.created == [] and (await _adj(db, world)).status == STATUS_AWAITING_CHOICE


async def test_restore_of_another_clinic_or_an_unknown_change_is_not_applicable(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    async with db() as session:
        assert await old_slot.restore_old_slot(
            session, tenant_id=uuid4(), adjustment_id=world.adjustment_id,
            now=NOW + timedelta(days=1), trigger="silence",
        ) == "not_applicable"
        assert await old_slot.restore_old_slot(
            session, tenant_id=world.tenant_id, adjustment_id=uuid4(),
            now=NOW + timedelta(days=1), trigger="silence",
        ) == "not_applicable"
    assert cal.created == []


async def test_losing_the_race_deletes_the_event_it_created(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)

    async def cancelled_meanwhile():
        async with db() as other:  # "Cancelar ambas" lands while the event is being created
            await other.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == world.adjustment_id)
                .values(status=STATUS_CANCELLED)
            )
            await other.commit()

    cal.on_create = cancelled_meanwhile

    assert await _restore(db, world) == "already_decided"

    own = old_slot.restore_event_id(world.adjustment_id, calendar=cal)
    assert own in cal.cancelled
    row = await _row(db, world)
    assert row.google_event_id == "evt-1"
    assert (await _adj(db, world)).status == STATUS_CANCELLED


async def test_a_twin_restore_that_won_keeps_the_shared_event(db, provider, cal):
    """Two restorers (the tap and the sweep) create the SAME deterministic id; the one that
    loses the claim must not delete the winner's event."""
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    own = old_slot.restore_event_id(world.adjustment_id, calendar=cal)

    async def twin_won():
        async with db() as other:
            await other.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == world.adjustment_id)
                .values(status=STATUS_KEPT_OLD, old_slot=OLD_SLOT_RESTORED)
            )
            await other.execute(
                update(Appointment)
                .where(Appointment.id == world.appointment_id)
                .values(google_event_id=own)
            )
            await other.commit()

    cal.on_create = twin_won

    assert await _restore(db, world) == "already_decided"
    assert own not in cal.cancelled


async def test_restore_never_raises(db, provider, cal, monkeypatch):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)

    async def explode(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(old_slot, "old_slot_free", explode)
    assert await _restore(db, world) == "calendar_unavailable"
    assert (await _adj(db, world)).status == STATUS_AWAITING_CHOICE


# --------------------------------------------------------------------------
# mark_old_slot_lost / to_restoring
# --------------------------------------------------------------------------


async def _lose(db, world, now=None):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant_id)
        adjustment = await session.get(DepositAdjustment, world.adjustment_id)
        won = await old_slot.mark_old_slot_lost(
            session, tenant=tenant, adjustment=adjustment, now=now or NOW + timedelta(days=1)
        )
        await session.commit()
        return won


async def test_a_lost_slot_opens_one_item_for_the_clinic_and_frees_the_new_window(
    db, provider, cal
):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)

    assert await _lose(db, world) is True
    assert await _lose(db, world) is False

    adjustment = await _adj(db, world)
    assert adjustment.status == STATUS_OLD_SLOT_LOST and adjustment.resolved_at is None
    assert await _holds(db) == []
    [item] = await _items(db)
    assert (item.reason, item.status, item.tenant_id) == (
        REASON_SERVICE_DIFF_REBOOK_DUE, ATTENTION_OPEN, world.tenant_id,
    )
    assert item.amount_cents == 6000  # the paid sinal
    assert (_utc(item.slot_start), _utc(item.slot_end)) == (OLD_START, OLD_END)
    assert item.deposit_id == world.deposit_id and item.service_label == "Consulta"
    assert item.dedupe_key == old_slot.rebook_item_key("asaas", world.adjustment_id)
    assert str(world.adjustment_id) in item.dedupe_key


@pytest.mark.parametrize("status", [STATUS_AWAITING_PAYMENT, STATUS_KEPT_OLD, STATUS_CANCELLED])
async def test_only_a_restorable_change_can_lose_its_slot(db, provider, cal, status):
    world = await _change(db, status=status, old_slot_state=OLD_SLOT_RELEASED)
    assert await _lose(db, world) is False
    assert await _items(db) == [] and (await _adj(db, world)).status == status


async def test_to_restoring_is_guarded_and_frees_the_new_window(db, provider, cal):
    world = await _change(db)
    async with db() as session:
        adjustment = await session.get(DepositAdjustment, world.adjustment_id)
        assert await old_slot.to_restoring(
            session, adjustment=adjustment, from_status=STATUS_AWAITING_CHOICE, now=NOW
        ) is False
        assert await old_slot.to_restoring(
            session, adjustment=adjustment, from_status=STATUS_AWAITING_PAYMENT, now=NOW
        ) is True
        await session.commit()
    adjustment = await _adj(db, world)
    assert adjustment.status == STATUS_RESTORING and adjustment.resolved_at is None
    assert await _holds(db) == []


# --------------------------------------------------------------------------
# rebookable / finish_rebook
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "found"),
    [
        (STATUS_AWAITING_CHOICE, True),
        (STATUS_OLD_SLOT_LOST, True),
        (STATUS_AWAITING_PAYMENT, False),
        (STATUS_RESTORING, False),
        (STATUS_KEPT_OLD, False),
    ],
)
async def test_rebookable_is_only_a_choice_or_a_lost_slot(db, provider, cal, status, found):
    world = await _change(db, status=status)
    async with db() as session:
        hit = await old_slot.rebookable(
            session, tenant_id=world.tenant_id, appointment_id=world.appointment_id
        )
        other = await old_slot.rebookable(
            session, tenant_id=uuid4(), appointment_id=world.appointment_id
        )
    assert (hit is not None and hit.id == world.adjustment_id) is found
    assert other is None


async def _finish(db, world, *, source="patient", tenant_id=None):
    async with db() as session:
        row = await session.get(Appointment, world.appointment_id)
        new_start = NOW + timedelta(days=7)
        row.start_at, row.end_at = new_start, new_start + timedelta(minutes=30)
        row.status = AppointmentStatus.RESCHEDULED
        won = await old_slot.finish_rebook(
            session, tenant_id=tenant_id or world.tenant_id, adjustment_id=world.adjustment_id,
            appointment=row, event_id="difmovenew", event_link="https://cal/difmovenew",
            now=NOW + timedelta(days=1), source=source,
        )
        if won:
            await session.commit()
        else:
            await session.rollback()
        return won


async def test_finishing_a_rebook_closes_the_change_and_its_item(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    assert await _lose(db, world) is True
    async with db() as session:
        deposit = await session.get(PixDeposit, world.deposit_id)
        assert deposit.fulfillment_status == FULFILLMENT_NEEDS_ATTENTION

    assert await _finish(db, world, source="hub") is True

    adjustment = await _adj(db, world)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_REBOOKED, OLD_SLOT_REBOOKED)
    assert _utc(adjustment.resolved_at) == NOW + timedelta(days=1)
    row = await _row(db, world)
    assert (row.google_event_id, row.google_event_link) == ("difmovenew", "https://cal/difmovenew")
    assert row.status == AppointmentStatus.RESCHEDULED
    [item] = await _items(db)
    assert (item.status, item.resolution_action) == (ATTENTION_RESOLVED, "rebooked")
    async with db() as session:
        deposit = await session.get(PixDeposit, world.deposit_id)
        assert deposit.fulfillment_status != FULFILLMENT_NEEDS_ATTENTION
        assert deposit.appointment_id == world.appointment_id  # the paid sinal stays
    assert await _holds(db) == []


async def test_a_rebook_is_decided_once(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE)
    async with db() as session:  # fix round 1 (I1): the callers' first step
        assert await old_slot.ensure_old_event_gone(
            session, tenant_id=world.tenant_id, adjustment_id=world.adjustment_id
        ) is True
    assert await _finish(db, world) is True
    assert await _finish(db, world, source="hub") is False
    assert (await _row(db, world)).google_event_id == "difmovenew"
    assert cal.events["evt-1"]["status"] == "cancelled"  # the old event is gone for good


async def test_a_rebook_of_another_clinic_is_refused(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE)
    assert await _finish(db, world, tenant_id=uuid4()) is False
    assert (await _adj(db, world)).status == STATUS_AWAITING_CHOICE


async def test_a_rebook_of_a_change_being_restored_is_refused(db, provider, cal):
    world = await _change(db, status=STATUS_RESTORING)
    assert await _finish(db, world) is False
    assert (await _row(db, world)).google_event_id == "evt-1"


# --------------------------------------------------------------------------
# place_new_event_after_payment
# --------------------------------------------------------------------------


async def _paid_place(db, world, **kw):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant_id)
        row = await session.get(Appointment, world.appointment_id)
        adjustment = await session.get(DepositAdjustment, world.adjustment_id)
        # What the webhook wrote (`_apply_full_edit`): the new service and window on the row.
        row.appointment_type = "Avaliação"
        row.start_at, row.end_at = world.new_start, world.new_start + timedelta(minutes=60)
        await session.execute(
            update(DepositAdjustment)
            .where(DepositAdjustment.id == world.adjustment_id)
            .values(status=STATUS_APPLIED)
        )
        await session.commit()
        await session.refresh(adjustment)
        await old_slot.place_new_event_after_payment(
            session, tenant=tenant, appointment=row, adjustment=adjustment, patient_name="Maria",
            **kw,
        )


async def test_paid_after_the_old_event_left_creates_the_new_event(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")

    await _paid_place(db, world)

    own = old_slot.paid_event_id(world.adjustment_id, calendar=cal)
    [created] = cal.created
    assert (created["id"], created["start"]) == (own, world.new_start)
    assert created["summary"] == "Avaliação - Maria"
    row = await _row(db, world)
    assert (row.google_event_id, row.google_event_link) == (own, f"https://cal/{own}")
    assert row.google_calendar_source == "clinic"
    adjustment = await _adj(db, world)
    assert adjustment.old_slot == OLD_SLOT_REPLACED and _utc(adjustment.old_slot_at) == NOW


async def test_paid_while_still_releasing_deletes_the_old_event_first(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASING)
    await _paid_place(db, world)
    assert cal.cancelled == ["evt-1"]
    assert (await _adj(db, world)).old_slot == OLD_SLOT_REPLACED


async def test_paid_onto_a_taken_window_raises_and_changes_nothing(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.add("someone", world.new_start, world.new_start + timedelta(minutes=60))

    with pytest.raises(CalendarSlotTakenError):
        await _paid_place(db, world)

    assert cal.created == []
    assert (await _row(db, world)).google_event_id == "evt-1"
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASED


async def test_paid_with_the_agenda_down_raises(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASING)
    cal.fail_cancel = True
    with pytest.raises(CalendarUnavailableError):
        await _paid_place(db, world)
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASING


async def test_paid_twice_is_the_same_event(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    await _paid_place(db, world)
    await _paid_place(db, world)
    assert len(cal.created) == 1


async def test_paid_change_of_doctor_lands_on_the_new_doctors_agenda(db, provider, monkeypatch):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASING)
    clinic, doctor_cal = _Calendar(), _Calendar()
    clinic.add("evt-1")
    async with db() as session:
        doctor = Professional(id=uuid4(), tenant_id=world.tenant_id, name="Dr. B", is_active=True)
        session.add(doctor)
        await session.flush()
        (await session.get(Appointment, world.appointment_id)).professional_id = doctor.id
        await session.commit()

    async def resolve(session, tenant, owner):
        return doctor_cal if owner.professional_id == doctor.id else clinic

    monkeypatch.setattr(service_change, "calendar_for_appointment", resolve)

    await _paid_place(db, world)

    assert clinic.cancelled == ["evt-1"] and clinic.created == []
    [created] = doctor_cal.created
    assert created["id"] == old_slot.paid_event_id(world.adjustment_id, calendar=doctor_cal)
    assert (await _row(db, world)).google_calendar_source == "professional"


# --------------------------------------------------------------------------
# discard_event
# --------------------------------------------------------------------------


async def test_discard_event_never_raises(cal):
    cal.fail_cancel = True
    await old_slot.discard_event(cal, "difkeepx")
    await old_slot.discard_event(None, "difkeepx")
    cal.fail_cancel = False
    await old_slot.discard_event(cal, "")
    assert cal.cancelled == []
    await old_slot.discard_event(cal, "difkeepx")
    assert cal.cancelled == ["difkeepx"]


# --------------------------------------------------------------------------
# the secretarIA's own agenda (exclusive writes)
# --------------------------------------------------------------------------


async def _on_local_agenda(db, monkeypatch, world):
    """The seeded clinic on the secretarIA's own agenda: the old event is a local one."""
    wire_local_agenda(monkeypatch, db)
    local = local_agenda(world.tenant_id)
    event = await local.create_event(OLD_START, OLD_END, "Consulta - Maria")
    async with db() as session:
        (await session.get(Appointment, world.appointment_id)).google_event_id = event["id"]
        await session.commit()

    async def resolve(session, tenant, appointment):
        return local

    monkeypatch.setattr(service_change, "calendar_for_appointment", resolve)
    return local, event["id"]


async def _local_event(db, event_id) -> CalendarEvent | None:
    async with db() as session:
        return await session.get(CalendarEvent, event_id)


async def test_own_agenda_vacate_frees_the_slot_and_restore_brings_it_back(
    db, provider, monkeypatch
):
    world = await _change(db)
    local, old_id = await _on_local_agenda(db, monkeypatch, world)

    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "released"
    assert (await _local_event(db, old_id)).status == "cancelled"
    assert await local.check_availability(OLD_START, OLD_END) == []

    async with db() as session:
        await session.execute(
            update(DepositAdjustment)
            .where(DepositAdjustment.id == world.adjustment_id)
            .values(status=STATUS_AWAITING_CHOICE)
        )
        await session.commit()
    assert await _restore(db, world) == "restored"

    own = old_slot.restore_event_id(world.adjustment_id, calendar=local)
    assert own.startswith("loc_difkeep")
    restored = await _local_event(db, own)
    assert restored.status == "confirmed" and _utc(restored.start_at) == OLD_START
    assert (await _row(db, world)).google_event_id == own


async def test_own_agenda_restore_is_exclusive(db, provider, monkeypatch):
    """Another booking that lands between the check and the write: the exclusive create
    refuses, the restore answers "slot_taken" and nothing changes."""
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    local, old_id = await _on_local_agenda(db, monkeypatch, world)
    await local.cancel_event(old_id)
    await local.create_event(OLD_START, OLD_END, "Outro paciente")

    async def looks_free(*args, **kwargs):
        return True

    monkeypatch.setattr(old_slot, "old_slot_free", looks_free)

    assert await _restore(db, world) == "slot_taken"
    own = old_slot.restore_event_id(world.adjustment_id, calendar=local)
    assert await _local_event(db, own) is None
    assert (await _adj(db, world)).status == STATUS_AWAITING_CHOICE


async def test_own_agenda_paid_change_creates_the_local_event(db, provider, monkeypatch):
    world = await _change(db)
    local, _old_id = await _on_local_agenda(db, monkeypatch, world)
    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == "released"

    await _paid_place(db, world)

    own = old_slot.paid_event_id(world.adjustment_id, calendar=local)
    assert own.startswith("loc_difpaid")
    assert _utc((await _local_event(db, own)).start_at) == world.new_start
    assert (await _row(db, world)).google_event_id == own


# --------------------------------------------------------------------------
# a person's agenda shared by two clinics (TASK-083)
# --------------------------------------------------------------------------


async def _shared(db, world):
    """The seeded consultation belongs to Dr. X, who also works at clinic P (one agenda)."""
    async with db() as session:
        other = Tenant(
            id=uuid4(), clinic_name="Clínica P", is_active=True, timezone="America/Sao_Paulo"
        )
        session.add(other)
        await session.flush()
        x_here = Professional(id=uuid4(), tenant_id=world.tenant_id, name="Dr. X", is_active=True)
        x_there = Professional(id=uuid4(), tenant_id=other.id, name="Dr. X", is_active=True)
        person = Agenda(person_key="acct-x")
        session.add_all([x_here, x_there, person])
        await session.flush()
        x_here.agenda_id = x_there.agenda_id = person.id
        row = await session.get(Appointment, world.appointment_id)
        row.professional_id = x_here.id
        row.google_calendar_source = "professional"
        await session.commit()
    return other, x_there


async def _sibling_visit(db, other, x_there, *, suspended=False):
    async with db() as session:
        patient = Patient(id=uuid4(), tenant_id=other.id, wa_id="5511988887777", name="Z")
        session.add(patient)
        await session.flush()
        visit = Appointment(
            id=uuid4(), tenant_id=other.id, patient_id=patient.id, google_event_id="evt-p",
            appointment_type="Consulta", professional_id=x_there.id, start_at=OLD_START,
            end_at=OLD_END, status=AppointmentStatus.SCHEDULED,
        )
        session.add(visit)
        await session.flush()
        if suspended:
            deposit = PixDeposit(
                id=uuid4(), tenant_id=other.id, appointment_id=visit.id, provider="asaas",
                amount_cents=6000, percent_applied=30, status=PixDepositStatus.PAID,
            )
            session.add(deposit)
            await session.flush()
            session.add(DepositAdjustment(
                tenant_id=other.id, deposit_id=deposit.id, appointment_id=visit.id,
                kind="increase", mode="suspend", status=STATUS_AWAITING_PAYMENT,
                old_service="Consulta", new_service="Avaliação", diff_cents=3000,
            ))
        await session.commit()


async def test_shared_agenda_the_doctor_busy_at_the_other_clinic_takes_the_slot(
    db, provider, cal
):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")
    other, x_there = await _shared(db, world)
    await _sibling_visit(db, other, x_there)

    assert await _free(db, world) is False
    assert await _restore(db, world) == "slot_taken"
    assert cal.created == []


async def test_shared_agenda_a_suspended_visit_at_the_other_clinic_does_not(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")
    other, x_there = await _shared(db, world)
    await _sibling_visit(db, other, x_there, suspended=True)

    assert await _free(db, world) is True
    assert await _restore(db, world) == "restored"


# --------------------------------------------------------------------------
# Fix round 1 (reviews/UA1-review.md) - the reviewer's probes as real tests
# --------------------------------------------------------------------------


async def _ensure(db, world, tenant_id=None):
    async with db() as session:
        return await old_slot.ensure_old_event_gone(
            session, tenant_id=tenant_id or world.tenant_id, adjustment_id=world.adjustment_id
        )


async def test_i1_a_rebook_is_refused_while_the_old_event_may_still_be_there(db, provider, cal):
    """I1: `finish_rebook` only from `old_slot='released'` - a rebook with the old event still on
    the agenda (NULL: the vacate never ran) would leave it there forever."""
    world = await _change(db, status=STATUS_AWAITING_CHOICE)

    assert await _finish(db, world) is False

    assert cal.events["evt-1"]["status"] == "confirmed"
    adjustment = await _adj(db, world)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_AWAITING_CHOICE, None)
    assert (await _row(db, world)).google_event_id == "evt-1"


async def test_i1_ensure_old_event_gone_claims_and_deletes_a_never_vacated_event(
    db, provider, cal
):
    world = await _change(db, status=STATUS_AWAITING_CHOICE)

    assert await _ensure(db, world) is True

    assert cal.events["evt-1"]["status"] == "cancelled"
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASED
    assert await _finish(db, world) is True
    async with db() as session:
        assert await old_slot.retry_vacates(session, now=NOW + timedelta(days=2)) == 0
    assert cal.cancelled == ["evt-1"]


async def test_i1_a_lost_slot_still_releasing_is_finished_before_the_hub_rebooks(
    db, provider, cal
):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASING)
    assert await _lose(db, world) is True
    assert await _finish(db, world, source="hub") is False  # still releasing: refused

    assert await _ensure(db, world) is True
    assert await _finish(db, world, source="hub") is True

    assert cal.events["evt-1"]["status"] == "cancelled"
    adjustment = await _adj(db, world)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_REBOOKED, OLD_SLOT_REBOOKED)


async def test_i1_ensure_with_the_agenda_down_is_false_and_changes_nothing_else(
    db, provider, cal
):
    world = await _change(db, status=STATUS_OLD_SLOT_LOST, old_slot_state=OLD_SLOT_RELEASING)
    cal.fail_cancel = True

    assert await _ensure(db, world) is False

    adjustment = await _adj(db, world)
    assert (adjustment.status, adjustment.old_slot) == (STATUS_OLD_SLOT_LOST, OLD_SLOT_RELEASING)
    assert await _finish(db, world, source="hub") is False


@pytest.mark.parametrize(
    "kw",
    [
        {"status": STATUS_KEPT_OLD, "old_slot_state": OLD_SLOT_RESTORED},
        {"status": STATUS_APPLIED, "old_slot_state": OLD_SLOT_REPLACED},
        {"status": STATUS_REBOOKED, "old_slot_state": OLD_SLOT_REBOOKED},
        {"mode": "legacy", "status": STATUS_AWAITING_CHOICE},
    ],
)
async def test_i1_ensure_never_touches_a_change_that_is_over(db, provider, cal, kw):
    world = await _change(db, **kw)
    assert await _ensure(db, world) is False
    assert cal.cancelled == []


async def test_i1_ensure_of_another_clinic_is_false(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE)
    assert await _ensure(db, world, tenant_id=uuid4()) is False
    assert cal.cancelled == [] and (await _adj(db, world)).old_slot is None


async def test_i1_ensure_on_a_released_change_is_true_without_agenda_calls(db, provider, cal):
    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    cal.fail_cancel = True
    assert await _ensure(db, world) is True


async def test_m2_finish_rebook_locks_the_consultation_before_the_change(db, provider, cal):
    """M2: lock order consultation -> adjustment, whatever the caller flushed before."""
    from sqlalchemy import event

    world = await _change(db, status=STATUS_AWAITING_CHOICE, old_slot_state=OLD_SLOT_RELEASED)
    engine = db.kw["bind"].sync_engine
    statements: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement.lstrip().split("\n")[0].upper())

    async with db() as session:
        row = await session.get(Appointment, world.appointment_id)
        event.listen(engine, "before_cursor_execute", record)
        try:
            assert await old_slot.finish_rebook(
                session, tenant_id=world.tenant_id, adjustment_id=world.adjustment_id,
                appointment=row, event_id="difmovenew", event_link=None,
                now=NOW + timedelta(days=1), source="patient",
            ) is True
        finally:
            event.remove(engine, "before_cursor_execute", record)
        await session.commit()
    first = next(s for s in statements if "APPOINTMENTS" in s or "DEPOSIT_ADJUSTMENTS" in s)
    assert first.startswith("SELECT") and "APPOINTMENTS" in first


async def _place(db, world, *, row_status=None):
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant_id)
        row = await session.get(Appointment, world.appointment_id)
        adjustment = await session.get(DepositAdjustment, world.adjustment_id)
        row.appointment_type = "Avaliação"
        row.start_at, row.end_at = world.new_start, world.new_start + timedelta(minutes=60)
        if row_status is not None:
            row.status = row_status
        await session.execute(
            update(DepositAdjustment)
            .where(DepositAdjustment.id == world.adjustment_id)
            .values(status=STATUS_APPLIED)
        )
        await session.commit()
        await session.refresh(adjustment)
        await old_slot.place_new_event_after_payment(
            session, tenant=tenant, appointment=row, adjustment=adjustment, patient_name="M"
        )


async def _cancel_row(db, world):
    async with db() as other:
        await other.execute(
            update(Appointment)
            .where(Appointment.id == world.appointment_id)
            .values(status=AppointmentStatus.CANCELLED)
        )
        await other.commit()


async def test_i2_a_consultation_cancelled_while_the_event_is_created_keeps_no_event(
    db, provider, cal
):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")

    async def cancel_meanwhile():
        await _cancel_row(db, world)

    cal.on_create = cancel_meanwhile

    await _place(db, world)  # nothing to place: no error for the caller

    own = old_slot.paid_event_id(world.adjustment_id, calendar=cal)
    row = await _row(db, world)
    assert row.status == AppointmentStatus.CANCELLED and row.google_event_id != own
    assert cal.events[own]["status"] == "cancelled"
    assert [e for e in cal.events.values() if e["status"] == "confirmed"] == []


async def test_i2_a_consultation_already_cancelled_gets_no_event_at_all(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")

    await _place(db, world, row_status=AppointmentStatus.CANCELLED)

    assert cal.created == []


async def test_i2_a_lost_claim_after_the_create_discards_the_event_and_raises(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")

    async def moved_meanwhile():
        async with db() as other:
            await other.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == world.adjustment_id)
                .values(old_slot=OLD_SLOT_RESTORED)
            )
            await other.commit()

    cal.on_create = moved_meanwhile

    with pytest.raises(RuntimeError):
        await _place(db, world)

    own = old_slot.paid_event_id(world.adjustment_id, calendar=cal)
    assert cal.events[own]["status"] == "cancelled"
    assert (await _row(db, world)).google_event_id == "evt-1"


async def test_i2_a_twin_hook_that_placed_the_same_event_keeps_it(db, provider, cal):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")
    own = old_slot.paid_event_id(world.adjustment_id, calendar=cal)

    async def twin_placed():
        async with db() as other:
            await other.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == world.adjustment_id)
                .values(old_slot=OLD_SLOT_REPLACED)
            )
            await other.execute(
                update(Appointment)
                .where(Appointment.id == world.appointment_id)
                .values(google_event_id=own)
            )
            await other.commit()

    cal.on_create = twin_placed

    await _place(db, world)

    assert cal.events[own]["status"] == "confirmed"
    assert (await _row(db, world)).google_event_id == own


async def test_i2_a_failing_commit_discards_the_created_event(db, provider, cal, monkeypatch):
    world = await _change(db, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")
    from sqlalchemy.ext.asyncio import AsyncSession

    real_commit = AsyncSession.commit
    armed = {"on": False}

    async def commit(self):
        if armed["on"]:
            armed["on"] = False
            raise ConnectionError("db blip")
        return await real_commit(self)

    async def arm():
        armed["on"] = True

    cal.on_create = arm
    monkeypatch.setattr(AsyncSession, "commit", commit)

    with pytest.raises(ConnectionError):
        await _place(db, world)

    own = old_slot.paid_event_id(world.adjustment_id, calendar=cal)
    assert cal.events[own]["status"] == "cancelled"
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASED


async def test_i3_the_old_event_is_deleted_on_its_own_agenda_even_if_paid_meanwhile(
    db, provider, monkeypatch
):
    """I3: the payment (a change of DOCTOR) commits between the vacate's reads. The old event is
    deleted on the OLD doctor's agenda (read under the lock), never looked up on the new one."""
    from sqlalchemy.ext.asyncio import AsyncSession

    world = await _change(db)
    clinic, doctor_cal = _Calendar(), _Calendar()
    clinic.add("evt-1")
    async with db() as session:
        doctor = Professional(id=uuid4(), tenant_id=world.tenant_id, name="Dr. B", is_active=True)
        session.add(doctor)
        await session.commit()
        doctor_id = doctor.id

    async def resolve(session, tenant, owner):
        return doctor_cal if owner.professional_id == doctor_id else clinic

    monkeypatch.setattr(service_change, "calendar_for_appointment", resolve)
    original_get = AsyncSession.get
    fired = {"done": False}

    async def get(self, entity, ident, **kw):
        if entity is Tenant and not fired["done"]:
            current = await original_get(self, DepositAdjustment, world.adjustment_id)
            if current is not None and current.old_slot == OLD_SLOT_RELEASING:
                fired["done"] = True
                async with db() as pay:
                    await pay.execute(
                        update(DepositAdjustment)
                        .where(DepositAdjustment.id == world.adjustment_id)
                        .values(status=STATUS_APPLIED)
                    )
                    await pay.execute(
                        update(Appointment)
                        .where(Appointment.id == world.appointment_id)
                        .values(professional_id=doctor_id)
                    )
                    await pay.commit()
        return await original_get(self, entity, ident, **kw)

    monkeypatch.setattr(AsyncSession, "get", get)
    await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id)
    monkeypatch.setattr(AsyncSession, "get", original_get)
    assert fired["done"]
    async with db() as session:
        tenant = await session.get(Tenant, world.tenant_id)
        row = await session.get(Appointment, world.appointment_id)
        adjustment = await session.get(DepositAdjustment, world.adjustment_id)
        await old_slot.place_new_event_after_payment(
            session, tenant=tenant, appointment=row, adjustment=adjustment, patient_name="M"
        )

    assert clinic.events["evt-1"]["status"] == "cancelled"
    assert "evt-1" not in doctor_cal.cancelled
    own = old_slot.paid_event_id(world.adjustment_id, calendar=doctor_cal)
    assert [c["id"] for c in doctor_cal.created] == [own]
    assert (await _adj(db, world)).old_slot == OLD_SLOT_REPLACED


async def test_i3_the_release_is_not_recorded_once_the_change_left_the_suspension(
    db, provider, cal
):
    """The final `releasing -> released` is guarded by the status too: a change paid meanwhile
    keeps `releasing`, so the paid path still owns (and deletes) the old event."""
    world = await _change(db)

    async def paid_meanwhile():
        async with db() as other:
            await other.execute(
                update(DepositAdjustment)
                .where(DepositAdjustment.id == world.adjustment_id)
                .values(status=STATUS_APPLIED)
            )
            await other.commit()

    original = cal.cancel_event

    async def cancel_then_pay(event_id):
        result = await original(event_id)
        await paid_meanwhile()
        return result

    cal.cancel_event = cancel_then_pay

    assert await old_slot.vacate_old_slot(world.tenant_id, world.adjustment_id) == (
        "not_applicable"
    )
    assert (await _adj(db, world)).old_slot == OLD_SLOT_RELEASING


async def test_m1_a_transient_error_after_the_restore_event_is_created_is_recovered(
    db, provider, cal, monkeypatch
):
    """M1: a DB blip in the transition keeps the created event; the next attempt adopts the
    same deterministic id (never a dead one) and the old consultation comes back."""
    world = await _change(db, status=STATUS_RESTORING, old_slot_state=OLD_SLOT_RELEASED)
    cal.events.pop("evt-1")
    real = booking_hold.release_difference_hold_in
    calls = {"n": 0}

    async def flaky(session, adjustment_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("db blip")
        return await real(session, adjustment_id)

    monkeypatch.setattr(booking_hold, "release_difference_hold_in", flaky)
    outcomes = [await _restore(db, world, trigger="no_room") for _ in range(2)]

    assert outcomes == ["calendar_unavailable", "restored"]
    own = old_slot.restore_event_id(world.adjustment_id, calendar=cal)
    assert cal.events[own]["status"] == "confirmed" and own not in cal.cancelled
    assert (await _row(db, world)).google_event_id == own
    assert len(cal.created) == 1
