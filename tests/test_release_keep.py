"""services/release_keep.py - the CLINIC accepts "Manter horário" (TASK-052, owner 2026-10-12).

Only on an open request (`keep_requested`), before S, with the refund not marked. TAKEN when Google
has someone else's event there, another conversation holds it, or the clinic booked another live
consultation of the same professional over it. Accepted: a NEW confirmed consultation with a
deterministic event id, the PAID deposit re-pointed, the patient told - in one transaction.
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select

from secretaria.models import (
    Appointment,
    AppointmentStatus,
    BookingHold,
    Conversation,
    Patient,
    PixDeposit,
    Tenant,
)
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import booking_promotion, release_keep as rk, release_offer as ro
from tests.payments_support import make_sessionmaker
from tests.release_offer_support import RELEASED_START, T, emails, make_released, utc

TAP = T + timedelta(hours=1)
NOW = T + timedelta(hours=2)


class _Calendar:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.fail = False
        self.created: list[tuple] = []
        self.cancelled: list[str] = []

    async def check_availability(self, start, end, max_results=50):
        if self.fail:
            raise RuntimeError("google down")
        return list(self.events)

    async def create_event(
        self, start, end, summary, description="", reminders=None, *, event_id=None
    ):
        self.created.append((start, end, summary, event_id))
        return {"id": event_id, "htmlLink": f"https://calendar.google.com/{event_id}"}

    async def cancel_event(self, event_id):
        self.cancelled.append(event_id)


@pytest_asyncio.fixture
async def db():
    engine, maker = await make_sessionmaker()
    yield maker
    await engine.dispose()


@pytest.fixture
def calendar(monkeypatch) -> _Calendar:
    owner = _Calendar()

    async def _for_booking(session, tenant, professional_id):
        return owner

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", _for_booking)
    return owner


async def _requested(db, *, ask=True, **kw):
    async with db() as session:
        released = await make_released(session, **kw)
        offer = await ro.open_offer(
            session,
            tenant=released.tenant,
            appointment=released.appointment,
            deposit=released.deposit,
            now=T,
        )
        if ask:
            await ro.request_keep(session, offer=offer, now=TAP)
        await session.commit()
    return released, offer


async def _accept(db, released, offer_id, now=NOW):
    async with db() as session:
        offer = await session.get(DepositReleaseOffer, offer_id)
        tenant = await session.get(Tenant, released.tenant_id)
        return await rk.accept_keep(session, tenant=tenant, offer=offer, resolved_by="Ana", now=now)


async def _state(db, offer_id):
    async with db() as session:
        offer = await session.get(DepositReleaseOffer, offer_id)
        live = list(
            await session.scalars(
                select(Appointment).where(Appointment.status == AppointmentStatus.SCHEDULED)
            )
        )
        deposit = await session.scalar(select(PixDeposit))
        kinds = [e.kind for e in await emails(session, offer_id)]
    return offer, live, deposit, kinds


async def test_accepting_a_free_slot_recreates_the_consultation_with_the_same_deposit(db, calendar):
    released, offer = await _requested(db)

    result = await _accept(db, released, offer.id)

    assert result.outcome == rk.ACCEPT_KEPT
    kept, live, deposit, kinds = await _state(db, offer.id)
    (again,) = live
    assert again.id == result.appointment.id and utc(again.start_at) == RELEASED_START
    assert again.patient_id == released.patient.id and again.confirmation_count == 1
    assert again.google_event_id == rk.keep_event_id(offer.id)
    assert (kept.decision, kept.keep_resolved_by) == ("kept", "Ana")
    assert deposit.appointment_id == again.id
    assert kinds == ["clinic_keep_request", "patient_keep_accepted"]


async def test_no_database_transaction_is_open_while_the_calendar_answers(db, calendar):
    """Session 07: `accept_keep` read the offer, the released row and the holds, then called the
    calendar (Google: seconds, can time out) with that read transaction still open - on Postgres
    an "idle in transaction" connection held through the network call. The transaction is now
    ended before each calendar call; the claim still happens in ONE transaction afterwards."""
    released, offer = await _requested(db)
    seen: list[tuple[str, bool]] = []
    async with db() as session:
        original_check, original_create = calendar.check_availability, calendar.create_event

        async def check_availability(start, end, max_results=50):
            seen.append(("check", session.in_transaction()))
            return await original_check(start, end, max_results)

        async def create_event(
            start, end, summary, description="", reminders=None, *, event_id=None
        ):
            seen.append(("create", session.in_transaction()))
            return await original_create(start, end, summary, description, event_id=event_id)

        calendar.check_availability, calendar.create_event = check_availability, create_event
        live_offer = await session.get(DepositReleaseOffer, offer.id)
        tenant = await session.get(Tenant, released.tenant_id)
        result = await rk.accept_keep(
            session, tenant=tenant, offer=live_offer, resolved_by="Ana", now=NOW
        )

    assert result.outcome == rk.ACCEPT_KEPT
    assert seen == [("check", False), ("create", False)]
    kept, live, deposit, _kinds = await _state(db, offer.id)
    assert kept.decision == "kept" and deposit.appointment_id == live[0].id


async def test_accept_keep_works_on_the_merged_native_agenda(db, monkeypatch):
    from secretaria.services.local_calendar import LocalCalendarService
    from tests._local_agenda import agenda, wire_local_agenda

    released, offer = await _requested(db)
    local = agenda(released.tenant_id)
    wire_local_agenda(monkeypatch, db)

    async def local_calendar(_session, _tenant, _professional_id):
        return local

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", local_calendar)
    result = await _accept(db, released, offer.id)
    assert result.outcome == rk.ACCEPT_KEPT
    assert result.appointment.google_event_id == rk.keep_event_id(offer.id, calendar=local)
    assert isinstance(local, LocalCalendarService)
    assert result.appointment.google_event_id.startswith("loc_")


async def test_accepting_twice_is_the_same_answer(db, calendar):
    released, offer = await _requested(db)
    first = await _accept(db, released, offer.id)

    second = await _accept(db, released, offer.id, now=NOW + timedelta(minutes=5))

    assert second.outcome == rk.ACCEPT_ALREADY and second.appointment.id == first.appointment.id
    assert len(calendar.created) == 1


async def test_a_retry_finding_its_own_event_is_not_a_conflict(db, calendar):
    released, offer = await _requested(db)
    calendar.events = [{"id": rk.keep_event_id(offer.id)}]

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_KEPT


async def test_an_event_in_google_means_taken_and_nothing_changes(db, calendar):
    released, offer = await _requested(db)
    calendar.events = [{"id": "someone-else"}]

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_TAKEN

    still, live, deposit, _kinds = await _state(db, offer.id)
    assert still.decision == "keep_requested" and live == [] and calendar.created == []
    assert deposit.appointment_id == released.appointment.id


async def test_another_conversations_hold_means_taken(db, calendar):
    released, offer = await _requested(db)
    async with db() as session:
        other = Patient(id=uuid4(), tenant_id=released.tenant_id, wa_id="5511900000009")
        session.add(other)
        await session.flush()
        theirs = Conversation(id=uuid4(), tenant_id=released.tenant_id, patient_id=other.id)
        session.add(theirs)
        await session.flush()
        session.add(
            BookingHold(
                tenant_id=released.tenant_id,
                conversation_id=theirs.id,
                patient_id=other.id,
                appointment_type="Consulta",
                start_at=RELEASED_START,
                end_at=RELEASED_START + timedelta(minutes=30),
                expires_at=datetime.now(UTC) + timedelta(minutes=30),
                # TASK-072: only a Pix (payment phase) blocks a slot; a code wait reserves nothing.
                phase="payment",
                charge_id=uuid4(),
            )
        )
        await session.commit()

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_TAKEN


async def test_a_consultation_the_clinic_booked_there_means_taken(db, calendar):
    """The clinic re-occupied the freed slot with another patient."""
    released, offer = await _requested(db)
    async with db() as session:
        session.add(
            Appointment(
                id=uuid4(),
                tenant_id=released.tenant_id,
                google_event_id="evt-clinic",
                appointment_type="Retorno",
                status=AppointmentStatus.SCHEDULED,
                start_at=RELEASED_START + timedelta(minutes=15),
                end_at=RELEASED_START + timedelta(minutes=45),
            )
        )
        await session.commit()

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_TAKEN


async def test_accepting_after_the_start_is_past(db, calendar):
    released, offer = await _requested(db, start=T + timedelta(hours=1, minutes=30))

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_PAST
    assert calendar.created == []


async def test_accepting_without_an_open_request_is_refused(db, calendar):
    released, never = await _requested(db, ask=False)
    assert (await _accept(db, released, never.id)).outcome == rk.ACCEPT_NOT_REQUESTED

    other, moved_on = await _requested(db)
    async with db() as session:
        row = await session.get(DepositReleaseOffer, moved_on.id)
        await ro.choose_reschedule(
            session,
            offer=row,
            conversation_id=other.conversation.id,
            now=TAP + timedelta(minutes=5),
        )
        await session.commit()
    assert (await _accept(db, other, moved_on.id)).outcome == rk.ACCEPT_NOT_REQUESTED
    assert calendar.created == []


async def test_google_down_changes_nothing(db, calendar):
    released, offer = await _requested(db)
    calendar.fail = True

    assert (await _accept(db, released, offer.id)).outcome == rk.ACCEPT_UNAVAILABLE
    still, live, _deposit, _kinds = await _state(db, offer.id)
    assert still.decision == "keep_requested" and live == []


async def test_losing_the_claim_deletes_the_event_it_created(db, calendar):
    released, offer = await _requested(db)
    async with db() as session:
        stale = await session.get(DepositReleaseOffer, offer.id)  # read while still requested
    async with db() as session:  # meanwhile the patient cancels
        row = await session.get(DepositReleaseOffer, offer.id)
        await ro.choose_cancel(session, offer=row, now=NOW)
        await session.commit()

    async with db() as session:
        tenant = await session.get(Tenant, released.tenant_id)
        result = await rk.accept_keep(
            session, tenant=tenant, offer=stale, resolved_by="Ana", now=NOW
        )

    assert result.outcome == rk.ACCEPT_NOT_REQUESTED
    assert calendar.cancelled == [rk.keep_event_id(offer.id)]
    still, live, deposit, _kinds = await _state(db, offer.id)
    assert still.decision == "cancelled" and live == []
    assert deposit.appointment_id == released.appointment.id
