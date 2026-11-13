# ruff: noqa: F811
"""TASK-083 spec §7 items 1-2: one person's agenda shared by two clinics, on the native agenda.

Clinic P and clinic C each have a `professionals` row for Dr. X; both rows point at ONE
`agendas` row. The calendars of each clinic are built the production way
(`resolve_professional_calendar` / `CalendarService.from_tenant_config`), so these tests also
prove WHERE the shared busy time is wired in:

* reads - the CalendarService gets a `shared_busy_provider` (only when the professional has an
  agenda WITH a sibling) and every read the booking paths use sees the sibling's busy windows:
  `check_availability`, `has_conflict`, `is_slot_free`, `list_free_slots`,
  `list_available_days`, and `slot_taken_at_confirm` on top of them;
* writes - the local exclusive write takes the AGENDA-wide lock and re-checks the sibling's
  busy time inside it, so two clinics can never both book the person for one window.

Only bare windows ever cross: the sibling's item has no id and no summary.
Without an agenda (or alone on it) nothing changes and no provider exists
(tests/test_agenda_characterization.py pins the rest).
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from secretaria.models import (
    Agenda,
    Appointment,
    AppointmentStatus,
    BookingHold,
    CalendarEvent,
    Professional,
    Tenant,
)
from secretaria.services import local_calendar
from secretaria.services.agenda import agenda_lock_key
from secretaria.services.availability import free_slots_for_day, slot_taken_at_confirm
from secretaria.services.calendar import CalendarService, CalendarSlotTakenError
from secretaria.services.local_calendar import (
    LocalCalendarService,
    create_event_exclusive,
    update_event_exclusive,
)
from secretaria.services.tenant_config import load_tenant_config, resolve_professional_calendar
from tests._local_agenda import (
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda as _wire_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):
    _wire_agenda(monkeypatch, db)


# --------------------------------------------------------------------------
# World
# --------------------------------------------------------------------------


async def _link(db, agenda_key: str, *professionals) -> object:
    async with db() as session:
        row = Agenda(person_key=agenda_key)
        session.add(row)
        await session.flush()
        for professional in professionals:
            (await session.get(Professional, professional.id)).agenda_id = row.id
        await session.commit()
        return row.id


async def _world(db):
    """P's Dr. X and C's Dr. X share one agenda; each clinic has only him active."""
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    agenda_id = await _link(db, "acct-x", x_p, x_c)
    return tenant_p, tenant_c, x_p, x_c, agenda_id


async def _calendar(db, tenant, professional) -> CalendarService:
    """The professional's agenda exactly as the booking paths resolve it."""
    async with db() as session:
        tenant_row = await session.get(Tenant, tenant.id)
        professional_row = await session.get(Professional, professional.id)
        return await resolve_professional_calendar(session, tenant_row, professional_row)


async def _clinic_calendar(db, tenant) -> CalendarService:
    """The clinic's own agenda (a clinic with one active doctor: his)."""
    async with db() as session:
        config = await load_tenant_config(session, await session.get(Tenant, tenant.id))
    return CalendarService.from_tenant_config(config)


async def _add(db, *rows):
    async with db() as session:
        session.add_all(rows)
        await session.commit()


def _window(hour: int = 10):
    start = tomorrow_at(hour)
    return start, start + timedelta(minutes=30)


async def _labels(calendar, start) -> list[str]:
    day = start.astimezone(calendar.tzinfo)
    return [s["label"] for s in await calendar.list_free_slots(day=day, max_slots=40)]


def _hold(tenant, professional, start, end, *, phase):
    return BookingHold(
        tenant_id=tenant.id,
        conversation_id=uuid4(),
        professional_id=professional.id,
        start_at=start,
        end_at=end,
        expires_at=datetime.now(UTC) + timedelta(minutes=20),
        phase=phase,
    )


async def _assert_c_sees_busy(calendar, start, end):
    assert await calendar.has_conflict(start, end)
    assert not await calendar.is_slot_free(start, end)
    assert await slot_taken_at_confirm(calendar, start, end, source="shared_agenda_test")
    assert "10:00" not in await _labels(calendar, start)
    starts = await free_slots_for_day(
        calendar, day=start.astimezone(calendar.tzinfo).date(), duration_minutes=30
    )
    assert start not in starts


async def _assert_c_sees_free(calendar, start, end):
    assert not await calendar.has_conflict(start, end)
    assert await calendar.is_slot_free(start, end)
    assert not await slot_taken_at_confirm(calendar, start, end, source="shared_agenda_test")
    assert "10:00" in await _labels(calendar, start)


# --------------------------------------------------------------------------
# Reads: the sibling's busy time reaches every booking read
# --------------------------------------------------------------------------


async def test_a_sibling_appointment_takes_the_slot_away_from_the_other_clinic(db):
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    await _add(
        db,
        Appointment(
            tenant_id=tenant_p.id,
            professional_id=x_p.id,
            google_event_id=f"evt_{uuid4().hex}",
            start_at=start,
            end_at=end,
            status=AppointmentStatus.SCHEDULED,
        ),
    )

    cal_c = await _calendar(db, tenant_c, x_c)
    assert cal_c.shared_busy_provider is not None
    await _assert_c_sees_busy(cal_c, start, end)


