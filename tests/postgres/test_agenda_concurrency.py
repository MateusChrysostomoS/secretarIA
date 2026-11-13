"""TASK-083 Task 8: the shared agenda's "exactly one winner" guarantee, on REAL Postgres.

The SQLite suite cannot prove it: SQLite has one writer at a time and `pg_advisory_xact_lock`
is a no-op there (services/agenda.py::agenda_write_lock, local_calendar._advisory_lock). Here
`WRITERS` concurrent writers of TWO clinics - tenant P and tenant C, whose professionals share
ONE `agendas` row (the same person) - all try to book the SAME window at once, `ROUNDS` times,
each writer on its own session/connection (the production code opens one per call from the
session factory this module points at the throwaway database).

Two modes for every booking scenario:

* ``in_process``    - production exactly as it is: the per-agenda ``asyncio.Lock`` plus the
  Postgres advisory locks.
* ``cross_process`` - ``local_calendar._process_lock`` hands every caller a FRESH lock, so each
  writer behaves like its own worker process (API + worker, two replicas): ONLY the Postgres
  advisory locks stand between them. This is the mode that proves the cross-process guarantee.

A small random pause right after the busy check (and a slow, jittery fake Google) widens the
check-then-write gap a broken lock would expose.

Negative controls: the same harness with the locks broken (advisory locks no-ops, in-process
lock off) and a soft barrier right after the busy check produces MORE than one winner - so the
assertions here would catch a broken lock - while the very same barrier with the locks on still
yields exactly one.

Skipped entirely without ``T083_PG_URL`` (see tests/postgres/README.md): the normal suite never
touches Postgres.
"""

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from secretaria.models import Agenda, CalendarEvent, Professional, Tenant
from secretaria.models.calendar_event import EVENT_STATUS_CONFIRMED, EVENT_STATUS_GOOGLE_LEASE
from secretaria.services import agenda_linking, local_calendar
from secretaria.services.agenda_linking import ensure_agenda_for_professional
from secretaria.services.calendar import CalendarService, CalendarSlotTakenError
from secretaria.services.local_calendar import LocalCalendarService, create_event_exclusive
from secretaria.services.tenant_config import load_tenant_config
from tests._google_fake import GoogleFake, install_google_fake
from tests._local_agenda import seed_professional, seed_tenant, tomorrow_at, wire_local_agenda

PG_URL = os.environ.get("T083_PG_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL,
    reason="T083_PG_URL not set: real-Postgres concurrency proof (tests/postgres/README.md)",
)

ROUNDS = 20
WRITERS = 8
MODES = ("in_process", "cross_process")
_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _require_throwaway_database(url: str) -> None:
    """These tests create and delete tenants: refuse anything but a local throwaway database
    (a loopback host AND a database named t083*, as tests/postgres/README.md creates)."""
    parsed = make_url(url)
    if parsed.host not in _LOOPBACK_HOSTS or not (parsed.database or "").startswith("t083"):
        pytest.fail(
            "T083_PG_URL must point at a THROWAWAY Postgres: a loopback host and a database "
            f"named t083* (got host={parsed.host!r}, database={parsed.database!r}). "
            "See tests/postgres/README.md.",
            pytrace=False,
        )


# --------------------------------------------------------------------------
# The throwaway database
# --------------------------------------------------------------------------


@dataclass
class Pg:
    factory: async_sessionmaker
    engine: object
    tenant_ids: list[UUID] = field(default_factory=list)
    agenda_ids: list[UUID] = field(default_factory=list)


@pytest.fixture
async def pg(monkeypatch):
    """An engine on T083_PG_URL; the agenda services' own sessions point at it. Every row a
    test creates is deleted again (tenants cascade to professionals and calendar events)."""
    _require_throwaway_database(PG_URL)
    engine = create_async_engine(PG_URL, pool_size=WRITERS * 2, max_overflow=WRITERS * 2)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    wire_local_agenda(monkeypatch, factory)
    world = Pg(factory=factory, engine=engine)
    try:
        yield world
    finally:
        async with factory() as session, session.begin():
            if world.tenant_ids:
                await session.execute(delete(Tenant).where(Tenant.id.in_(world.tenant_ids)))
            if world.agenda_ids:
                await session.execute(delete(Agenda).where(Agenda.id.in_(world.agenda_ids)))
        await engine.dispose()


