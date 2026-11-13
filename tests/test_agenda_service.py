# ruff: noqa: F811
"""services/agenda.py (TASK-083 spec §7): sibling professionals, shared busy windows, lock.

Two clinics (tenants P and C) whose `professionals` rows point at ONE `agendas` row. The busy
time of the sibling comes back as bare `(start, end)` windows - never a summary, patient or tenant.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from secretaria.models import (
    Agenda,
    Appointment,
    AppointmentStatus,
    BookingHold,
    CalendarEvent,
    Professional,
)
from secretaria.services import agenda as agenda_service
from secretaria.services.agenda import BusyWindow, shared_busy, sibling_professional_ids
from tests._local_agenda import seed_professional, seed_tenant
from tests._reminder_fixtures import db  # noqa: F401

START = datetime(2030, 3, 4, 13, 0, tzinfo=UTC)
END = START + timedelta(hours=1)


async def _shared_world(db):
    """P's Dr. X and C's Dr. X share an agenda; P's Dra. Y is somebody else (own agenda)."""
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    y_p = await seed_professional(db, tenant_p, "Dra. Y")
    async with db() as session:
        shared, other = Agenda(person_key="acct-x"), Agenda(person_key="acct-y")
        session.add_all([shared, other])
        await session.flush()
        (await session.get(Professional, x_p.id)).agenda_id = shared.id
        (await session.get(Professional, x_c.id)).agenda_id = shared.id
        (await session.get(Professional, y_p.id)).agenda_id = other.id
        await session.commit()
        return tenant_p, tenant_c, x_p, x_c, y_p, shared.id


async def _add(db, *rows):
    async with db() as session:
        session.add_all(rows)
        await session.commit()


def _appointment(tenant, professional, status=AppointmentStatus.SCHEDULED, start=START, end=END):
    return Appointment(
        tenant_id=tenant.id,
        professional_id=professional.id,
        google_event_id=f"evt_{uuid4().hex}",
        start_at=start,
        end_at=end,
        status=status,
    )


def _event(tenant, professional, *, status="confirmed", busy=True, google=None, start=START):
    return CalendarEvent(
        id=f"loc_{uuid4().hex}",
        tenant_id=tenant.id,
        professional_id=professional.id,
        summary="Maria Silva - retorno",
        start_at=start,
        end_at=start + timedelta(hours=1),
        status=status,
        busy=busy,
        google_event_id=google,
    )


def _hold(tenant, professional, *, phase="payment", expires=None, start=START):
    return BookingHold(
        tenant_id=tenant.id,
        conversation_id=uuid4(),
        professional_id=professional.id,
        start_at=start,
        end_at=start + timedelta(hours=1),
        expires_at=expires or datetime.now(UTC) + timedelta(minutes=20),
        phase=phase,
    )


async def test_siblings_are_the_other_rows_of_the_same_agenda_across_tenants(db):
    _p, _c, x_p, x_c, y_p, _agenda = await _shared_world(db)
    async with db() as session:
        assert await sibling_professional_ids(session, x_p.id) == [x_c.id]
        assert await sibling_professional_ids(session, x_c.id) == [x_p.id]
        assert await sibling_professional_ids(session, y_p.id) == []  # alone on its agenda


async def test_without_an_agenda_everything_is_empty(db):
    tenant = await seed_tenant(db)
    solo = await seed_professional(db, tenant, "Dr. Solo")
    other = await seed_professional(db, tenant, "Dr. Other")
    await _add(db, _appointment(tenant, other), _event(tenant, other))
    async with db() as session:
        assert await sibling_professional_ids(session, solo.id) == []
        assert await shared_busy(session, solo.id, START, END) == []
        assert await shared_busy(session, None, START, END) == []
        assert await shared_busy(session, uuid4(), START, END) == []


