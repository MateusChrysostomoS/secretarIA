# ruff: noqa: F811
"""TASK-088 §B: the doctor's buffer between consultations ("intervalo entre consultas").

`Professional.buffer_minutes` = N: a patient is only OFFERED (and only allowed to CONFIRM) a
time that leaves N free minutes before and after each CONSULTATION of that professional - a
consultation being a live appointment WITH a patient, plus the windows other conversations
are holding. Blocks, all-day events and events with no appointment row are not padded
(designer decision 2: when consultation and block cannot be told apart, offer LESS padding).

The core rule lives in services/availability.py; the DB-backed read of the consultation
windows is `CalendarService.consultation_windows` (services/agenda.py does the query), so
two halves are tested here: the rule over a fake calendar (the fake of
test_booking_min_notice.py, plus the seam), and the seam over real SQLite rows.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from datetime import UTC, date, datetime, timedelta  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import (  # noqa: E402
    Agenda,
    Appointment,
    AppointmentStatus,
    Patient,
    Professional,
)
from secretaria.services import availability  # noqa: E402
from secretaria.services.availability_windows import DayRange, scan_free_windows  # noqa: E402
from tests._local_agenda import (  # noqa: E402
    agenda,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: E402, F401

TZ = ZoneInfo("America/Sao_Paulo")
D0 = date(2026, 10, 5)
D1 = D0 + timedelta(days=1)
D2 = D0 + timedelta(days=2)


def _at(hhmm: str, day: date = D0, minutes: int = 30) -> tuple[datetime, datetime]:
    hour, minute = (int(part) for part in hhmm.split(":"))
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=TZ)
    return start, start + timedelta(minutes=minutes)


def _labels(slots) -> list[str]:
    return [s.strftime("%H:%M") for s in slots]


class _Agenda:
    """08:00-12:00 in 30-minute slots on `open_days`; `busy` windows are not offered.

    `consultations` is what the seam `consultation_windows` answers (a subset of the busy
    time: a block is busy but is not a consultation). Every read of the seam is recorded.
    `with_seam=False` is the small fake calendar most existing tests use: no seam at all.
    """

    tzinfo = TZ

    def __init__(self, *, open_days=(D0,), busy=(), consultations=(), with_seam=True,
                 hours=(8, 12)):
        self.open_days = list(open_days)
        self.busy = list(busy)
        self.consultation_list = list(consultations)
        self.hours = hours
        self.seam_reads: list[tuple[datetime, datetime, str | None]] = []
        self.slot_reads: list[date] = []
        self.day_scans = 0
        if with_seam:
            self.consultation_windows = self._consultation_windows

    async def _consultation_windows(self, start, end, *, ignore_event_id=None):
        self.seam_reads.append((start, end, ignore_event_id))
        return [
            (s, e) for s, e in self.consultation_list if availability.overlaps(start, end, s, e)
        ]

    def _walk(self, d, length, max_slots, extra_busy=()):
        slots, t = [], datetime(d.year, d.month, d.day, self.hours[0], tzinfo=TZ)
        stop = datetime(d.year, d.month, d.day, self.hours[1], tzinfo=TZ)
        blocked = [*self.busy, *extra_busy]
        while t + length <= stop and len(slots) < max_slots:
            if not any(availability.overlaps(t, t + length, s, e) for s, e in blocked):
                slots.append({
                    "start": t.isoformat(),
                    "end": (t + length).isoformat(),
                    "label": t.strftime("%H:%M"),
                })
            t += timedelta(minutes=30)
        return slots

    async def list_available_days(self, start_day, days, slot_minutes=None, extra_busy=None):
        """One scan. Like the real calendar, `extra_busy` is taken time that empties a day."""
        self.day_scans += 1
        first = start_day.date()
        length = timedelta(minutes=slot_minutes or 30)
        return [
            datetime(d.year, d.month, d.day, tzinfo=TZ)
            for d in self.open_days
            if first <= d < first + timedelta(days=days)
            and (not extra_busy or self._walk(d, length, 1, extra_busy))
        ]

    async def list_free_slots(self, day, slot_minutes=None, max_slots=6):
        d = day.date()
        self.slot_reads.append(d)
        if d not in self.open_days:
            return []
        return self._walk(d, timedelta(minutes=slot_minutes or 30), max_slots)


def _doctor(buffer_minutes):
    return SimpleNamespace(id=uuid4(), buffer_minutes=buffer_minutes)


# --- the number ---------------------------------------------------------------------------


def test_booking_buffer_minutes_clamps():
    assert availability.booking_buffer_minutes(None) == 0
    assert availability.booking_buffer_minutes(SimpleNamespace()) == 0
    assert availability.booking_buffer_minutes(_doctor(None)) == 0
    assert availability.booking_buffer_minutes(_doctor(500)) == 120
    assert availability.booking_buffer_minutes(_doctor(120)) == 120
    assert availability.booking_buffer_minutes(_doctor(-5)) == 0
    assert availability.booking_buffer_minutes(_doctor(0)) == 0
    assert availability.booking_buffer_minutes(_doctor(15)) == 15
    for junk in (True, "30", 7.5):
        assert availability.booking_buffer_minutes(_doctor(junk)) == 0


# --- free_slots_for_day -------------------------------------------------------------------


async def test_buffer_zero_is_unchanged():
    busy = [_at("10:00")]
    # A calendar WITHOUT the seam (the small fakes of the existing suite) with buffer 0 ...
    plain = _Agenda(busy=busy, consultations=busy, with_seam=False)
    base = await availability.free_slots_for_day(plain, day=D0, duration_minutes=30)
    # ... and one WITH it: buffer 0 (the default) must not even read it.
    seamed = _Agenda(busy=busy, consultations=busy)
    same = await availability.free_slots_for_day(
        seamed, day=D0, duration_minutes=30, buffer_minutes=0
    )
    assert _labels(base) == _labels(same) == [
        "08:00", "08:30", "09:00", "09:30", "10:30", "11:00", "11:30"
    ]
    assert seamed.seam_reads == []


async def test_a_calendar_without_the_seam_is_never_padded():
    calendar = _Agenda(busy=[_at("10:00")], with_seam=False)
    slots = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, buffer_minutes=30
    )
    assert "09:30" in _labels(slots) and "10:30" in _labels(slots)


async def test_slot_adjacent_to_appointment_dropped_with_buffer_15():
    consult = [_at("10:00")]
    calendar = _Agenda(busy=consult, consultations=consult)
    slots = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, buffer_minutes=15
    )
    assert _labels(slots) == ["08:00", "08:30", "09:00", "11:00", "11:30"]


async def test_slot_exactly_at_buffer_boundary_is_kept():
    consult = [_at("10:00")]  # 10:00-10:30
    calendar = _Agenda(busy=consult, consultations=consult)
    slots = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, buffer_minutes=30
    )
    # 09:00-09:30 ends exactly 30 min before 10:00; 11:00 starts exactly 30 min after 10:30.
    assert _labels(slots) == ["08:00", "08:30", "09:00", "11:00", "11:30"]
    one_minute_more = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, buffer_minutes=31
    )
    assert _labels(one_minute_more) == ["08:00", "08:30", "11:30"]


async def test_buffer_applies_before_and_after_existing_appointment():
    consult = [_at("09:30"), _at("11:00")]
    calendar = _Agenda(busy=consult, consultations=consult)
    slots = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, buffer_minutes=30
    )
    # before 09:30: 09:00 dropped; after 10:00: 10:00 dropped; around 11:00: 10:30, 11:30 dropped.
    assert _labels(slots) == ["08:00", "08:30"]


async def test_all_day_block_not_padded():
    # A block (lunch, vacation, an all-day event) is busy time but not a consultation: the
    # calendar already hides its own window, and the buffer must not widen it.
    block = [_at("10:00", minutes=60)]
    calendar = _Agenda(busy=block, consultations=[])
    slots = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, buffer_minutes=30
    )
    assert _labels(slots) == ["08:00", "08:30", "09:00", "09:30", "11:00", "11:30"]


async def test_held_slot_is_padded():
    calendar = _Agenda()
    slots = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, holds=[_at("10:00")], buffer_minutes=30
    )
    assert _labels(slots) == ["08:00", "08:30", "09:00", "11:00", "11:30"]
    # Without a buffer the hold only removes its own slot, as before.
    unpadded = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, holds=[_at("10:00")]
    )
    assert "09:30" in _labels(unpadded) and "10:30" in _labels(unpadded)


async def test_a_long_buffer_reaches_over_an_early_visit():
    # 120 minutes after a 06:30-07:30 visit (outside the 08:00-12:00 hours) is 09:30.
    early = _at("06:30", minutes=60)
    calendar = _Agenda(consultations=[early])
    slots = await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, buffer_minutes=120
    )
    # 07:30 + 120 = 09:30: slots before 09:30 are dropped.
    assert _labels(slots) == ["09:30", "10:00", "10:30", "11:00", "11:30"]


async def test_the_seam_is_read_once_with_the_ignored_event_not_set_for_offers():
    consult = [_at("10:00")]
    calendar = _Agenda(busy=consult, consultations=consult)
    await availability.free_slots_for_day(
        calendar, day=D0, duration_minutes=30, buffer_minutes=15
    )
    assert len(calendar.seam_reads) == 1
    start, end, ignore = calendar.seam_reads[0]
    assert ignore is None
    # The read covers the whole day plus the buffer on both sides.
    assert start <= datetime(2026, 10, 5, 0, 0, tzinfo=TZ) - timedelta(minutes=15)
    assert end >= datetime(2026, 10, 6, 0, 0, tzinfo=TZ) + timedelta(minutes=15)


# --- available_day_starts ------------------------------------------------------------------


async def test_day_dropped_when_buffer_removes_every_slot():
    # D0 has a single free slot (09:30), right before a 10:00 consultation: with a buffer
    # the day has nothing left. D1 is untouched.
    consult = [_at("10:00")]
    busy = consult + [_at("08:00"), _at("08:30"), _at("09:00"), _at("10:30"),
                      _at("11:00"), _at("11:30")]
    calendar = _Agenda(open_days=[D0, D1], busy=busy, consultations=consult)
    start = datetime(D0.year, D0.month, D0.day, tzinfo=TZ)
    plain = await availability.available_day_starts(
        calendar, start=start, window_days=20, duration_minutes=30
    )
    assert [d.date() for d in plain] == [D0, D1]
    padded = await availability.available_day_starts(
        calendar, start=start, window_days=20, duration_minutes=30, buffer_minutes=15
    )
    assert [d.date() for d in padded] == [D1]


async def test_day_list_with_a_buffer_is_one_scan_however_many_days_hold_consultations():
    # One consultation on EVERY working day: the buffer must not turn the list into one
    # calendar read per busy day (on Google each read is an API call). The padded
    # consultations travel to the day scan as `extra_busy`; the answer is one seam read and
    # one day scan, and no day is read slot by slot.
    open_days = [D0 + timedelta(days=n) for n in range(180)]
    consult = [_at("10:00", day=d) for d in open_days]
    calendar = _Agenda(open_days=open_days, busy=consult, consultations=consult)
    start = datetime(D0.year, D0.month, D0.day, tzinfo=TZ)
    days = await availability.available_day_starts(
        calendar, start=start, window_days=180, duration_minutes=30, buffer_minutes=15
    )
    assert [d.date() for d in days] == open_days
    assert len(calendar.seam_reads) == 1
    assert calendar.day_scans == 1
    assert calendar.slot_reads == []


async def test_day_list_with_a_buffer_drops_a_day_the_padding_empties_without_a_slot_read():
    consult = [_at("10:00", day=D1)]
    busy = consult + [_at("08:00", day=D1), _at("08:30", day=D1), _at("09:00", day=D1),
                      _at("10:30", day=D1), _at("11:00", day=D1), _at("11:30", day=D1)]
    calendar = _Agenda(open_days=[D0, D1, D2], busy=busy, consultations=consult)
    start = datetime(D0.year, D0.month, D0.day, tzinfo=TZ)
    days = await availability.available_day_starts(
        calendar, start=start, window_days=20, duration_minutes=30, buffer_minutes=15
    )
    assert [d.date() for d in days] == [D0, D2]
    assert calendar.day_scans == 1 and calendar.slot_reads == []


async def test_day_list_with_a_buffer_still_rereads_the_day_a_hold_touches():
    consult = [_at("10:00", day=D1)]
    calendar = _Agenda(open_days=[D0, D1, D2], busy=consult, consultations=consult,
                       hours=(8, 9))
    start = datetime(D0.year, D0.month, D0.day, tzinfo=TZ)
    days = await availability.available_day_starts(
        calendar, start=start, window_days=20, duration_minutes=30,
        holds=[_at("08:00", day=D2)], buffer_minutes=30,
    )
    # D2: the hold pads 08:30 away and takes 08:00 -> nothing left; only D2 was read.
    assert [d.date() for d in days] == [D0, D1]
    assert calendar.slot_reads == [D2]


async def test_day_list_buffer_zero_works_for_a_calendar_that_does_not_know_extra_busy():
    class Plain:
        tzinfo = TZ

        async def list_available_days(self, start_day, days, slot_minutes=None):
            return [datetime(D0.year, D0.month, D0.day, tzinfo=TZ)]

    days = await availability.available_day_starts(
        Plain(), start=datetime(D0.year, D0.month, D0.day, tzinfo=TZ),
        window_days=20, duration_minutes=30, buffer_minutes=0,
    )
    assert [d.date() for d in days] == [D0]


async def test_day_list_with_buffer_zero_makes_no_extra_read():
    calendar = _Agenda(open_days=[D0, D1], consultations=[_at("10:00")])
    start = datetime(D0.year, D0.month, D0.day, tzinfo=TZ)
    days = await availability.available_day_starts(
        calendar, start=start, window_days=20, duration_minutes=30, buffer_minutes=0
    )
    assert [d.date() for d in days] == [D0, D1]
    assert calendar.seam_reads == [] and calendar.slot_reads == []


async def test_day_list_pads_held_windows_too():
    # A hold at 08:00 on D0 pads D0's 08:30 away; D0 has only 08:00/08:30 free.
    calendar = _Agenda(open_days=[D0, D1], hours=(8, 9))
    start = datetime(D0.year, D0.month, D0.day, tzinfo=TZ)
    days = await availability.available_day_starts(
        calendar, start=start, window_days=20, duration_minutes=30,
        holds=[_at("08:00")], buffer_minutes=30,
    )
    assert [d.date() for d in days] == [D1]


async def test_days_with_free_slots_passes_the_buffer_through():
    consult = [_at("08:00", minutes=30)]
    busy = consult + [_at("08:30")]
    calendar = _Agenda(open_days=[D0, D1], hours=(8, 9), busy=busy, consultations=consult)
    start = datetime(D0.year, D0.month, D0.day, tzinfo=TZ)
    days = await availability.days_with_free_slots(
        calendar, start=start, window_days=20, duration_minutes=30, buffer_minutes=15
    )
    assert days == [D1]


# --- scan_free_windows ---------------------------------------------------------------------


async def test_scan_free_windows_pads_and_reads_the_seam_once():
    consult = [_at("10:00"), _at("10:00", day=D1)]
    calendar = _Agenda(open_days=[D0, D1], busy=consult, consultations=consult)
    span = DayRange(first=D0, last=D1)
    scan = await scan_free_windows(
        calendar, span=span, duration_minutes=30, buffer_minutes=30
    )
    assert [(w.day, w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in scan.windows] == [
        (D0, "08:00", "09:30"), (D0, "11:00", "12:00"),
        (D1, "08:00", "09:30"), (D1, "11:00", "12:00"),
    ]
    assert len(calendar.seam_reads) == 1
    plain = await scan_free_windows(
        _Agenda(open_days=[D0, D1], busy=consult, consultations=consult),
        span=span, duration_minutes=30,
    )
    assert [(w.start.strftime("%H:%M"), w.end.strftime("%H:%M")) for w in plain.windows][:2] == [
        ("08:00", "10:00"), ("10:30", "12:00"),
    ]


# --- the confirm / reschedule recheck ------------------------------------------------------


async def test_confirm_recheck_is_false_without_a_buffer_and_reads_nothing():
    calendar = _Agenda(consultations=[_at("10:00")])
    start, end = _at("10:30")
    assert await availability.buffer_taken_at_confirm(
        calendar, start, end, buffer_minutes=0, source="test"
    ) is False
    assert calendar.seam_reads == []


async def test_confirm_recheck_flags_a_time_inside_the_buffer():
    calendar = _Agenda(consultations=[_at("10:00")])
    inside = _at("10:30")
    assert await availability.buffer_taken_at_confirm(
        calendar, *inside, buffer_minutes=15, source="test"
    ) is True
    exactly = _at("10:45")  # gap of exactly 15 minutes: allowed
    assert await availability.buffer_taken_at_confirm(
        calendar, *exactly, buffer_minutes=15, source="test"
    ) is False
    before = _at("09:30")
    assert await availability.buffer_taken_at_confirm(
        calendar, *before, buffer_minutes=15, source="test"
    ) is True


async def test_confirm_recheck_passes_the_ignored_event_to_the_seam():
    calendar = _Agenda(consultations=[])
    await availability.buffer_taken_at_confirm(
        calendar, *_at("10:00"), buffer_minutes=15, source="test", ignore_event_id="evt-moved"
    )
    assert calendar.seam_reads[0][2] == "evt-moved"


async def test_confirm_recheck_counts_other_conversations_holds():
    calendar = _Agenda()
    assert await availability.buffer_taken_at_confirm(
        calendar, *_at("10:30"), buffer_minutes=15, source="test", holds=[_at("10:00")]
    ) is True


async def test_confirm_recheck_fails_open():
    calendar = _Agenda()

    async def boom(*_a, **_k):
        raise RuntimeError("database gone")

    calendar.consultation_windows = boom
    assert await availability.buffer_taken_at_confirm(
        calendar, *_at("10:30"), buffer_minutes=15, source="test"
    ) is False


async def test_confirm_recheck_without_the_seam_is_false():
    calendar = _Agenda(with_seam=False)
    assert await availability.buffer_taken_at_confirm(
        calendar, *_at("10:30"), buffer_minutes=60, source="test"
    ) is False


# --- the DB-backed seam: CalendarService.consultation_windows -------------------------------


@pytest.fixture
def _wired(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


async def _appointment(db, tenant, professional, start, *, patient=True, kind="Consulta",
                       status=AppointmentStatus.SCHEDULED, event_id=None, minutes=30):
    async with db() as session:
        patient_id = None
        if patient:
            row = Patient(
                id=uuid4(), tenant_id=tenant.id, wa_id=f"55119{uuid4().int % 10**8:08d}", name="P"
            )
            session.add(row)
            await session.flush()
            patient_id = row.id
        appointment = Appointment(
            id=uuid4(),
            tenant_id=tenant.id,
            patient_id=patient_id,
            google_event_id=event_id or f"evt{uuid4().hex[:8]}",
            appointment_type=kind,
            professional_id=professional.id if professional is not None else None,
            start_at=start,
            end_at=start + timedelta(minutes=minutes),
            status=status,
        )
        session.add(appointment)
        await session.commit()
        return appointment


async def _windows(calendar, start, *, ignore_event_id=None):
    day_start = start - timedelta(hours=2)
    return await calendar.consultation_windows(
        day_start, day_start + timedelta(hours=6), ignore_event_id=ignore_event_id
    )


def _utc(value):
    return value.astimezone(UTC)


async def test_db_a_patient_appointment_is_a_consultation_window(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dr. A")
    start = tomorrow_at(10)
    await _appointment(db, tenant, doctor, start)
    windows = await _windows(agenda(tenant.id, doctor.id), start)
    expected = (_utc(start), _utc(start + timedelta(minutes=30)))
    assert [(_utc(s), _utc(e)) for s, e in windows] == [expected]


async def test_db_a_block_is_not_a_consultation_window(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dr. A")
    start = tomorrow_at(10)
    await _appointment(db, tenant, doctor, start, patient=False, kind="Bloqueado")
    assert await _windows(agenda(tenant.id, doctor.id), start) == []


async def test_db_cancelled_other_doctor_and_outside_rows_do_not_pad(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dr. A")
    other = await seed_professional(db, tenant, "Dr. B")
    start = tomorrow_at(10)
    await _appointment(db, tenant, doctor, start, status=AppointmentStatus.CANCELLED)
    await _appointment(db, tenant, other, start)
    await _appointment(db, tenant, doctor, start + timedelta(days=3))
    assert await _windows(agenda(tenant.id, doctor.id), start) == []


async def test_db_other_tenants_unrelated_doctor_does_not_pad(db, _wired):
    tenant = await seed_tenant(db)
    stranger_tenant = await seed_tenant(db, name="Outra")
    doctor = await seed_professional(db, tenant, "Dr. A")
    stranger = await seed_professional(db, stranger_tenant, "Dr. A")
    start = tomorrow_at(10)
    await _appointment(db, stranger_tenant, stranger, start)
    assert await _windows(agenda(tenant.id, doctor.id), start) == []


async def test_db_the_clinics_own_agenda_reads_only_rows_without_a_professional(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dr. A")
    start = tomorrow_at(10)
    await _appointment(db, tenant, doctor, start)
    assert await _windows(agenda(tenant.id, None), start) == []
    await _appointment(db, tenant, None, start + timedelta(hours=1))
    assert len(await _windows(agenda(tenant.id, None), start)) == 1


async def test_db_a_siblings_patient_appointment_pads(db, _wired):
    tenant_p = await seed_tenant(db, name="P")
    tenant_c = await seed_tenant(db, name="C")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    async with db() as session:
        row = Agenda(person_key="acct-x")
        session.add(row)
        await session.flush()
        for professional in (x_p, x_c):
            (await session.get(Professional, professional.id)).agenda_id = row.id
        await session.commit()
    start = tomorrow_at(10)
    await _appointment(db, tenant_c, x_c, start)
    windows = await _windows(agenda(tenant_p.id, x_p.id), start)
    assert [_utc(s) for s, _e in windows] == [_utc(start)]
    # ... but a sibling's BLOCK does not.
    await _appointment(db, tenant_c, x_c, start + timedelta(hours=2), patient=False,
                       kind="Bloqueado")
    assert len(await _windows(agenda(tenant_p.id, x_p.id), start)) == 1


async def test_db_the_ignored_event_does_not_pad(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dr. A")
    start = tomorrow_at(10)
    await _appointment(db, tenant, doctor, start, event_id="evt-moved")
    other = await _appointment(db, tenant, doctor, start + timedelta(hours=1))
    windows = await _windows(agenda(tenant.id, doctor.id), start, ignore_event_id="evt-moved")
    assert [_utc(s) for s, _e in windows] == [_utc(other.start_at)]


async def test_db_end_to_end_slots_around_a_real_patient_appointment(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dr. A")
    start = tomorrow_at(10)  # 10:00 clinic time tomorrow
    await _appointment(db, tenant, doctor, start)
    calendar = agenda(tenant.id, doctor.id)
    day = (start.astimezone(calendar.tzinfo)).date()
    plain = await availability.free_slots_for_day(calendar, day=day, duration_minutes=30)
    padded = await availability.free_slots_for_day(
        calendar, day=day, duration_minutes=30, buffer_minutes=30
    )
    # (The row has no calendar event here, so the plain list still offers 10:00 itself.)
    assert {"09:00", "09:30", "10:00", "10:30", "11:00"} <= set(_labels(plain))
    assert {"09:30", "10:00", "10:30"}.isdisjoint(_labels(padded))
    # 09:00 and 11:00 are exactly 30 minutes away: kept.
    assert {"09:00", "11:00"} <= set(_labels(padded))
