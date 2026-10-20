# ruff: noqa: F811
"""LocalCalendarService - the secretarIA's own agenda (native agenda, 2026-10-20).

Same interface and shapes as the Google CalendarService, over `calendar_events`:
busy/free reads, the inherited slot arithmetic, per-agenda isolation, exclusive
(atomic) patient writes, idempotent cancel, moves, and tenant isolation.
"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from secretaria.models.calendar_event import CalendarEvent
from secretaria.services import local_calendar
from secretaria.services.calendar import (
    CalendarEventNotFoundError,
    CalendarSlotTakenError,
    is_event_not_found,
    is_local_event_id,
)
from secretaria.services.local_calendar import (
    LocalCalendarService,
    create_event_exclusive,
    list_unpushed_events,
    update_event_exclusive,
)
from tests._local_agenda import (
    agenda,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda as _wire_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):
    _wire_agenda(monkeypatch, db)


async def _row(db, event_id) -> CalendarEvent | None:
    async with db() as session:
        return await session.get(CalendarEvent, event_id)


# --------------------------------------------------------------------------
# Shapes and busy/free
# --------------------------------------------------------------------------


async def test_create_returns_a_google_shaped_event_with_a_local_id(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)

    event = await cal.create_event(start, start + timedelta(minutes=30), "Consulta - Ana", "x")

    assert is_local_event_id(event["id"]) and event["id"].startswith("loc_")
    assert len(event["id"]) <= 255  # fits appointments.google_event_id
    assert event["status"] == "confirmed"
    assert event["htmlLink"] is None  # no Google page to open
    assert event["start"]["timeZone"] == "America/Sao_Paulo"
    assert event["start"]["dateTime"].endswith("-03:00")
    row = await _row(db, event["id"])
    assert row.tenant_id == tenant.id and row.professional_id is None
    assert row.google_event_id is None and row.busy is True


async def test_check_availability_lists_overlapping_busy_events_only(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    event = await cal.create_event(start, start + timedelta(minutes=30), "Consulta - Ana")

    busy = await cal.check_availability(start - timedelta(hours=1), start + timedelta(hours=1))
    assert busy == [
        {
            "id": event["id"],
            "summary": "Consulta - Ana",
            "start": event["start"]["dateTime"],
            "end": event["end"]["dateTime"],
        }
    ]
    # Half-open like Google: an event ending at 10:30 does not touch 10:30-11:00.
    after = start + timedelta(minutes=30)
    assert await cal.check_availability(after, after + timedelta(minutes=30)) == []
    assert await cal.list_events(start - timedelta(days=1), start + timedelta(days=1)) == busy


async def test_a_free_row_is_never_busy(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    event = await cal.create_event(start, start + timedelta(minutes=30), "Lembrete")
    async with db() as session, session.begin():
        await session.execute(
            update(CalendarEvent).where(CalendarEvent.id == event["id"]).values(busy=False)
        )

    assert await cal.check_availability(start, start + timedelta(minutes=30)) == []
    assert not await cal.has_conflict(start, start + timedelta(minutes=30))


async def test_the_inherited_slot_walk_skips_a_busy_slot(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    await cal.create_event(start, start + timedelta(minutes=30), "Consulta - Ana")

    slots = await cal.list_free_slots(day=start.astimezone(cal.tzinfo), max_slots=20)
    labels = [slot["label"] for slot in slots]
    assert "09:30" in labels and "10:30" in labels
    assert "10:00" not in labels
    assert await cal.has_conflict(start, start + timedelta(minutes=30))
    assert not await cal.is_slot_free(start, start + timedelta(minutes=30))
    assert await cal.is_slot_free(start + timedelta(hours=1), start + timedelta(minutes=90))


async def test_each_agenda_is_its_own(db):
    """A doctor's booking never blocks the clinic's agenda or a colleague's."""
    tenant = await seed_tenant(db)
    ana = await seed_professional(db, tenant, "Dra. Ana")
    bia = await seed_professional(db, tenant, "Dra. Bia")
    start = tomorrow_at(10)
    window = (start, start + timedelta(minutes=30))
    await agenda(tenant.id, ana.id).create_event(*window, "Consulta - Paciente")

    assert await agenda(tenant.id, ana.id).has_conflict(*window)
    assert not await agenda(tenant.id, bia.id).has_conflict(*window)
    assert not await agenda(tenant.id).has_conflict(*window)
    assert agenda(tenant.id, ana.id).references_same_calendar(agenda(tenant.id, ana.id))
    assert not agenda(tenant.id, ana.id).references_same_calendar(agenda(tenant.id, bia.id))
    assert not agenda(tenant.id, ana.id).references_same_calendar(agenda(tenant.id))


