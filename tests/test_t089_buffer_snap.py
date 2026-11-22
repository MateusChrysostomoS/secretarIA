# ruff: noqa: F811
"""TASK-089: with the doctor's buffer, the first time after a consultation is the consultation's
end PLUS the buffer - not the next point of the day's fixed grid.

Reported on the test clinic (2026-11-21): a Retorno 08:00-08:30, buffer 5, and the patient was
offered 08:40 (a 40-minute service on the 08:00/08:40 grid) instead of 08:35. With a 30-minute
service the grid would have offered 09:00: 25 free minutes lost for a 5-minute buffer.

Rule under test: with a buffer, the free-slot walk jumps to the END of the taken time that blocked
a slot (consultation/hold padded by the buffer, or any busy event) instead of advancing one slot.
Buffer 0 never snaps: the grid is exactly what it always was.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from datetime import datetime, timedelta  # noqa: E402
from uuid import uuid4  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import Appointment, AppointmentStatus, Patient  # noqa: E402
from secretaria.services import availability  # noqa: E402
from tests._local_agenda import (  # noqa: E402
    agenda,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: E402, F401

TZ = ZoneInfo("America/Sao_Paulo")


def _t(hhmm: str) -> datetime:
    hour, minute = (int(part) for part in hhmm.split(":"))
    return datetime(2026, 11, 24, hour, minute, tzinfo=TZ)


def _labels(slots) -> list[str]:
    return [s["label"] if isinstance(s, dict) else s.strftime("%H:%M") for s in slots]


@pytest.fixture
def _wired(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


# --- the walk itself ------------------------------------------------------------------------


def _walk(calendar, busy, minutes, *, snap):
    kwargs = {"snap_after_busy": True} if snap else {}
    return calendar._walk_free_slots(
        [(_t("08:00"), _t("12:00"))],
        busy,
        timedelta(minutes=minutes),
        _t("00:00"),
        50,
        **kwargs,
    )


async def test_walk_without_snap_is_the_old_fixed_grid(db, _wired):
    tenant = await seed_tenant(db)
    calendar = agenda(tenant.id)
    # consultation 08:00-08:30 + buffer 5 => taken 07:55-08:35
    slots = _walk(calendar, [(_t("07:55"), _t("08:35"))], 30, snap=False)
    assert _labels(slots) == ["09:00", "09:30", "10:00", "10:30", "11:00", "11:30"]


async def test_walk_with_snap_starts_right_after_the_padding(db, _wired):
    tenant = await seed_tenant(db)
    calendar = agenda(tenant.id)
    slots = _walk(calendar, [(_t("07:55"), _t("08:35"))], 30, snap=True)
    assert _labels(slots) == ["08:35", "09:05", "09:35", "10:05", "10:35", "11:05"]


async def test_walk_with_snap_a_40_minute_service_is_the_reported_case(db, _wired):
    tenant = await seed_tenant(db)
    calendar = agenda(tenant.id)
    slots = _walk(calendar, [(_t("07:55"), _t("08:35"))], 40, snap=True)
    assert _labels(slots)[0] == "08:35"  # the bug offered 08:40


async def test_walk_with_snap_never_skips_a_free_start(db, _wired):
    tenant = await seed_tenant(db)
    calendar = agenda(tenant.id)
    slots = _walk(calendar, [(_t("09:50"), _t("10:20"))], 30, snap=True)
    # 08:00-09:30 on the grid; 09:30-10:00 touches the taken time, so the walk jumps to 10:20.
    assert _labels(slots) == ["08:00", "08:30", "09:00", "10:20", "10:50", "11:20"]


async def test_walk_with_snap_keeps_the_clinic_clock_when_the_taken_time_comes_in_utc(db, _wired):
    from datetime import UTC

    tenant = await seed_tenant(db)
    calendar = agenda(tenant.id)
    taken_utc = (_t("07:55").astimezone(UTC), _t("08:35").astimezone(UTC))  # agenda rows are UTC
    slots = _walk(calendar, [taken_utc], 30, snap=True)
    assert _labels(slots)[0] == "08:35"
    assert slots[0]["start"] == "2026-11-24T08:35"


async def test_walk_with_snap_and_nothing_busy_is_the_plain_grid(db, _wired):
    tenant = await seed_tenant(db)
    calendar = agenda(tenant.id)
    assert _labels(_walk(calendar, [], 30, snap=True)) == _labels(
        _walk(calendar, [], 30, snap=False)
    )


# --- the real agenda, end to end ------------------------------------------------------------


async def _consultation(db, tenant, doctor, calendar, start, minutes=30):
    event = await calendar.create_event(
        start=start, end=start + timedelta(minutes=minutes), summary="Consulta - P"
    )
    async with db() as session:
        patient = Patient(
            id=uuid4(), tenant_id=tenant.id, wa_id=f"55119{uuid4().int % 10**8:08d}", name="P"
        )
        session.add(patient)
        await session.flush()
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=tenant.id,
                patient_id=patient.id,
                google_event_id=event["id"],
                appointment_type="Consulta",
                professional_id=doctor.id,
                start_at=start,
                end_at=start + timedelta(minutes=minutes),
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()


async def test_db_first_time_after_a_consultation_is_its_end_plus_the_buffer(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. A")
    calendar = agenda(tenant.id, doctor.id)
    start = tomorrow_at(8)
    await _consultation(db, tenant, doctor, calendar, start)
    day = start.astimezone(calendar.tzinfo).date()

    plain = await availability.free_slots_for_day(calendar, day=day, duration_minutes=30)
    assert _labels(plain)[0] == "08:30"  # no buffer: exactly what it always was

    for minutes in (30, 40):
        slots = await availability.free_slots_for_day(
            calendar, day=day, duration_minutes=minutes, buffer_minutes=5
        )
        assert _labels(slots)[0] == "08:35", minutes


async def test_db_buffered_free_slots_for_the_ai_tools_snap_too(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. A")
    calendar = agenda(tenant.id, doctor.id)
    start = tomorrow_at(8)
    await _consultation(db, tenant, doctor, calendar, start)
    day = start.astimezone(calendar.tzinfo).replace(hour=0, minute=0, second=0, microsecond=0)

    slots = await availability.buffered_free_slots(
        calendar, day=day, max_slots=3, held=(), buffer_minutes=5, slot_minutes=30
    )
    assert _labels(slots) == ["08:35", "09:05", "09:35"]


async def test_db_confirm_accepts_the_snapped_time_and_still_refuses_one_minute_closer(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. A")
    calendar = agenda(tenant.id, doctor.id)
    start = tomorrow_at(8)
    await _consultation(db, tenant, doctor, calendar, start)
    snapped = start + timedelta(minutes=35)
    too_close = start + timedelta(minutes=34)
    kwargs = {"source": "test", "buffer_minutes": 5}
    assert not await availability.buffer_taken_at_confirm(
        calendar, snapped, snapped + timedelta(minutes=30), **kwargs
    )
    assert await availability.buffer_taken_at_confirm(
        calendar, too_close, too_close + timedelta(minutes=30), **kwargs
    )


# --- the typed time and the day list --------------------------------------------------------


async def test_db_a_typed_time_on_the_old_grid_still_matches_with_include_grid(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. A")
    calendar = agenda(tenant.id, doctor.id)
    start = tomorrow_at(8)
    await _consultation(db, tenant, doctor, calendar, start)
    day = start.astimezone(calendar.tzinfo).date()

    tapped = await availability.free_slots_for_day(
        calendar, day=day, duration_minutes=30, buffer_minutes=5
    )
    assert "09:00" not in _labels(tapped)  # the list the patient taps from is the snapped one
    typed = await availability.free_slots_for_day(
        calendar, day=day, duration_minutes=30, buffer_minutes=5, include_grid=True
    )
    # "amanhã às 9h" is free and respects the buffer: it must still match; 08:35 is there too.
    assert {"08:35", "09:00", "09:30"} <= set(_labels(typed))
    assert _labels(typed) == sorted(_labels(typed))


async def test_db_include_grid_without_a_buffer_changes_nothing(db, _wired):
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. A")
    calendar = agenda(tenant.id, doctor.id)
    start = tomorrow_at(8)
    await _consultation(db, tenant, doctor, calendar, start)
    day = start.astimezone(calendar.tzinfo).date()
    plain = await availability.free_slots_for_day(calendar, day=day, duration_minutes=30)
    same = await availability.free_slots_for_day(
        calendar, day=day, duration_minutes=30, include_grid=True
    )
    assert _labels(plain) == _labels(same)


async def test_db_the_day_list_keeps_a_day_that_only_the_snapped_walk_can_fill(db, _wired):
    """Saturday-like day: 60-minute service, buffer 15, consultations 08:00-09:00 and
    10:30-11:30. The old grid (08:00, 09:00, 10:00, 11:00...) finds nothing, 09:15 fits."""
    tenant = await seed_tenant(db)
    doctor = await seed_professional(db, tenant, "Dra. A")
    calendar = agenda(tenant.id, doctor.id)
    day_start = tomorrow_at(8)
    await _consultation(db, tenant, doctor, calendar, day_start, minutes=60)
    await _consultation(db, tenant, doctor, calendar, tomorrow_at(10, 30), minutes=60)
    day = day_start.astimezone(calendar.tzinfo).date()
    slots = await availability.free_slots_for_day(
        calendar, day=day, duration_minutes=60, buffer_minutes=15
    )
    assert _labels(slots)[0] == "09:15"
