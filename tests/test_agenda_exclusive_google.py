# ruff: noqa: F811
"""TASK-083 spec §7 item 3: the exclusive patient write on a GOOGLE agenda of a shared person.

Clinic P and clinic C each have a `professionals` row for Dr. X on ONE `agendas` row. Here
both clinics (or one of them) keep the agenda on Google - two different Google accounts, so
neither clinic's Google can see the other's events. The exclusive write
(`CalendarService.exclusive_write`, reached through `create_event_exclusive` & co.) takes the
person's agenda lock, re-checks the siblings' busy time and its own Google calendar, writes,
and leaves a short LEASE (a bare window in `calendar_events`) so the sibling sees the booking
before the caller's appointment row lands. Without a shared agenda the Google write is the
plain call it always was (tests/test_agenda_characterization.py pins the rest).
"""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from googleapiclient.errors import HttpError
from sqlalchemy import event, select, update

from secretaria.models import Agenda, Appointment, AppointmentStatus, CalendarEvent, Professional
from secretaria.models.calendar_event import EVENT_STATUS_GOOGLE_LEASE
from secretaria.models.tenant import Tenant
from secretaria.services import agenda as agenda_service, local_calendar
from secretaria.services.agenda import agenda_lock_key, shared_busy
from secretaria.services.calendar import (
    CalendarService,
    CalendarSlotTakenError,
    CalendarUnavailableError,
)
from secretaria.services.local_calendar import (
    LocalCalendarService,
    create_event_exclusive,
    list_unpushed_events,
    update_event_details_exclusive,
    update_event_exclusive,
)
from secretaria.services.tenant_config import load_tenant_config
from tests._google_fake import install_google_fake
from tests._local_agenda import (
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: F401


@pytest.fixture(autouse=True)
def _wire(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


@pytest.fixture
def google(monkeypatch):
    return install_google_fake(monkeypatch)


# --------------------------------------------------------------------------
# World
# --------------------------------------------------------------------------


async def _link(db, key: str, *professionals):
    async with db() as session:
        row = Agenda(person_key=key)
        session.add(row)
        await session.flush()
        for professional in professionals:
            (await session.get(Professional, professional.id)).agenda_id = row.id
        await session.commit()
        return row.id


async def _world(db):
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    agenda_id = await _link(db, "acct-x", x_p, x_c)
    return tenant_p, tenant_c, x_p, x_c, agenda_id


async def _config(db, tenant):
    async with db() as session:
        return await load_tenant_config(session, await session.get(Tenant, tenant.id))


async def _google(db, tenant, professional, token: str) -> CalendarService:
    """The professional's GOOGLE agenda, built by the production factory."""
    config = await _config(db, tenant)
    calendar = CalendarService.from_tenant_config(
        replace(config, google_refresh_token=token), professional_id=professional.id
    )
    assert not isinstance(calendar, LocalCalendarService)
    return calendar


async def _local(db, tenant, professional) -> LocalCalendarService:
    calendar = CalendarService.from_tenant_config(
        await _config(db, tenant), professional_id=professional.id
    )
    assert isinstance(calendar, LocalCalendarService)
    return calendar


def _window(hour: int = 10):
    start = tomorrow_at(hour)
    return start, start + timedelta(minutes=30)


async def _appointment(db, tenant, professional, start, end, *, status=AppointmentStatus.SCHEDULED):
    async with db() as session:
        row = Appointment(
            tenant_id=tenant.id,
            professional_id=professional.id,
            google_event_id="evt-sibling",
            appointment_type="Consulta",
            start_at=start,
            end_at=end,
            status=status,
        )
        session.add(row)
        await session.commit()
        return row


async def _rows(db, status: str | None = None) -> list[CalendarEvent]:
    async with db() as session:
        stmt = select(CalendarEvent)
        if status is not None:
            stmt = stmt.where(CalendarEvent.status == status)
        return list(await session.scalars(stmt))


def _statements(db):
    engine = db.kw["bind"].sync_engine
    seen: list[str] = []

    def _record(conn, cursor, statement, params, context, executemany):
        seen.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    return seen, lambda: event.remove(engine, "before_cursor_execute", _record)


# --------------------------------------------------------------------------
# The capability
# --------------------------------------------------------------------------


async def test_google_writes_exclusively_only_with_a_shared_agenda(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    solo = await seed_professional(db, tenant_c, "Dr. Solo")

    assert (await _google(db, tenant_c, x_c, "tok-c")).supports_exclusive_write is True
    assert (await _google(db, tenant_c, solo, "tok-c")).supports_exclusive_write is False
    assert (await _local(db, tenant_c, solo)).supports_exclusive_write is True
    assert (await _local(db, tenant_c, x_c)).supports_exclusive_write is True
    assert CalendarService().supports_exclusive_write is False  # bare env service (dev)

    view = (await _google(db, tenant_c, x_c, "tok-c")).own_agenda_view()
    assert view.supports_exclusive_write is False
    assert view.shared_busy_provider is None


# --------------------------------------------------------------------------
# Google <-> Google
# --------------------------------------------------------------------------


async def test_a_google_exclusive_create_is_refused_over_a_sibling_booking(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    await _appointment(db, tenant_p, x_p, start, end)
    cal_c = await _google(db, tenant_c, x_c, "tok-c")

    with pytest.raises(CalendarSlotTakenError):
        await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")
    assert google.events_of("tok-c") == []  # nothing written on C's Google

    # A cancelled sibling booking frees it again.
    async with db() as session, session.begin():
        await session.execute(update(Appointment).values(status=AppointmentStatus.CANCELLED))
    booked = await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")
    assert [e["id"] for e in google.events_of("tok-c")] == [booked["id"]]


async def test_the_google_exclusive_write_also_rechecks_its_own_google(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    google.put("tok-c", start, end)  # the doctor typed it straight into Google

    with pytest.raises(CalendarSlotTakenError):
        await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")
    # The reservation made before that Google read is dropped again: P may book at once.
    assert await _rows(db, EVENT_STATUS_GOOGLE_LEASE) == []
    cal_p = await _google(db, tenant_p, x_p, "tok-p")
    assert await create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P")


async def test_two_google_clinics_writing_one_window_at_once_have_exactly_one_winner(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    cal_p = await _google(db, tenant_p, x_p, "tok-p")
    cal_c = await _google(db, tenant_c, x_c, "tok-c")

    results = await asyncio.gather(
        create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P1"),
        create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C1"),
        create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P2"),
        create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C2"),
        return_exceptions=True,
    )

    won = [r for r in results if isinstance(r, dict)]
    lost = [r for r in results if isinstance(r, CalendarSlotTakenError)]
    assert len(won) == 1 and len(lost) == 3, results
    assert len(google.inserts) == 1


async def test_a_google_move_onto_a_sibling_window_is_refused(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    later, later_end = _window(14)
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    own = await create_event_exclusive(cal_c, start=later, end=later_end, summary="Consulta - C")
    await _appointment(db, tenant_p, x_p, start, end)

    with pytest.raises(CalendarSlotTakenError):
        await update_event_exclusive(cal_c, own["id"], start=start, end=end)
    with pytest.raises(CalendarSlotTakenError):
        await update_event_details_exclusive(cal_c, own["id"], start, end, "Retorno - C")
    # Its own event never blocks its own move (a longer window over itself).
    await update_event_exclusive(
        cal_c, own["id"], start=later, end=later_end + timedelta(minutes=30)
    )
    assert google.patches == [("tok-c", own["id"])]


# --------------------------------------------------------------------------
# Google <-> local (each clinic its own backend)
# --------------------------------------------------------------------------


async def test_mixed_backends_refuse_each_other_both_ways(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    cal_p = await _google(db, tenant_p, x_p, "tok-p")
    cal_c = await _local(db, tenant_c, x_c)

    start, end = _window()
    await create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P")
    with pytest.raises(CalendarSlotTakenError):  # local sees P's Google booking (lease)
        await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")

    later, later_end = _window(14)
    await create_event_exclusive(cal_c, start=later, end=later_end, summary="Consulta - C")
    with pytest.raises(CalendarSlotTakenError):  # Google sees C's local event
        await create_event_exclusive(cal_p, start=later, end=later_end, summary="Consulta - P")


async def test_mixed_backends_writing_at_once_have_exactly_one_winner(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    cal_p = await _google(db, tenant_p, x_p, "tok-p")
    cal_c = await _local(db, tenant_c, x_c)
    start, end = _window()

    results = await asyncio.gather(
        create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C1"),
        create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P1"),
        create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C2"),
        create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P2"),
        return_exceptions=True,
    )

    won = [r for r in results if isinstance(r, dict)]
    assert len(won) == 1, results
    assert all(isinstance(r, CalendarSlotTakenError) for r in results if r not in won)
    local_live = await _rows(db, "confirmed")
    assert len(google.inserts) + len(local_live) == 1


async def test_a_pre_connection_local_event_moves_exclusively_without_a_nested_lock(db, google):
    """A Google agenda still holding a never-pushed `loc_` event moves it on the secretarIA
    agenda - exclusively there (siblings counted) and NOT inside the Google write's agenda lock
    (a nested exclusive write on the same agenda lock would wait on itself forever)."""
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    later, later_end = _window(14)
    legacy = await (await _local(db, tenant_c, x_c)).create_event(later, later_end, "Consulta")
    await _appointment(db, tenant_p, x_p, start, end)
    cal_c = await _google(db, tenant_c, x_c, "tok-c")

    with pytest.raises(CalendarSlotTakenError):
        await asyncio.wait_for(
            update_event_exclusive(cal_c, legacy["id"], start=start, end=end), timeout=5
        )
    free, free_end = _window(16)
    moved = await asyncio.wait_for(
        update_event_exclusive(cal_c, legacy["id"], start=free, end=free_end), timeout=5
    )
    assert moved["id"] == legacy["id"]
    assert google.patches == []


# --------------------------------------------------------------------------
# The lease: what the sibling sees between the Google write and the appointment row
# --------------------------------------------------------------------------


async def test_the_lease_is_a_bare_window_only_the_siblings_see(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    cal_p = await _google(db, tenant_p, x_p, "tok-p")
    booked = await create_event_exclusive(
        cal_p, start=start, end=end, summary="Consulta - Maria", description="Paciente: Maria"
    )

    [lease] = await _rows(db, EVENT_STATUS_GOOGLE_LEASE)
    assert lease.tenant_id == tenant_p.id and lease.professional_id == x_p.id
    assert lease.summary is None and lease.description is None
    assert lease.google_event_id == booked["id"]

    # C sees only the window; P's own secretarIA-agenda reads never list it.
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    items = await cal_c.check_availability(start, end)
    assert [(i["id"], i["summary"]) for i in items] == [(None, None)]
    async with db() as session:
        assert await list_unpushed_events(
            session, tenant_p.id, {x_p.id, None}, start, end, cal_p.tzinfo
        ) == []
        windows = await shared_busy(session, x_c.id, start, end)
    assert [(w.start, w.end) for w in windows] == [(start, end)]


async def test_an_expired_lease_no_longer_blocks(db, google, monkeypatch):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    await create_event_exclusive(
        await _google(db, tenant_p, x_p, "tok-p"), start=start, end=end, summary="Consulta - P"
    )
    async with db() as session, session.begin():
        await session.execute(
            update(CalendarEvent).values(
                created_at=datetime.now(UTC) - agenda_service.GOOGLE_WRITE_LEASE - timedelta(1)
            )
        )
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    assert await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")


# --------------------------------------------------------------------------
# Locks
# --------------------------------------------------------------------------


async def test_the_google_exclusive_write_holds_the_agenda_wide_lock(db, google, monkeypatch):
    tenant_p, tenant_c, x_p, x_c, agenda_id = await _world(db)
    start, end = _window()
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    advisory = []

    async def _record(session, locked):
        advisory.append(locked)

    monkeypatch.setattr(local_calendar, "shared_agenda_write_lock", _record)
    lock = local_calendar._process_lock(agenda_lock_key(agenda_id))
    await lock.acquire()
    task = asyncio.create_task(
        create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")
    )
    await asyncio.sleep(0.05)
    assert not task.done()  # waiting for the person's agenda (P may be writing)
    assert google.inserts == []
    lock.release()
    assert (await task)["id"]
    assert advisory == [agenda_id]


# --------------------------------------------------------------------------
# Without a shared agenda: the plain Google call, byte for byte
# --------------------------------------------------------------------------


async def test_without_a_shared_agenda_the_google_write_is_plain(db, google, monkeypatch):
    tenant = await seed_tenant(db)
    solo = await seed_professional(db, tenant, "Dr. Solo")
    lonely = await seed_professional(db, tenant, "Dra. Sozinha")
    await _link(db, "acct-lonely", lonely)  # an agenda without a sibling
    start, end = _window()

    called = []

    async def _never(*args, **kwargs):  # pragma: no cover - the assertion is that it is unused
        called.append(args)
        raise AssertionError("no exclusive write without a shared agenda")

    monkeypatch.setattr(CalendarService, "exclusive_write", _never)
    for professional, token in ((solo, "tok-solo"), (lonely, "tok-lonely")):
        cal = await _google(db, tenant, professional, token)
        google.put(token, start, end)  # Google's own event overlaps: plain write ignores it
        seen, stop = _statements(db)
        try:
            event_ = await create_event_exclusive(cal, start=start, end=end, summary="Consulta")
            await update_event_exclusive(cal, event_["id"], start=start, end=end)
            await update_event_details_exclusive(cal, event_["id"], start, end, "Retorno")
        finally:
            stop()
        assert seen == []  # not a single database statement
    assert called == []
    assert await _rows(db) == []


# --------------------------------------------------------------------------
# Reserve first, write outside the lock (review fix)
# --------------------------------------------------------------------------


def _open_transactions(db):
    """How many transactions are open on the test engine right now (begin - end events)."""
    engine = db.kw["bind"].sync_engine
    state = {"open": 0}

    def _begin(conn):
        state["open"] += 1

    def _end(conn):
        state["open"] -= 1

    event.listen(engine, "begin", _begin)
    event.listen(engine, "commit", _end)
    event.listen(engine, "rollback", _end)

    def _stop():
        event.remove(engine, "begin", _begin)
        event.remove(engine, "commit", _end)
        event.remove(engine, "rollback", _end)

    return state, _stop


async def test_no_transaction_or_lock_is_held_while_google_answers(db, google):
    tenant_p, tenant_c, x_p, x_c, agenda_id = await _world(db)
    start, end = _window()
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    lock = local_calendar._process_lock(agenda_lock_key(agenda_id))
    seen = []
    state, stop = _open_transactions(db)
    google.on_write = lambda: seen.append((state["open"], lock.locked()))
    try:
        booked = await create_event_exclusive(cal_c, start=start, end=end, summary="C")
        own = await create_event_exclusive(
            cal_c, start=start + timedelta(hours=2), end=end + timedelta(hours=2), summary="C2"
        )
        await update_event_exclusive(
            cal_c, own["id"], start=start + timedelta(hours=3), end=end + timedelta(hours=3)
        )
    finally:
        stop()
    assert booked["id"]
    assert seen == [(0, False), (0, False), (0, False)]  # 2 inserts + 1 patch, all unlocked


async def test_a_google_write_that_fails_after_the_reservation_frees_the_window(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    google.fail_next_write = HttpError(
        resp=type("R", (), {"status": 503, "reason": "down"})(), content=b"{}"
    )

    with pytest.raises(CalendarUnavailableError):
        await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")

    assert await _rows(db, EVENT_STATUS_GOOGLE_LEASE) == []
    cal_p = await _google(db, tenant_p, x_p, "tok-p")
    assert await create_event_exclusive(cal_p, start=start, end=end, summary="Consulta - P")


async def test_the_reservation_sweeps_the_agendas_expired_leases(db, google):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    other_tenant = await seed_tenant(db, name="Clínica Z")
    stranger = await seed_professional(db, other_tenant, "Dra. Z")
    old = datetime.now(UTC) - agenda_service.GOOGLE_WRITE_LEASE - timedelta(minutes=1)
    later, later_end = _window(15)
    async with db() as session:
        for index, (tenant, owner) in enumerate(
            ((tenant_p, x_p), (tenant_c, x_c), (other_tenant, stranger))
        ):
            session.add(
                CalendarEvent(
                    id=f"lease_old{index}", tenant_id=tenant.id, professional_id=owner.id,
                    start_at=later, end_at=later_end, busy=True,
                    status=EVENT_STATUS_GOOGLE_LEASE, created_at=old, updated_at=old,
                )
            )
        await session.commit()

    start, end = _window()
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")

    ids = {row.id for row in await _rows(db, EVENT_STATUS_GOOGLE_LEASE)}
    assert "lease_old0" not in ids and "lease_old1" not in ids  # both owners of the agenda
    assert "lease_old2" in ids  # another person's agenda is not this write's business
    assert len(ids) == 2  # + the fresh lease of this write


async def test_the_exclusive_google_calls_get_an_explicit_timeout(db, google, monkeypatch):
    tenant_p, tenant_c, x_p, x_c, _ = await _world(db)
    start, end = _window()
    cal_c = await _google(db, tenant_c, x_c, "tok-c")
    monkeypatch.setattr(cal_c._settings, "GOOGLE_CALENDAR_EXCLUSIVE_TIMEOUT_SECONDS", 7.5)

    await cal_c.check_availability(start, end)  # a plain read: the library default
    await create_event_exclusive(cal_c, start=start, end=end, summary="Consulta - C")

    assert google.timeouts == [None, 7.5]


def test_the_timed_client_really_carries_the_socket_timeout(monkeypatch):
    from google.oauth2.credentials import Credentials

    monkeypatch.setattr(Credentials, "refresh", lambda self, request: None)
    service = CalendarService()
    service._settings = type(
        "S", (), {"GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "secret",
                  "GOOGLE_REFRESH_TOKEN": "tok"}
    )()
    timed = service._build_service(timeout=5.0)
    assert timed._http.http.timeout == 5.0
    from googleapiclient.http import DEFAULT_HTTP_TIMEOUT_SEC

    plain = service._build_service()
    assert plain._http.http.timeout == DEFAULT_HTTP_TIMEOUT_SEC  # untouched library default
