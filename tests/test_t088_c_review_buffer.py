# ruff: noqa: F811
"""TASK-088 §B, review round C: three findings of the buffer review.

1. A Pix hold of the person's OTHER clinic (shared agenda, TASK-083) pads like a consultation.
2. `service_change._inside_patient_buffer` reads the doctor in its own short session and never
   touches the caller's transaction.
3. The `f9a1d7c4e2b6` migration file carries no BOM and documents its deploy order.
"""

import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
os.environ.setdefault("META_PHONE_NUMBER_ID", "1234567890")
os.environ.setdefault("OPENAI_API_KEY", "test-openai-key")

from datetime import UTC, datetime, timedelta  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

import pytest  # noqa: E402

from secretaria.models import Agenda, BookingHold, Professional  # noqa: E402
from secretaria.services import availability  # noqa: E402
from tests._local_agenda import (  # noqa: E402
    agenda,
    seed_professional,
    seed_tenant,
    tomorrow_at,
    wire_local_agenda,
)
from tests._reminder_fixtures import db  # noqa: E402, F401

ROOT = Path(__file__).resolve().parents[1]


# --- 1. a sibling clinic's Pix hold pads ------------------------------------------------------


@pytest.fixture
def _wired(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


async def _shared_pair(db):
    """Dr. X at clinics P and C, one shared agenda. Returns (tenant_p, x_p, tenant_c, x_c)."""
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
    return tenant_p, x_p, tenant_c, x_c


async def _hold(db, tenant, professional, start, *, phase="payment", minutes=30, ttl=15):
    async with db() as session:
        session.add(
            BookingHold(
                id=uuid4(),
                tenant_id=tenant.id,
                conversation_id=None,
                professional_id=professional.id,
                start_at=start,
                end_at=start + timedelta(minutes=minutes),
                expires_at=datetime.now(UTC) + timedelta(minutes=ttl),
                phase=phase,
            )
        )
        await session.commit()


async def _windows(calendar, start):
    day_start = start - timedelta(hours=2)
    return await calendar.consultation_windows(day_start, day_start + timedelta(hours=6))


def _utc(value):
    return value.astimezone(UTC)


async def test_a_sibling_clinics_pix_hold_is_a_consultation_window(db, _wired):
    tenant_p, x_p, tenant_c, x_c = await _shared_pair(db)
    start = tomorrow_at(14)
    await _hold(db, tenant_c, x_c, start)
    windows = await _windows(agenda(tenant_p.id, x_p.id), start)
    assert [(_utc(s), _utc(e)) for s, e in windows] == [
        (_utc(start), _utc(start + timedelta(minutes=30)))
    ]


async def test_only_blocking_live_holds_of_the_same_person_pad(db, _wired):
    tenant_p, x_p, tenant_c, x_c = await _shared_pair(db)
    stranger_tenant = await seed_tenant(db, name="S")
    stranger = await seed_professional(db, stranger_tenant, "Dr. X")
    start = tomorrow_at(14)
    await _hold(db, tenant_c, x_c, start, phase="code")  # a code in transit reserves nothing
    await _hold(db, tenant_c, x_c, start, ttl=-5)  # expired
    await _hold(db, stranger_tenant, stranger, start)  # an unrelated doctor
    assert await _windows(agenda(tenant_p.id, x_p.id), start) == []


async def test_a_sibling_hold_leaves_only_a_bare_window(db, _wired):
    tenant_p, x_p, tenant_c, x_c = await _shared_pair(db)
    start = tomorrow_at(14)
    await _hold(db, tenant_c, x_c, start)
    windows = await _windows(agenda(tenant_p.id, x_p.id), start)
    assert windows and all(
        isinstance(w, tuple) and len(w) == 2 and all(isinstance(v, datetime) for v in w)
        for w in windows
    )


async def test_a_sibling_hold_and_its_promoted_row_are_one_window(db, _wired):
    from tests.test_booking_buffer import _appointment

    tenant_p, x_p, tenant_c, x_c = await _shared_pair(db)
    start = tomorrow_at(14)
    await _hold(db, tenant_c, x_c, start)
    await _appointment(db, tenant_c, x_c, start)
    assert len(await _windows(agenda(tenant_p.id, x_p.id), start)) == 1


async def test_end_to_end_a_slot_next_to_a_sibling_pix_hold_is_not_offered(db, _wired):
    tenant_p, x_p, tenant_c, x_c = await _shared_pair(db)
    start = tomorrow_at(14)  # 14:00 clinic time tomorrow, held at clinic C
    await _hold(db, tenant_c, x_c, start)
    calendar = agenda(tenant_p.id, x_p.id)
    day = start.astimezone(calendar.tzinfo).date()
    padded = await availability.free_slots_for_day(
        calendar, day=day, duration_minutes=30, buffer_minutes=30
    )
    labels = {s.strftime("%H:%M") for s in padded}
    assert {"13:30", "14:00", "14:30"}.isdisjoint(labels)
    assert {"13:00", "15:00"} <= labels


async def test_a_consultation_booked_by_reception_without_a_patient_record_pads(db, _wired):
    # Agenda "Nova consulta" with the patient typed as name/phone: patient_id None, no type or a
    # service name. It is a consultation; only the clinic BLOCK marker is not.
    from tests.test_booking_buffer import _appointment

    tenant = await seed_tenant(db, name="P")
    doctor = await seed_professional(db, tenant, "Dr. X")
    start = tomorrow_at(14)
    await _appointment(db, tenant, doctor, start, patient=False, kind=None)
    await _appointment(
        db, tenant, doctor, start + timedelta(hours=2), patient=False, kind="Consulta"
    )
    await _appointment(
        db, tenant, doctor, start + timedelta(hours=3), patient=False, kind="Bloqueado"
    )
    windows = await _windows(agenda(tenant.id, doctor.id), start)
    assert [(_utc(s), _utc(e)) for s, e in windows] == [
        (_utc(start), _utc(start + timedelta(minutes=30))),
        (_utc(start + timedelta(hours=2)), _utc(start + timedelta(hours=2, minutes=30))),
    ]


async def test_end_to_end_a_slot_next_to_a_reception_booking_is_not_offered(db, _wired):
    from tests.test_booking_buffer import _appointment

    tenant = await seed_tenant(db, name="P")
    doctor = await seed_professional(db, tenant, "Dr. X")
    start = tomorrow_at(14)
    await _appointment(db, tenant, doctor, start, patient=False, kind=None)
    calendar = agenda(tenant.id, doctor.id)
    day = start.astimezone(calendar.tzinfo).date()
    padded = await availability.free_slots_for_day(
        calendar, day=day, duration_minutes=30, buffer_minutes=30
    )
    labels = {s.strftime("%H:%M") for s in padded}
    assert {"13:30", "14:00", "14:30"}.isdisjoint(labels)
    assert {"13:00", "15:00"} <= labels


# --- 2. the patient-buffer check never touches the caller's transaction --------------------


class _TouchedSession:
    """The caller's open transaction: any use at all is a bug (recorded, since the code under
    test is fail-open and would swallow an exception)."""

    def __init__(self):
        self.touched: list[str] = []

    def __getattr__(self, name):
        self.touched.append(name)
        raise RuntimeError(f"the caller's session was used: {name}")


class _OwnSession:
    def __init__(self, professional=None, error=None):
        self._professional = professional
        self._error = error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, _model, _key):
        if self._error is not None:
            raise self._error
        return self._professional