async def test_the_sibling_item_is_a_bare_window(db):
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    await (await _calendar(db, tenant_p, x_p)).create_event(start, end, "Maria Silva - retorno")

    cal_c = await _calendar(db, tenant_c, x_c)
    busy = await cal_c.check_availability(start - timedelta(hours=1), end + timedelta(hours=1))

    assert len(busy) == 1
    item = busy[0]
    assert item["id"] is None and item["summary"] is None
    assert datetime.fromisoformat(item["start"]) == start
    assert datetime.fromisoformat(item["end"]) == end
    assert "Maria" not in repr(busy)
    # The hub's own agenda listing is NOT the shared read: P's event is not C's to list.
    assert await cal_c.list_events(start - timedelta(hours=1), end + timedelta(hours=1)) == []


async def test_a_sibling_local_event_and_block_take_the_slot_and_cancel_frees_it(db):
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    cal_p = await _calendar(db, tenant_p, x_p)
    cal_c = await _calendar(db, tenant_c, x_c)

    event = await cal_p.create_event(start, end, "Consulta - P")
    await _assert_c_sees_busy(cal_c, start, end)

    assert await cal_p.cancel_event(event["id"]) is True
    await _assert_c_sees_free(cal_c, start, end)

    # A staff block at P (a plain, non-patient event) blocks C the same way.
    await cal_p.create_event(start - timedelta(minutes=30), end + timedelta(hours=1), "Bloqueado")
    await _assert_c_sees_busy(cal_c, start, end)


async def test_the_clinic_agenda_of_a_one_doctor_clinic_sees_the_sibling(db):
    tenant_p, tenant_c, x_p, _x_c, _agenda = await _world(db)
    start, end = _window()
    await (await _calendar(db, tenant_p, x_p)).create_event(start, end, "Consulta - P")

    cal_c = await _clinic_calendar(db, tenant_c)
    assert isinstance(cal_c, LocalCalendarService)
    assert cal_c.shared_busy_provider is not None
    await _assert_c_sees_busy(cal_c, start, end)


async def test_a_day_the_sibling_fills_is_not_an_available_day(db):
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, _ = _window(8)
    cal_p = await _calendar(db, tenant_p, x_p)
    await cal_p.create_event(start, start + timedelta(hours=10), "Congresso")  # 08:00-18:00

    cal_c = await _calendar(db, tenant_c, x_c)
    day = start.astimezone(cal_c.tzinfo).replace(hour=0, minute=0, second=0, microsecond=0)
    days = await cal_c.list_available_days(start_day=day, days=2, slot_minutes=30)
    assert day not in days
    assert day + timedelta(days=1) in days


async def test_a_payment_hold_of_the_sibling_blocks_a_code_hold_does_not(db):
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    cal_c = await _calendar(db, tenant_c, x_c)

    await _add(db, _hold(tenant_p, x_p, start, end, phase="code"))
    await _assert_c_sees_free(cal_c, start, end)

    await _add(db, _hold(tenant_p, x_p, start, end, phase="payment"))
    await _assert_c_sees_busy(cal_c, start, end)


async def test_a_different_agenda_does_not_interfere(db):
    tenant_p, tenant_c, x_p, _x_c, _agenda = await _world(db)
    z_p = await seed_professional(db, tenant_p, "Dra. Z")
    z_c = await seed_professional(db, tenant_c, "Dra. Z")
    await _link(db, "acct-z", z_p, z_c)
    start, end = _window()
    await (await _calendar(db, tenant_p, x_p)).create_event(start, end, "Consulta - X")

    cal_z_c = await _calendar(db, tenant_c, z_c)
    assert cal_z_c.shared_busy_provider is not None  # Z has a sibling too, just not X
    await _assert_c_sees_free(cal_z_c, start, end)
    booked = await create_event_exclusive(cal_z_c, start=start, end=end, summary="Consulta - Z")
    assert booked["id"]


async def test_the_google_backend_reads_the_sibling_windows_too(db, monkeypatch):
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    await (await _calendar(db, tenant_p, x_p)).create_event(start, end, "Consulta - P")
    async with db() as session:
        config = await load_tenant_config(session, await session.get(Tenant, tenant_c.id))
    google = CalendarService.from_tenant_config(replace(config, google_refresh_token="tok"))
    assert not isinstance(google, LocalCalendarService)
    assert google.shared_busy_provider is not None
    # Google itself knows nothing about P: its page is empty.
    monkeypatch.setattr(CalendarService, "_events_page", lambda *a, **k: ([], None))

    busy = await google.check_availability(start, end)
    assert [(i["id"], i["summary"]) for i in busy] == [(None, None)]
    assert await google.has_conflict(start, end)
    assert await slot_taken_at_confirm(google, start, end, source="shared_agenda_test")


# --------------------------------------------------------------------------
# No agenda, or alone on it: no provider at all (zero extra reads)
# --------------------------------------------------------------------------


