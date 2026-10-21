# ruff: noqa: F811
"""A patient books end to end on the secretarIA agenda (native agenda, 2026-10-20).

The Portal code-gate path of tests/test_booking_code_gate.py, with NOTHING faked on the
calendar side: the clinic has no Google credential, so the confirmation re-check, the
hold, the promotion at the verified code and the slot it then occupies all run on
services/local_calendar.py through the real factories. Plus the race the local agenda
closes and Google cannot: a second patient whose re-check went stale.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from secretaria.models import BookingHold, FlowState
from secretaria.models.calendar_event import CalendarEvent
from secretaria.services import booking_hold as holds, flow_router as fr
from secretaria.services.calendar import CalendarSlotTakenError
from secretaria.services.local_calendar import LocalCalendarService, create_event_exclusive
from secretaria.workers import tasks
from tests._local_agenda import agenda, wire_local_agenda
from tests.test_booking_code_gate import (  # noqa: F401 - fixtures
    EXTERNAL_ID,
    OTHER_EXTERNAL_ID,
    _appointments,
    _conversation_snapshot,
    _hold_rows,
    _reply,
    _seed_conversation,
    _seed_tenant,
    _wire,
    calls,
    db,
)

# 10:00 in São Paulo on the snapshot's day.
SLOT_ISO = "2099-01-05T10:00"
SLOT_UTC = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _local(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


async def _calendar_rows(db, tenant) -> list[CalendarEvent]:
    async with db() as session:
        return list(
            await session.scalars(select(CalendarEvent).where(CalendarEvent.tenant_id == tenant.id))
        )


def _gate(tenant, conversation, patient, *, armed: bool, external_id=EXTERNAL_ID):
    return holds.BookingGate(
        tenant_id=tenant.id,
        conversation_id=conversation.id,
        patient_id=getattr(patient, "id", None),
        external_id=external_id,
        armed=armed,
    )


@pytest.mark.asyncio
async def test_a_portal_patient_books_on_the_secretaria_agenda_end_to_end(db, calls):
    tenant = await _seed_tenant(db)
    patient, conversation = await _seed_conversation(db, tenant)
    snapshot = _conversation_snapshot(SLOT_ISO)
    snapshot.id = conversation.id
    calendar = agenda(tenant.id)

    # 1. "Confirmar": the re-check reads the local agenda, the gate holds the slot.
    held = await fr._handle_confirmation(
        snapshot, tenant, calendar, fr.LABEL_CONFIRM, "Maria",
        gate=_gate(tenant, conversation, patient, armed=True),
    )
    assert held.flow_state is FlowState.AWAITING_EMAIL_CODE
    assert await _calendar_rows(db, tenant) == []  # nothing booked before the code
    [hold] = await _hold_rows(db, tenant)
    # SQLite (the test engine) keeps the -03:00 wall time and drops its offset; Postgres
    # stores the instant. Store the instant, as production does.
    assert hold.start_at.replace(tzinfo=None) == datetime(2099, 1, 5, 10, 0)
    async with db() as session, session.begin():
        row = await session.get(BookingHold, hold.id)
        row.start_at, row.end_at = SLOT_UTC, SLOT_UTC + timedelta(minutes=30)

    # 2. The verified code promotes the hold - the worker resolves the agenda itself.
    extra = await tasks._promote_booking_hold(
        _reply(conversation), tenant=tenant, waba_token=None, redis=None
    )

    assert "Pronto! Seu agendamento está confirmado." in extra
    assert "05/01/2099 às 10:00" in extra
    assert "Adicionar à sua agenda" in extra  # the patient's own link is still sent
    [appointment] = await _appointments(db, tenant)
    [row] = await _calendar_rows(db, tenant)
    assert appointment.google_event_id == row.id and row.id.startswith("loc_")
    assert appointment.google_event_link is None
    assert row.professional_id is None and row.status == "confirmed"
    assert row.start_at.replace(tzinfo=UTC) == SLOT_UTC
    assert await _hold_rows(db, tenant) == []
    assert calls.events == []  # no Google (or fake) calendar was ever written

    # 3. The slot is taken now, for every other patient.
    assert await calendar.has_conflict(SLOT_UTC, SLOT_UTC + timedelta(minutes=30))
    slots = await calendar.list_free_slots(day=SLOT_UTC.astimezone(calendar.tzinfo), max_slots=20)
    assert "10:00" not in [slot["label"] for slot in slots]
    _, other = await _seed_conversation(db, tenant, external_id=OTHER_EXTERNAL_ID)
    other_snapshot = _conversation_snapshot(SLOT_ISO)
    other_snapshot.id = other.id
    second = await fr._handle_confirmation(
        other_snapshot, tenant, calendar, fr.LABEL_CONFIRM, "Rita",
        gate=_gate(tenant, other, None, armed=False, external_id="5511900000000"),
    )
    assert second.appointment is None
    assert len(await _calendar_rows(db, tenant)) == 1


@pytest.mark.asyncio
async def test_a_stale_recheck_still_cannot_double_book(db, calls, monkeypatch):
    """The re-check and the write are two reads of Google; here the write itself refuses."""
    tenant = await _seed_tenant(db)
    calendar = agenda(tenant.id)
    await calendar.create_event(SLOT_UTC, SLOT_UTC + timedelta(minutes=30), "Consulta - Ana")
    _, conversation = await _seed_conversation(db, tenant)
    snapshot = _conversation_snapshot(SLOT_ISO)
    snapshot.id = conversation.id

    async def _stale(*_args, **_kwargs):
        return False  # the re-check ran a moment before the other booking landed

    monkeypatch.setattr(fr, "slot_taken_at_confirm", _stale)
    result = await fr._handle_confirmation(
        snapshot, tenant, calendar, fr.LABEL_CONFIRM, "Maria",
        gate=_gate(tenant, conversation, None, armed=False, external_id="5511988887777"),
    )

    assert result.appointment is None  # offered the day again instead
    assert result.action == "reply"
    assert len(await _calendar_rows(db, tenant)) == 1


@pytest.mark.asyncio
async def test_whatsapp_commits_straight_onto_the_secretaria_agenda(db, calls):
    tenant = await _seed_tenant(db)
    _, conversation = await _seed_conversation(db, tenant)
    snapshot = _conversation_snapshot(SLOT_ISO)
    snapshot.id = conversation.id

    result = await fr._handle_confirmation(
        snapshot, tenant, agenda(tenant.id), fr.LABEL_CONFIRM, "Maria",
        gate=_gate(tenant, conversation, None, armed=False, external_id="5511988887777"),
    )

    assert result.appointment["google_event_id"].startswith("loc_")
    assert result.appointment["google_event_link"] is None
    with pytest.raises(CalendarSlotTakenError):
        await create_event_exclusive(
            agenda(tenant.id), start=SLOT_UTC, end=SLOT_UTC + timedelta(minutes=30), summary="X"
        )
    assert isinstance(agenda(tenant.id), LocalCalendarService)