async def test_another_clinic_never_sees_or_touches_the_event(db):
    tenant = await seed_tenant(db)
    other = await seed_tenant(db, name="Outra")
    start = tomorrow_at(10)
    event = await agenda(tenant.id).create_event(start, start + timedelta(minutes=30), "X")

    assert await agenda(other.id).check_availability(start, start + timedelta(hours=1)) == []
    assert await agenda(other.id).cancel_event(event["id"]) is False
    with pytest.raises(CalendarEventNotFoundError):
        await agenda(other.id).update_event(event["id"], start, start + timedelta(hours=1))
    assert await agenda(other.id).get_event(event["id"]) is None
    assert (await _row(db, event["id"])).status == "confirmed"


# --------------------------------------------------------------------------
# Exclusive writes: two patients never get one slot
# --------------------------------------------------------------------------


async def test_an_exclusive_create_refuses_an_overlap_and_allows_adjacent(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    await cal.create_event(start, start + timedelta(minutes=30), "Primeira")

    with pytest.raises(CalendarSlotTakenError):
        await cal.create_event(
            start + timedelta(minutes=15), start + timedelta(minutes=45), "B", exclusive=True
        )
    adjacent = await cal.create_event(
        start + timedelta(minutes=30), start + timedelta(minutes=60), "C", exclusive=True
    )
    assert adjacent["id"]


async def test_a_plain_create_may_overlap_like_google(db):
    """Staff writes stay permissive: an encaixe or a block over a booking is allowed."""
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    await cal.create_event(start, start + timedelta(minutes=30), "Consulta")

    block = await cal.create_event(start, start + timedelta(hours=2), "Bloqueado")

    assert block["id"]
    assert len(await cal.check_availability(start, start + timedelta(hours=2))) == 2


async def test_two_concurrent_exclusive_bookings_of_one_slot_have_one_winner(db):
    tenant = await seed_tenant(db)
    start = tomorrow_at(10)
    end = start + timedelta(minutes=30)

    async def _book(name):
        return await create_event_exclusive(
            agenda(tenant.id), start=start, end=end, summary=f"Consulta - {name}"
        )

    results = await asyncio.gather(*(_book(n) for n in "ABCD"), return_exceptions=True)

    won = [r for r in results if isinstance(r, dict)]
    lost = [r for r in results if isinstance(r, CalendarSlotTakenError)]
    assert len(won) == 1 and len(lost) == 3
    assert len(await agenda(tenant.id).check_availability(start, end)) == 1


async def test_the_exclusive_helpers_are_plain_calls_on_any_other_calendar():
    calls = []

    class _Google:
        async def create_event(self, **kwargs):
            calls.append(("create", kwargs))
            return {"id": "g1"}

        async def update_event(self, event_id, **kwargs):
            calls.append(("update", event_id, kwargs))
            return {"id": event_id}

    start = tomorrow_at(10)
    await create_event_exclusive(_Google(), start=start, end=start, summary="S")
    await update_event_exclusive(_Google(), "g1", start=start, end=start)
    assert calls == [
        ("create", {"start": start, "end": start, "summary": "S", "description": ""}),
        ("update", "g1", {"start": start, "end": start}),
    ]


async def test_a_postgres_session_takes_the_cross_process_advisory_lock():
    executed = []

    class _Session:
        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        async def execute(self, statement, params):
            executed.append((str(statement), params))

    key = local_calendar._agenda_key(uuid4(), None)
    await local_calendar._advisory_lock(_Session(), key)

    assert executed == [
        ("SELECT pg_advisory_xact_lock(:key)", {"key": local_calendar._advisory_key(key)})
    ]
    # Stable across processes (the API and the worker must collide on the same key).
    assert local_calendar._advisory_key(key) == local_calendar._advisory_key(key)
    assert -(2**63) <= local_calendar._advisory_key(key) < 2**63


# --------------------------------------------------------------------------
# Cancel, move, get
# --------------------------------------------------------------------------


async def test_cancel_is_idempotent_and_frees_the_slot(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    window = (start, start + timedelta(minutes=30))
    event = await cal.create_event(*window, "Consulta")

    assert await cal.cancel_event(event["id"]) is True
    assert await cal.cancel_event(event["id"]) is False  # already cancelled
    assert await cal.cancel_event("loc_" + "0" * 32) is False  # never existed
    assert not await cal.has_conflict(*window)
    assert (await cal.get_event(event["id"]))["status"] == "cancelled"
    # The slot can be booked again, exclusively.
    assert (await cal.create_event(*window, "Outra", exclusive=True))["id"]


async def test_cancel_reaches_the_event_through_any_agenda_of_the_clinic(db):
    """Ids are unique: an action is never lost to an owner that drifted (1 -> 2 doctors)."""
    tenant = await seed_tenant(db)
    ana = await seed_professional(db, tenant, "Dra. Ana")
    start = tomorrow_at(10)
    event = await agenda(tenant.id, ana.id).create_event(start, start + timedelta(minutes=30), "X")

    assert await agenda(tenant.id).cancel_event(event["id"]) is True


async def test_move_and_rewrite(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    event = await cal.create_event(start, start + timedelta(minutes=30), "Consulta - Ana")
    new_start = start + timedelta(hours=2)

    moved = await cal.update_event(event["id"], new_start, new_start + timedelta(minutes=30))
    assert moved["id"] == event["id"]
    assert not await cal.has_conflict(start, start + timedelta(minutes=30))
    assert await cal.has_conflict(new_start, new_start + timedelta(minutes=30))

    await cal.update_event_details(
        event["id"], new_start, new_start + timedelta(minutes=45), "Retorno - Ana", "Convênio: X"
    )
    row = await _row(db, event["id"])
    assert (row.summary, row.description) == ("Retorno - Ana", "Convênio: X")


async def test_an_exclusive_move_refuses_a_taken_time_but_never_its_own(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    mine = await cal.create_event(start, start + timedelta(minutes=30), "Minha")
    other_start = start + timedelta(hours=1)
    await cal.create_event(other_start, other_start + timedelta(minutes=30), "Outra")

    with pytest.raises(CalendarSlotTakenError):
        await update_event_exclusive(
            cal, mine["id"], start=other_start, end=other_start + timedelta(minutes=30)
        )
    # Overlapping only ITSELF (a 15-minute shift) is fine.
    shifted = start + timedelta(minutes=15)
    await update_event_exclusive(
        cal, mine["id"], start=shifted, end=shifted + timedelta(minutes=30)
    )
    assert (await _row(db, mine["id"])).start_at.replace(tzinfo=None) == shifted.replace(
        tzinfo=None
    )


async def test_moving_a_missing_or_cancelled_event_is_a_404(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    event = await cal.create_event(start, start + timedelta(minutes=30), "Consulta")
    await cal.cancel_event(event["id"])

    for event_id in (event["id"], "loc_" + "f" * 32):
        with pytest.raises(CalendarEventNotFoundError) as caught:
            await cal.update_event(event_id, start, start + timedelta(minutes=30))
        # The existing "not on this agenda" handling recognises it as Google's 404.
        assert is_event_not_found(caught.value)


async def test_an_event_ending_before_it_starts_is_refused(db):
    tenant = await seed_tenant(db)
    start = tomorrow_at(10)
    with pytest.raises(ValueError):
        await agenda(tenant.id).create_event(start, start - timedelta(minutes=1), "X")


async def test_no_invite_and_no_google_client(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    event = await cal.create_event(start, start + timedelta(minutes=30), "Consulta")

    assert await cal.add_attendee(event["id"], "paciente@example.com") == {"id": event["id"]}
    with pytest.raises(RuntimeError):
        cal._client()


# --------------------------------------------------------------------------
# Handed over to Google
# --------------------------------------------------------------------------


async def test_a_pushed_event_stops_counting_here(db):
    tenant = await seed_tenant(db)
    cal = agenda(tenant.id)
    start = tomorrow_at(10)
    window = (start, start + timedelta(minutes=30))
    event = await cal.create_event(*window, "Consulta")
    async with db() as session, session.begin():
        await session.execute(
            update(CalendarEvent)
            .where(CalendarEvent.id == event["id"])
            .values(google_event_id="sec" + event["id"][4:])
        )

    assert await cal.check_availability(*window) == []
    assert await cal.cancel_event(event["id"]) is False
    async with db() as session:
        assert await list_unpushed_events(session, tenant.id, {None}, *window, cal.tzinfo) == []


async def test_unpushed_events_are_listed_for_the_owners_asked(db):
    tenant = await seed_tenant(db)
    ana = await seed_professional(db, tenant, "Dra. Ana")
    start = tomorrow_at(10)
    window = (start, start + timedelta(minutes=30))
    clinic_event = await agenda(tenant.id).create_event(*window, "Clínica")
    ana_event = await agenda(tenant.id, ana.id).create_event(*window, "Ana")

    tz = agenda(tenant.id).tzinfo
    async with db() as session:
        only_ana = await list_unpushed_events(session, tenant.id, {ana.id}, *window, tz)
        both = await list_unpushed_events(session, tenant.id, {None, ana.id}, *window, tz)
        nobody = await list_unpushed_events(session, tenant.id, set(), *window, tz)
    assert [e["id"] for e in only_ana] == [ana_event["id"]]
    assert {e["id"] for e in both} == {clinic_event["id"], ana_event["id"]}
    assert nobody == []


async def test_rows_are_stored_in_utc(db):
    tenant = await seed_tenant(db)
    start = tomorrow_at(10)
    local_naive = start.astimezone(agenda(tenant.id).tzinfo).replace(tzinfo=None)
    # A naive time is the clinic's wall clock, exactly like the Google service.
    event = await agenda(tenant.id).create_event(
        local_naive, local_naive + timedelta(minutes=30), "X"
    )
    async with db() as session:
        stored = await session.scalar(
            select(CalendarEvent.start_at).where(CalendarEvent.id == event["id"])
        )
    assert stored.replace(tzinfo=None) == start.replace(tzinfo=None)
    assert isinstance(agenda(tenant.id), LocalCalendarService)