@dataclass
class World:
    tenant_p: Tenant
    tenant_c: Tenant
    x_p: Professional
    x_c: Professional
    agenda_id: UUID

    @property
    def owners(self) -> list[UUID]:
        return [self.x_p.id, self.x_c.id]


async def _tenant(pg: Pg, name: str) -> Tenant:
    tenant = await seed_tenant(pg.factory, name=name)
    pg.tenant_ids.append(tenant.id)
    return tenant


async def _shared_world(pg: Pg) -> World:
    """Dr. X at clinic P and at clinic C: two professionals rows, ONE agenda."""
    tenant_p = await _tenant(pg, "Clínica P")
    tenant_c = await _tenant(pg, "Clínica C")
    x_p = await seed_professional(pg.factory, tenant_p, "Dr. X")
    x_c = await seed_professional(pg.factory, tenant_c, "Dr. X")
    async with pg.factory() as session:
        row = Agenda(person_key=f"t083-{uuid4().hex}")
        session.add(row)
        await session.flush()
        for professional in (x_p, x_c):
            (await session.get(Professional, professional.id)).agenda_id = row.id
        await session.commit()
    pg.agenda_ids.append(row.id)
    return World(tenant_p, tenant_c, x_p, x_c, row.id)


async def _calendar(
    pg: Pg, tenant: Tenant, professional: Professional, *, google_token: str | None = None
) -> CalendarService:
    """The professional's agenda built by the production factory (local, or Google)."""
    async with pg.factory() as session:
        config = await load_tenant_config(session, await session.get(Tenant, tenant.id))
    if google_token is not None:
        config = replace(config, google_refresh_token=google_token)
    calendar = CalendarService.from_tenant_config(config, professional_id=professional.id)
    assert isinstance(calendar, LocalCalendarService) is (google_token is None)
    assert calendar.supports_exclusive_write is True
    return calendar


def _window(round_no: int) -> tuple[datetime, datetime]:
    """A fresh 30-minute window per round (rounds never overlap each other)."""
    start = tomorrow_at(6) + timedelta(minutes=45 * round_no)
    return start, start + timedelta(minutes=30)


async def _occupying(pg: Pg, world: World, start: datetime, end: datetime) -> list[CalendarEvent]:
    """Every row that keeps [start, end) busy for the person, across BOTH clinics: live local
    events and Google-write leases."""
    async with pg.factory() as session:
        return list(
            await session.scalars(
                select(CalendarEvent).where(
                    CalendarEvent.professional_id.in_(world.owners),
                    CalendarEvent.status.in_([EVENT_STATUS_CONFIRMED, EVENT_STATUS_GOOGLE_LEASE]),
                    CalendarEvent.busy.is_(True),
                    CalendarEvent.start_at < end,
                    CalendarEvent.end_at > start,
                )
            )
        )


async def _unstamped_leases(pg: Pg, world: World) -> int:
    """Leases whose Google write never happened (a loser that did not release its lease)."""
    async with pg.factory() as session:
        rows = await session.scalars(
            select(CalendarEvent.id).where(
                CalendarEvent.professional_id.in_(world.owners),
                CalendarEvent.status == EVENT_STATUS_GOOGLE_LEASE,
                CalendarEvent.google_event_id.is_(None),
            )
        )
        return len(list(rows))


# --------------------------------------------------------------------------
# Pressure, process model and broken locks
# --------------------------------------------------------------------------


def _each_writer_its_own_process(monkeypatch) -> None:
    """No in-process lock is ever shared: only Postgres can serialize the writers."""
    monkeypatch.setattr(local_calendar, "_process_lock", lambda _key: asyncio.Lock())


def _apply_mode(monkeypatch, mode: str) -> None:
    assert mode in MODES
    if mode == "cross_process":
        _each_writer_its_own_process(monkeypatch)