async def test_shared_busy_unions_appointments_local_events_and_payment_holds_as_windows_only(db):
    tenant_p, tenant_c, x_p, x_c, _y, _a = await _shared_world(db)
    await _add(
        db,
        _appointment(tenant_p, x_p, start=START, end=START + timedelta(minutes=30)),
        _event(tenant_p, x_p, start=START + timedelta(hours=2)),
        _hold(tenant_p, x_p, start=START + timedelta(hours=4)),
    )
    async with db() as session:
        busy = await shared_busy(
            session, x_c.id, START - timedelta(hours=1), START + timedelta(hours=9)
        )
    assert [type(w) for w in busy] == [BusyWindow] * 3
    assert [(w.start, w.end) for w in busy] == [
        (START, START + timedelta(minutes=30)),
        (START + timedelta(hours=2), START + timedelta(hours=3)),
        (START + timedelta(hours=4), START + timedelta(hours=5)),
    ]
    # Windows only: nothing of the sibling's summary, patient, service or clinic can ride along.
    assert set(BusyWindow.__dataclass_fields__) == {"start", "end"}
    blob = repr(busy)
    assert "Maria" not in blob and "Clínica" not in blob and str(tenant_p.id) not in blob


async def test_shared_busy_never_includes_the_callers_own_rows_or_other_agendas(db):
    tenant_p, tenant_c, x_p, x_c, y_p, _a = await _shared_world(db)
    await _add(
        db,
        _appointment(tenant_c, x_c),  # own row of the asker
        _appointment(tenant_p, y_p),  # a different person
        _event(tenant_p, y_p),
        _hold(tenant_p, y_p),
    )
    async with db() as session:
        assert await shared_busy(session, x_c.id, START, END) == []  # not its own row, not Y's
        # ...but C's row is P's sibling row: P sees exactly that one window.
        assert len(await shared_busy(session, x_p.id, START, END)) == 1


async def test_shared_busy_ignores_cancelled_terminal_code_phase_and_expired_rows(db):
    tenant_p, _c, x_p, x_c, _y, _a = await _shared_world(db)
    now = datetime.now(UTC)
    await _add(
        db,
        _appointment(tenant_p, x_p, status=AppointmentStatus.CANCELLED),
        _appointment(tenant_p, x_p, status=AppointmentStatus.ATTENDED),
        _appointment(tenant_p, x_p, status=AppointmentStatus.NO_SHOW),
        _event(tenant_p, x_p, status="cancelled"),
        _event(tenant_p, x_p, busy=False),  # a "free" marker, not a block
        _event(
            tenant_p, x_p, google="g_copy"
        ),  # handed to Google: that copy is the one that counts
        _hold(tenant_p, x_p, phase="code"),  # TASK-072: a code-phase hold reserves nothing
        _hold(tenant_p, x_p, phase="payment", expires=now - timedelta(minutes=1)),
    )
    async with db() as session:
        assert (
            await shared_busy(session, x_c.id, START - timedelta(days=1), END + timedelta(days=1))
            == []
        )


async def test_shared_busy_keeps_live_statuses_and_clips_to_the_requested_range(db):
    tenant_p, _c, x_p, x_c, _y, _a = await _shared_world(db)
    day = timedelta(days=1)
    await _add(
        db,
        _appointment(tenant_p, x_p, status=AppointmentStatus.CONFIRMED),
        _appointment(
            tenant_p,
            x_p,
            status=AppointmentStatus.RESCHEDULED,
            start=START + 2 * day,
            end=END + 2 * day,
        ),
        _appointment(tenant_p, x_p, start=START + 9 * day, end=END + 9 * day),  # out of range
    )
    async with db() as session:
        busy = await shared_busy(session, x_c.id, START, START + 3 * day)
    assert len(busy) == 2
    async with db() as session:
        # Half-open: a sibling window ending exactly at `start` does not overlap.
        assert await shared_busy(session, x_c.id, END, END + timedelta(hours=1)) == []


async def test_an_appointment_and_its_local_event_do_not_double_count(db):
    """A local booking writes both an appointment and a calendar event on the same window."""
    tenant_p, _c, x_p, x_c, _y, _a = await _shared_world(db)
    await _add(db, _appointment(tenant_p, x_p), _event(tenant_p, x_p))
    async with db() as session:
        assert [(w.start, w.end) for w in await shared_busy(session, x_c.id, START, END)] == [
            (START, END)
        ]


async def test_write_lock_is_a_noop_on_sqlite_and_without_an_agenda(db):
    _p, _c, _xp, _xc, _y, agenda_id = await _shared_world(db)
    async with db() as session:
        await agenda_service.agenda_write_lock(session, agenda_id)  # sqlite: no-op, no error
        await agenda_service.agenda_write_lock(session, None)