class _Cal:
    tzinfo = UTC

    def __init__(self, consultations):
        self._consultations = consultations

    async def consultation_windows(self, start, end, *, ignore_event_id=None):
        return list(self._consultations)


def _service_change_fixture(monkeypatch, factory):
    from secretaria.services.payments import service_change

    async def _no_windows(*_a, **_k):
        return []

    monkeypatch.setattr(service_change.booking_hold, "held_windows", _no_windows)
    monkeypatch.setattr(service_change, "async_session_factory", factory, raising=False)
    tenant = SimpleNamespace(id=uuid4())
    appointment = SimpleNamespace(professional_id=uuid4())
    return service_change, tenant, appointment


def _win(hour, minute=0):
    start = datetime(2026, 10, 5, hour, minute, tzinfo=UTC)
    return start, start + timedelta(minutes=30)


async def test_patient_buffer_reads_the_doctor_in_its_own_session(monkeypatch):
    holder = {}

    def factory():
        return _OwnSession(holder["doctor"])

    service_change, tenant, appointment = _service_change_fixture(monkeypatch, factory)
    holder["doctor"] = SimpleNamespace(tenant_id=tenant.id, buffer_minutes=30)
    visit = [_win(8, 30)]
    caller = _TouchedSession()
    start, end = _win(9)
    inside = await service_change._inside_patient_buffer(
        caller, tenant, appointment, _Cal(visit), start, end, "evt-moved"
    )
    assert inside is True
    start, end = _win(9, 30)
    outside = await service_change._inside_patient_buffer(
        caller, tenant, appointment, _Cal(visit), start, end, "evt-moved"
    )
    assert outside is False
    assert caller.touched == []


async def test_a_failing_doctor_read_is_fail_open_and_leaves_the_caller_session_alone(monkeypatch):
    def factory():
        return _OwnSession(error=RuntimeError("db down"))

    service_change, tenant, appointment = _service_change_fixture(monkeypatch, factory)
    caller = _TouchedSession()
    start, end = _win(9)
    assert (
        await service_change._inside_patient_buffer(
            caller, tenant, appointment, _Cal([_win(8, 30)]), start, end, None
        )
        is False
    )
    assert caller.touched == []


# --- 3. the migration file ---------------------------------------------------------------------

MIGRATION = ROOT / "migrations" / "versions" / "f9a1d7c4e2b6_conversation_insurance_auto.py"


def test_the_insurance_auto_migration_has_no_bom():
    assert not MIGRATION.read_bytes().startswith(b"\xef\xbb\xbf")


def test_the_insurance_auto_migration_documents_order_and_downgrades_in_batch():
    text = MIGRATION.read_text(encoding="utf-8")
    assert "Create Date:" in text
    assert "migração antes da API e do worker" in text
    assert "batch_alter_table" in text