async def test_without_an_agenda_or_without_a_sibling_there_is_no_provider(db):
    tenant = await seed_tenant(db)
    solo = await seed_professional(db, tenant, "Dr. Solo")
    assert (await _calendar(db, tenant, solo)).shared_busy_provider is None
    assert (await _clinic_calendar(db, tenant)).shared_busy_provider is None

    await _link(db, "acct-solo", solo)  # an agenda nobody else is on
    assert (await _calendar(db, tenant, solo)).shared_busy_provider is None
    assert (await _clinic_calendar(db, tenant)).shared_busy_provider is None

    async with db() as session:
        config = await load_tenant_config(session, await session.get(Tenant, tenant.id))
    assert config.shared_agenda_professional_ids == frozenset()


# --------------------------------------------------------------------------
# Writes: the exclusive write is atomic across the two clinics
# --------------------------------------------------------------------------


async def test_an_exclusive_write_is_refused_when_the_sibling_holds_the_window(db):
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    cal_p = await _calendar(db, tenant_p, x_p)
    cal_c = await _calendar(db, tenant_c, x_c)

    await create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P")
    with pytest.raises(CalendarSlotTakenError):
        await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")

    # ...and the other way round, on another window.
    later, later_end = _window(14)
    await create_event_exclusive(cal_c, start=later, end=later_end, summary="Consulta - C")
    with pytest.raises(CalendarSlotTakenError):
        await create_event_exclusive(cal_p, start=later, end=later_end, summary="Consulta - P")

    # A move of C's event onto P's window is refused as well.
    own = await create_event_exclusive(
        cal_c, start=start + timedelta(hours=2), end=end + timedelta(hours=2), summary="C2"
    )
    with pytest.raises(CalendarSlotTakenError):
        await update_event_exclusive(cal_c, own["id"], start=start, end=end)


async def test_the_exclusive_write_refuses_even_from_an_agenda_built_without_the_provider(db):
    """The write discovers the shared agenda itself: no factory path can skip the guard."""
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    await create_event_exclusive(
        await _calendar(db, tenant_p, x_p), start=start, end=end, summary="Consulta - P"
    )

    bare = LocalCalendarService(tenant_c.id, x_c.id)
    bare._tz = (await _calendar(db, tenant_c, x_c)).tzinfo
    assert bare.shared_busy_provider is None
    with pytest.raises(CalendarSlotTakenError):
        await create_event_exclusive(bare, start=start, end=end, summary="Consulta - C")


async def test_a_plain_write_stays_permissive_across_clinics(db):
    """Staff writes (encaixe/block) are not exclusive: unchanged by the shared agenda."""
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    await create_event_exclusive(
        await _calendar(db, tenant_p, x_p), start=start, end=end, summary="Consulta - P"
    )
    block = await (await _calendar(db, tenant_c, x_c)).create_event(start, end, "Encaixe")
    assert block["id"]


async def test_two_clinics_writing_one_window_at_once_have_exactly_one_winner(db):
    tenant_p, tenant_c, x_p, x_c, _agenda = await _world(db)
    start, end = _window()
    cal_p = await _calendar(db, tenant_p, x_p)
    cal_c = await _calendar(db, tenant_c, x_c)

    results = await asyncio.gather(
        create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P1"),
        create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C1"),
        create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P2"),
        create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C2"),
        return_exceptions=True,
    )

    won = [r for r in results if isinstance(r, dict)]
    lost = [r for r in results if isinstance(r, CalendarSlotTakenError)]
    assert len(won) == 1 and len(lost) == 3
    async with db() as session:
        live = await session.scalar(
            select(func.count())
            .select_from(CalendarEvent)
            .where(CalendarEvent.status == "confirmed")
        )
    assert live == 1


async def test_the_exclusive_write_holds_the_agenda_wide_lock(db, monkeypatch):
    """In process: the agenda's asyncio lock; across processes: the agenda advisory lock, on
    the SAME session (transaction) that checks and writes."""
    tenant_p, tenant_c, x_p, x_c, agenda_id = await _world(db)
    start, end = _window()
    cal_c = await _calendar(db, tenant_c, x_c)

    advisory = []

    async def _record(session, locked_agenda):
        advisory.append((session, locked_agenda))

    monkeypatch.setattr(local_calendar, "shared_agenda_write_lock", _record)

    lock = local_calendar._process_lock(agenda_lock_key(agenda_id))
    await lock.acquire()
    task = asyncio.create_task(
        create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # waiting for P's side of the person's agenda
    lock.release()
    event = await task

    assert event["id"]
    assert [locked for _session, locked in advisory] == [agenda_id]


async def test_a_no_agenda_exclusive_write_takes_no_agenda_lock(db, monkeypatch):
    tenant = await seed_tenant(db)
    solo = await seed_professional(db, tenant, "Dr. Solo")
    calls = []

    async def _record(session, locked_agenda):
        calls.append(locked_agenda)

    monkeypatch.setattr(local_calendar, "shared_agenda_write_lock", _record)
    start, end = _window()
    await create_event_exclusive(
        await _calendar(db, tenant, solo), start=start, end=end, summary="Consulta"
    )
    assert calls == []