def _break_the_locks(monkeypatch) -> None:
    """The negative control: no in-process lock, and both advisory locks are no-ops."""
    _each_writer_its_own_process(monkeypatch)

    async def _no_lock(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(local_calendar, "_advisory_lock", _no_lock)
    monkeypatch.setattr(local_calendar, "shared_agenda_write_lock", _no_lock)


def _pause_after_busy_check(monkeypatch, pause: Callable[[list], Awaitable[None]]) -> None:
    """Run `pause(windows)` right after the shared-busy check of every exclusive write - inside
    the locked transaction, between the check and the write (local `_overlap_exists` and the
    Google reservation `reserve_person_window` both call it from local_calendar)."""
    real = local_calendar.shared_busy

    async def _paused(session, professional_id, start, end):
        windows = await real(session, professional_id, start, end)
        await pause(windows)
        return windows

    monkeypatch.setattr(local_calendar, "shared_busy", _paused)


def _jitter(monkeypatch, rng: random.Random, max_delay: float = 0.01) -> None:
    async def _sleep(_windows) -> None:
        await asyncio.sleep(rng.uniform(0, max_delay))

    _pause_after_busy_check(monkeypatch, _sleep)


class SoftBarrier:
    """Waits until `parties` writers arrived or `timeout` passed - whichever is first.

    Without locks every writer reaches it together and they all leave at once (a forced race);
    with working locks only one is ever inside, so it times out and goes on alone."""

    def __init__(self, parties: int, timeout: float) -> None:
        self.parties, self.timeout = parties, timeout
        self.arrived = 0
        self.timeouts = 0
        self._all_in = asyncio.Event()

    async def wait(self) -> None:
        self.arrived += 1
        if self.arrived >= self.parties:
            self._all_in.set()
        try:
            await asyncio.wait_for(self._all_in.wait(), self.timeout)
        except TimeoutError:
            self.timeouts += 1


def _barrier_after_free_check(monkeypatch, barrier: SoftBarrier) -> None:
    async def _wait_if_free(windows) -> None:
        if not windows:  # only a writer that is about to write
            await barrier.wait()

    _pause_after_busy_check(monkeypatch, _wait_if_free)


def _slow_google(monkeypatch, rng: random.Random) -> GoogleFake:
    """tests/_google_fake.py, but every list and insert takes a few random milliseconds (in the
    worker thread, like a real HTTP call) so the reservation and the Google write overlap."""
    fake = install_google_fake(monkeypatch)
    plain_client = fake.client

    def _pause() -> None:
        time.sleep(rng.uniform(0.002, 0.02))

    def _client(token: str):
        base = plain_client(token)

        def _events():
            events = base.events()
            plain_list = events.list

            def _slow_list(*args, **kwargs):
                request = plain_list(*args, **kwargs)
                run = request.execute

                def _execute():
                    _pause()
                    return run()

                request.execute = _execute
                return request

            events.list = _slow_list
            return events

        return SimpleNamespace(events=_events)

    fake.client = _client
    fake.on_write = _pause
    return fake


# --------------------------------------------------------------------------
# One round
# --------------------------------------------------------------------------


@dataclass
class RoundResult:
    won: list[dict]
    refused: list[CalendarSlotTakenError]
    unexpected: list[BaseException]


async def _race(
    calendars: list[CalendarService],
    start: datetime,
    end: datetime,
    rng: random.Random,
    *,
    head_start: CalendarService | None = None,
) -> RoundResult:
    """Every calendar books [start, end) at once, each through the patient write path (a few
    random milliseconds of start offset vary who reaches the lock first; `head_start`'s writers
    get 20 ms more lead, so a slower backend gets to win some rounds too)."""
    order = list(enumerate(calendars))
    rng.shuffle(order)
    offsets = [
        rng.uniform(0, 0.02) + (0.02 if head_start is not None and cal is not head_start else 0)
        for _index, cal in order
    ]

    async def _book(index: int, calendar: CalendarService, offset: float) -> dict:
        await asyncio.sleep(offset)
        return await create_event_exclusive(
            calendar, start=start, end=end, summary=f"Consulta {index}"
        )

    results = await asyncio.gather(
        *(_book(index, cal, offset) for (index, cal), offset in zip(order, offsets, strict=True)),
        return_exceptions=True,
    )
    return RoundResult(
        won=[r for r in results if isinstance(r, dict)],
        refused=[r for r in results if isinstance(r, CalendarSlotTakenError)],
        unexpected=[r for r in results if not isinstance(r, dict | CalendarSlotTakenError)],
    )


def _assert_one_winner(result: RoundResult, round_no: int) -> None:
    assert result.unexpected == [], f"round {round_no}: unhandled errors {result.unexpected!r}"
    assert len(result.won) == 1, f"round {round_no}: {len(result.won)} winners"
    assert len(result.refused) == WRITERS - 1, f"round {round_no}: {len(result.refused)} refused"


def _half_and_half(cal_p: CalendarService, cal_c: CalendarService) -> list[CalendarService]:
    """WRITERS writers, alternating clinic P and clinic C."""
    return [cal_p if index % 2 == 0 else cal_c for index in range(WRITERS)]


# --------------------------------------------------------------------------
# a. Local backend (both clinics on the secretarIA agenda)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_local_agenda_two_clinics_exactly_one_winner_per_round(pg, monkeypatch, mode):
    _apply_mode(monkeypatch, mode)
    rng = random.Random(f"local-{mode}")
    _jitter(monkeypatch, rng)
    world = await _shared_world(pg)
    cal_p = await _calendar(pg, world.tenant_p, world.x_p)
    cal_c = await _calendar(pg, world.tenant_c, world.x_c)

    for round_no in range(ROUNDS):
        start, end = _window(round_no)
        result = await _race(_half_and_half(cal_p, cal_c), start, end, rng)
        _assert_one_winner(result, round_no)

        rows = await _occupying(pg, world, start, end)
        assert len(rows) == 1, f"round {round_no}: {len(rows)} occupying rows"
        assert rows[0].status == EVENT_STATUS_CONFIRMED and rows[0].id == result.won[0]["id"]


# --------------------------------------------------------------------------
# b. Fake-Google backend (both clinics on their own Google accounts)
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_google_agenda_two_clinics_exactly_one_winner_per_round(pg, monkeypatch, mode):
    _apply_mode(monkeypatch, mode)
    rng = random.Random(f"google-{mode}")
    _jitter(monkeypatch, rng)
    google = _slow_google(monkeypatch, rng)
    world = await _shared_world(pg)
    cal_p = await _calendar(pg, world.tenant_p, world.x_p, google_token="tok-p")
    cal_c = await _calendar(pg, world.tenant_c, world.x_c, google_token="tok-c")

    for round_no in range(ROUNDS):
        start, end = _window(round_no)
        inserts_before = len(google.inserts)
        result = await _race(_half_and_half(cal_p, cal_c), start, end, rng)
        _assert_one_winner(result, round_no)

        # Google got exactly one successful insert, and it is the winner's event.
        new_inserts = google.inserts[inserts_before:]
        assert [event_id for _token, event_id in new_inserts] == [result.won[0]["id"]]
        on_google = [
            e
            for token in ("tok-p", "tok-c")
            for e in google.events_of(token)
            if datetime.fromisoformat(e["start"]["dateTime"]) < end
            and datetime.fromisoformat(e["end"]["dateTime"]) > start
        ]
        assert len(on_google) == 1, f"round {round_no}: {len(on_google)} Google events"

        # The database: one lease for the window, stamped with the winner's Google id; no
        # loser left a lease behind.
        rows = await _occupying(pg, world, start, end)
        assert len(rows) == 1, f"round {round_no}: {len(rows)} occupying rows"
        assert rows[0].status == EVENT_STATUS_GOOGLE_LEASE
        assert rows[0].google_event_id == result.won[0]["id"]
        assert await _unstamped_leases(pg, world) == 0


# --------------------------------------------------------------------------
# c. Mixed: clinic P on Google, clinic C on the secretarIA agenda
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", MODES)
async def test_mixed_backends_two_clinics_exactly_one_winner_per_round(pg, monkeypatch, mode):
    _apply_mode(monkeypatch, mode)
    rng = random.Random(f"mixed-{mode}")
    _jitter(monkeypatch, rng)
    google = _slow_google(monkeypatch, rng)
    world = await _shared_world(pg)
    cal_p = await _calendar(pg, world.tenant_p, world.x_p, google_token="tok-p")
    cal_c = await _calendar(pg, world.tenant_c, world.x_c)

    winners_by_backend = {"google": 0, "local": 0}
    for round_no in range(ROUNDS):
        start, end = _window(round_no)
        inserts_before = len(google.inserts)
        # Odd rounds give the local writers a lead. Which backend wins is not asserted here:
        # `test_mixed_backends_each_side_is_refused_over_the_others_win` pins both directions.
        result = await _race(
            _half_and_half(cal_p, cal_c),
            start,
            end,
            rng,
            head_start=cal_c if round_no % 2 else None,
        )
        _assert_one_winner(result, round_no)

        winner = result.won[0]["id"]
        local_won = winner.startswith("loc_")
        winners_by_backend["local" if local_won else "google"] += 1
        assert len(google.inserts) - inserts_before == (0 if local_won else 1)
        rows = await _occupying(pg, world, start, end)
        assert len(rows) == 1, f"round {round_no}: {len(rows)} occupying rows"
        if local_won:
            assert rows[0].status == EVENT_STATUS_CONFIRMED and rows[0].id == winner
        else:
            assert rows[0].status == EVENT_STATUS_GOOGLE_LEASE
            assert rows[0].google_event_id == winner
        assert await _unstamped_leases(pg, world) == 0
    print(f"mixed/{mode} winners by backend: {winners_by_backend}")


@pytest.mark.parametrize("mode", MODES)
async def test_mixed_backends_each_side_is_refused_over_the_others_win(pg, monkeypatch, mode):
    """Deterministic directions: one clinic's writers race ALONE (one winner), then the other
    backend's writers race into the same window and are ALL refused - Google over a local
    win, and local over a Google win."""
    _apply_mode(monkeypatch, mode)
    rng = random.Random(f"mixed-directions-{mode}")
    _jitter(monkeypatch, rng)
    google = _slow_google(monkeypatch, rng)
    world = await _shared_world(pg)
    cal_p = await _calendar(pg, world.tenant_p, world.x_p, google_token="tok-p")
    cal_c = await _calendar(pg, world.tenant_c, world.x_c)
    half = WRITERS // 2

    for round_no, (first, second) in enumerate(((cal_c, cal_p), (cal_p, cal_c))):
        start, end = _window(round_no)
        inserts_before = len(google.inserts)
        alone = await _race([first] * half, start, end, rng)
        assert alone.unexpected == [] and len(alone.won) == 1, f"round {round_no}: {alone!r}"
        assert len(alone.refused) == half - 1
        winner = alone.won[0]["id"]
        assert winner.startswith("loc_") is (first is cal_c)

        late = await _race([second] * half, start, end, rng)
        assert late.unexpected == [] and late.won == [], f"round {round_no}: {late!r}"
        assert len(late.refused) == half
        assert len(google.inserts) - inserts_before == (0 if first is cal_c else 1)
        rows = await _occupying(pg, world, start, end)
        assert len(rows) == 1, f"round {round_no}: {len(rows)} occupying rows"
        assert winner in (rows[0].id, rows[0].google_event_id)
        assert await _unstamped_leases(pg, world) == 0


# --------------------------------------------------------------------------
# d. Linking: concurrent ensure_agenda_for_professional of one person, two clinics
# --------------------------------------------------------------------------


async def _two_clinic_doctors(pg: Pg) -> list[Professional]:
    """WRITERS professionals rows of one person, half at clinic P and half at clinic C."""
    tenant_p = await _tenant(pg, "Clínica P")
    tenant_c = await _tenant(pg, "Clínica C")
    return [
        await seed_professional(pg.factory, tenant_p if index % 2 == 0 else tenant_c, "Dr. X")
        for index in range(WRITERS)
    ]


async def _link_all(factory: async_sessionmaker, professionals, key: str) -> list:
    async def _link(professional_id: UUID):
        async with factory() as session:
            professional = await session.get(Professional, professional_id)
            agenda = await ensure_agenda_for_professional(session, professional, key)
            await session.commit()
            return agenda.id

    return await asyncio.gather(
        *(_link(professional.id) for professional in professionals), return_exceptions=True
    )


async def test_concurrent_linking_of_one_person_converges_on_one_agenda(pg):
    professionals = await _two_clinic_doctors(pg)

    for round_no in range(ROUNDS):
        key = f"t083-link-{uuid4().hex}"
        results = await _link_all(pg.factory, professionals, key)

        async with pg.factory() as session:
            agendas = list(await session.scalars(select(Agenda.id).where(Agenda.person_key == key)))
            linked = set(
                await session.scalars(
                    select(Professional.agenda_id).where(
                        Professional.id.in_([p.id for p in professionals])
                    )
                )
            )
        pg.agenda_ids.extend(agendas)  # cleaned up even when an assert below fails
        errors = [r for r in results if isinstance(r, BaseException)]
        assert errors == [], f"round {round_no}: {errors!r}"
        assert len(agendas) == 1, f"round {round_no}: {len(agendas)} agendas rows"
        assert set(results) == {agendas[0]} and linked == {agendas[0]}


# --------------------------------------------------------------------------
# Negative controls: the harness DOES catch a broken lock
# --------------------------------------------------------------------------


async def test_negative_control_local_without_locks_double_books(pg, monkeypatch):
    _break_the_locks(monkeypatch)
    barrier = SoftBarrier(WRITERS, timeout=5)
    _barrier_after_free_check(monkeypatch, barrier)
    world = await _shared_world(pg)
    cal_p = await _calendar(pg, world.tenant_p, world.x_p)
    cal_c = await _calendar(pg, world.tenant_c, world.x_c)
    start, end = _window(0)

    result = await _race(_half_and_half(cal_p, cal_c), start, end, random.Random(0))

    rows = await _occupying(pg, world, start, end)
    print(f"negative/local: winners={len(result.won)} occupying_rows={len(rows)}")
    assert result.unexpected == []
    assert barrier.arrived == WRITERS  # every writer passed the free check: a forced race
    assert len(result.won) > 1 and len(rows) > 1  # the double booking the locks prevent


async def test_negative_control_google_without_locks_double_books(pg, monkeypatch):
    _break_the_locks(monkeypatch)
    barrier = SoftBarrier(WRITERS, timeout=5)
    _barrier_after_free_check(monkeypatch, barrier)
    google = _slow_google(monkeypatch, random.Random(0))
    world = await _shared_world(pg)
    cal_p = await _calendar(pg, world.tenant_p, world.x_p, google_token="tok-p")
    cal_c = await _calendar(pg, world.tenant_c, world.x_c, google_token="tok-c")
    start, end = _window(0)

    result = await _race(_half_and_half(cal_p, cal_c), start, end, random.Random(0))

    print(f"negative/google: winners={len(result.won)} google_inserts={len(google.inserts)}")
    assert result.unexpected == []
    assert barrier.arrived == WRITERS  # every writer passed the free check: a forced race
    assert len(result.won) > 1 and len(google.inserts) > 1


@pytest.mark.parametrize("backend", ["local", "google"])
async def test_the_same_forced_race_with_the_locks_on_has_one_winner(pg, monkeypatch, backend):
    """The A/B partner of the negative controls: identical barrier, only the locks differ
    (cross-process mode: the advisory locks alone).

    While the first writer sits at the barrier inside its locked transaction, Postgres itself
    is asked who is waiting: the other WRITERS - 1 must be blocked on an ADVISORY lock - the
    proof that it is Postgres, not an in-process lock, that holds them back."""
    _each_writer_its_own_process(monkeypatch)
    barrier = SoftBarrier(WRITERS, timeout=1.5)
    _barrier_after_free_check(monkeypatch, barrier)
    world = await _shared_world(pg)
    if backend == "google":
        _slow_google(monkeypatch, random.Random(0))
        cal_p = await _calendar(pg, world.tenant_p, world.x_p, google_token="tok-p")
        cal_c = await _calendar(pg, world.tenant_c, world.x_c, google_token="tok-c")
    else:
        cal_p = await _calendar(pg, world.tenant_p, world.x_p)
        cal_c = await _calendar(pg, world.tenant_c, world.x_c)
    start, end = _window(0)

    race = asyncio.create_task(_race(_half_and_half(cal_p, cal_c), start, end, random.Random(0)))
    most_waiting = 0
    async with pg.factory() as probe:
        while not race.done() and barrier.timeouts == 0:
            waiting = await probe.scalar(
                text(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
                    " AND wait_event_type = 'Lock' AND wait_event = 'advisory'"
                )
            )
            await probe.rollback()  # a fresh snapshot of pg_stat_activity next time
            most_waiting = max(most_waiting, waiting)
            await asyncio.sleep(0.02)
    result = await race

    _assert_one_winner(result, 0)
    assert barrier.arrived == 1 and barrier.timeouts == 1  # nobody else ever got past the check
    assert most_waiting == WRITERS - 1, most_waiting
    assert len(await _occupying(pg, world, start, end)) == 1


class _BarrierAfterAgendaRead(AsyncSession):
    """A session that waits at `barrier` right after reading the `agendas` row (the linker's
    get-or-create read), so every linker misses the row before any of them inserts it."""

    barrier: SoftBarrier | None = None

    async def scalar(self, statement, *args, **kwargs):
        value = await super().scalar(statement, *args, **kwargs)
        if self.barrier is not None and value is None and "FROM agendas" in str(statement):
            await self.barrier.wait()
        return value


async def test_negative_control_linking_without_the_person_lock_collides(pg, monkeypatch):
    async def _no_lock(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(agenda_linking, "_person_lock", _no_lock)
    professionals = await _two_clinic_doctors(pg)
    barrier = SoftBarrier(WRITERS, timeout=5)
    _BarrierAfterAgendaRead.barrier = barrier
    racing = async_sessionmaker(pg.engine, class_=_BarrierAfterAgendaRead, expire_on_commit=False)
    key = f"t083-link-{uuid4().hex}"
    try:
        results = await _link_all(racing, professionals, key)
    finally:
        _BarrierAfterAgendaRead.barrier = None
    async with pg.factory() as session:
        made = await session.scalars(select(Agenda.id).where(Agenda.person_key == key))
        pg.agenda_ids.extend(made)

    collisions = [r for r in results if isinstance(r, IntegrityError)]
    print(f"negative/linking: integrity_errors={len(collisions)} of {WRITERS}")
    assert barrier.arrived == WRITERS  # every linker missed the row before any inserted it
    assert len(collisions) == WRITERS - 1  # what the person lock prevents


# --------------------------------------------------------------------------
# Opt-in: the broken lock under jitter alone (no barrier) - naturally racy, so not a gate
# --------------------------------------------------------------------------


@pytest.mark.skipif(
    not os.environ.get("T083_PG_NEGATIVE_JITTER"), reason="opt-in: T083_PG_NEGATIVE_JITTER=1"
)
@pytest.mark.parametrize("backend", ["local", "google"])
async def test_negative_control_jitter_only_without_locks(pg, monkeypatch, backend):
    _break_the_locks(monkeypatch)
    rng = random.Random(f"negative-{backend}")
    _jitter(monkeypatch, rng)
    world = await _shared_world(pg)
    if backend == "google":
        _slow_google(monkeypatch, rng)
        cal_p = await _calendar(pg, world.tenant_p, world.x_p, google_token="tok-p")
        cal_c = await _calendar(pg, world.tenant_c, world.x_c, google_token="tok-c")
    else:
        cal_p = await _calendar(pg, world.tenant_p, world.x_p)
        cal_c = await _calendar(pg, world.tenant_c, world.x_c)

    winners = []
    for round_no in range(ROUNDS):
        start, end = _window(round_no)
        result = await _race(_half_and_half(cal_p, cal_c), start, end, rng)
        winners.append(len(result.won))
    print(f"negative-jitter/{backend}: winners per round = {winners}")
    assert max(winners) > 1