class _PgBind:
    class dialect:  # noqa: N801 - mimics `session.get_bind().dialect`
        name = "postgresql"


class _PgSession:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def get_bind(self):
        return _PgBind()

    async def execute(self, statement, params=None):
        self.calls.append((str(statement), params))


async def test_write_lock_takes_one_stable_advisory_key_per_agenda_on_postgres():
    first, second, other = uuid4(), uuid4(), uuid4()
    session = _PgSession()
    await agenda_service.agenda_write_lock(session, first)
    await agenda_service.agenda_write_lock(session, first)
    await agenda_service.agenda_write_lock(session, second)
    await agenda_service.agenda_write_lock(session, None)  # no agenda: nothing to lock
    assert len(session.calls) == 3
    assert all("pg_advisory_xact_lock" in sql for sql, _ in session.calls)
    keys = [params["key"] for _, params in session.calls]
    assert keys[0] == keys[1] != keys[2]
    assert all(-(2**63) <= k < 2**63 for k in keys)
    assert agenda_service.agenda_lock_key(first) == f"agenda:{first}"
    assert agenda_service.agenda_lock_key(other) != agenda_service.agenda_lock_key(first)


# --------------------------------------------------------------------------
# Final wave M2: every calendar_events read/sweep of the siblings carries tenant_id, so the only
# index there - (tenant_id, professional_id, start_at) - serves it on Postgres (no full scan
# under the person's advisory lock).
# --------------------------------------------------------------------------


def _event_statements(session, seen: list):
    real = session.execute

    async def recording(statement, *args, **kwargs):
        sql = str(statement.compile(compile_kwargs={"literal_binds": False}))
        if "calendar_events" in sql:
            seen.append(sql)
        return await real(statement, *args, **kwargs)

    return recording


async def test_the_siblings_event_reads_filter_on_tenant_and_professional(db, monkeypatch):
    tenant_p, _c, x_p, x_c, _y, _a = await _shared_world(db)
    await _add(db, _event(tenant_p, x_p))
    seen: list[str] = []
    async with db() as session:
        monkeypatch.setattr(session, "execute", _event_statements(session, seen))
        busy = await shared_busy(session, x_c.id, START - timedelta(hours=1), END)
    assert [(w.start, w.end) for w in busy] == [(START, END)]
    assert len(seen) == 2  # local busy events + Google leases
    for sql in seen:
        assert "calendar_events.tenant_id" in sql and "calendar_events.professional_id" in sql


async def test_the_lease_sweep_and_own_lease_check_filter_on_tenant(db, monkeypatch):
    from contextlib import asynccontextmanager

    from secretaria.services import local_calendar
    from tests._local_agenda import wire_local_agenda

    _p, tenant_c, _x_p, x_c, _y, _a = await _shared_world(db)
    wire_local_agenda(monkeypatch, db)
    seen: list[str] = []
    real_lock = local_calendar.person_agenda_write_lock

    @asynccontextmanager
    async def recording_lock(professional_id):
        async with real_lock(professional_id) as (session, agenda_id):
            monkeypatch.setattr(session, "execute", _event_statements(session, seen))
            monkeypatch.setattr(session, "scalar", _scalar_recorder(session, seen))
            yield session, agenda_id

    monkeypatch.setattr(local_calendar, "person_agenda_write_lock", recording_lock)
    lease = await local_calendar.reserve_person_window(tenant_c.id, x_c.id, START, END)
    assert lease is not None and lease != local_calendar.NO_AGENDA
    sweeps = [sql for sql in seen if sql.startswith("DELETE")]
    assert len(sweeps) == 1 and "calendar_events.tenant_id" in sweeps[0]
    own = [sql for sql in seen if sql.startswith("SELECT calendar_events.id")]
    assert own and all("calendar_events.tenant_id" in sql for sql in own)


def _scalar_recorder(session, seen: list):
    real = session.scalar

    async def recording(statement, *args, **kwargs):
        sql = str(statement.compile(compile_kwargs={"literal_binds": False}))
        if "calendar_events" in sql:
            seen.append(sql)
        return await real(statement, *args, **kwargs)

    return recording
