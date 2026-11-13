# ruff: noqa: F811
"""TASK-083 spec §7 items 3 and 5: every patient write path, on a GOOGLE agenda of a shared person.

Clinic P already booked Dr. X for a window (its appointment row is committed). Clinic C keeps
Dr. X's agenda on Google - a different Google account, so C's Google knows nothing of P. For
each patient write path of C the usual last re-check is made STALE (it ran a moment before P's
booking landed), so the write itself is the only protection left: it must refuse, and the path
must answer exactly as it answers a taken re-check - nothing written on C's Google, no
appointment, the patient offered another time.

Plus spec §7 item 5: `release_keep` and the service-change window check count the sibling's
appointments (database checks the calendar read does not cover).
"""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select

from secretaria.ai import tools
from secretaria.models import (
    Agenda,
    Appointment,
    AppointmentStatus,
    PixDeposit,
    PixDepositStatus,
    Professional,
    Tenant,
)
from secretaria.models.deposit_release_offer import DepositReleaseOffer
from secretaria.services import (
    booking_hold as holds,
    booking_promotion,
    flow_router as fr,
    release_keep as rk,
    release_offer as ro,
)
from secretaria.services.calendar import CalendarService, CalendarSlotTakenError
from secretaria.services.payments import service_change
from secretaria.services.tenant_config import load_tenant_config
from secretaria.workers import tasks
from secretaria.workers.shared import booking_hold as worker_holds
from secretaria.workers.shared.booking_hold import SlotTakenAtPromotion
from tests._google_fake import install_google_fake
from tests._local_agenda import seed_professional, seed_tenant, wire_local_agenda
from tests._patching import workers_ns
from tests.release_offer_support import RELEASED_START, T, make_released
from tests.test_booking_code_gate import (  # noqa: F401 - fixtures
    _conversation_snapshot,
    _reply,
    _seed_conversation,
    _wire,
    calls,
    db,
)

# 10:00 in São Paulo on the flow snapshot's day (tests/test_booking_code_gate.py).
SLOT_ISO = "2099-01-05T10:00"
SLOT_UTC = datetime(2099, 1, 5, 13, 0, tzinfo=UTC)
SLOT_END = SLOT_UTC + timedelta(minutes=30)


@pytest.fixture(autouse=True)
def _agenda_wiring(db, monkeypatch):
    wire_local_agenda(monkeypatch, db)


@pytest.fixture
def google(monkeypatch):
    return install_google_fake(monkeypatch)


# --------------------------------------------------------------------------
# World: P booked Dr. X; C has Dr. X on Google
# --------------------------------------------------------------------------


async def _link(db, *professional_ids):
    async with db() as session:
        row = Agenda(person_key=f"acct-{uuid4().hex[:8]}")
        session.add(row)
        await session.flush()
        for professional_id in professional_ids:
            (await session.get(Professional, professional_id)).agenda_id = row.id
        await session.commit()


async def _book(db, tenant_id, professional_id, start=SLOT_UTC, end=SLOT_END):
    async with db() as session:
        session.add(
            Appointment(
                tenant_id=tenant_id,
                professional_id=professional_id,
                google_event_id=f"evt-{uuid4().hex[:8]}",
                appointment_type="Consulta",
                start_at=start,
                end_at=end,
                status=AppointmentStatus.SCHEDULED,
            )
        )
        await session.commit()


async def _google_of(db, tenant_id, professional_id, token="tok-c") -> CalendarService:
    async with db() as session:
        config = await load_tenant_config(session, await session.get(Tenant, tenant_id))
    calendar = CalendarService.from_tenant_config(
        replace(config, google_refresh_token=token), professional_id=professional_id
    )
    assert calendar.supports_exclusive_write is True  # a shared person's Google agenda
    return calendar


async def _world(db):
    tenant_p = await seed_tenant(db, name="Clínica P")
    tenant_c = await seed_tenant(db, name="Clínica C")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    x_c = await seed_professional(db, tenant_c, "Dr. X")
    await _link(db, x_p.id, x_c.id)
    await _book(db, tenant_p.id, x_p.id)
    return tenant_c, x_c, await _google_of(db, tenant_c.id, x_c.id)


async def _appointments_of(db, tenant_id) -> list[Appointment]:
    async with db() as session:
        return list(
            await session.scalars(select(Appointment).where(Appointment.tenant_id == tenant_id))
        )


async def _stale(*_args, **_kwargs):
    return False  # the re-check ran a moment before P's booking landed


# --------------------------------------------------------------------------
# The conversation flow: "Confirmar" and the manage-flow reschedule
# --------------------------------------------------------------------------


async def test_flow_confirm_is_refused_on_google_and_reoffers(db, calls, google, monkeypatch):
    tenant_c, _x_c, cal_c = await _world(db)
    _, conversation = await _seed_conversation(db, tenant_c)
    snapshot = _conversation_snapshot(SLOT_ISO)
    snapshot.id = conversation.id
    monkeypatch.setattr(fr, "slot_taken_at_confirm", _stale)

    result = await fr._handle_confirmation(
        snapshot, tenant_c, cal_c, fr.LABEL_CONFIRM, "Maria",
        gate=holds.BookingGate(
            tenant_id=tenant_c.id, conversation_id=conversation.id, patient_id=None,
            external_id="5511988887777", armed=False,
        ),
    )

    assert result.appointment is None and result.action == "reply"
    assert google.events_of("tok-c") == []


async def test_flow_manage_reschedule_is_refused_on_google(db, calls, google, monkeypatch):
    tenant_c, x_c, cal_c = await _world(db)
    own_start = SLOT_UTC + timedelta(hours=4)
    own = await cal_c.create_event(own_start, own_start + timedelta(minutes=30), "Consulta")
    appointment_id = uuid4()
    appointments = [{
        "id": str(appointment_id),
        "google_event_id": own["id"],
        "appointment_type": "Consulta",
        "start_at": own_start,
        "end_at": own_start + timedelta(minutes=30),
        "professional_id": str(x_c.id),
    }]
    conversation = SimpleNamespace(
        id=uuid4(),
        flow_state=fr.FlowState.MANAGE_BOOKING,
        flow_step=fr.STEP_MANAGE_CONFIRM,
        flow_managing_appointment_id=appointment_id,
        flow_selected_day="2099-01-05",
        flow_selected_slot=SLOT_ISO,
        flow_selected_type=None,
        flow_selected_professional_id=None,
        flow_selected_insurance=None,
    )
    monkeypatch.setattr(fr, "slot_taken_at_confirm", _stale)

    result = await fr._manage_reschedule(
        conversation, tenant_c, cal_c, fr.LABEL_CONFIRM, appointments
    )

    assert result.action == "reply"  # offered the day again, not "calendar_unavailable"
    assert google.patches == []
    [event] = google.events_of("tok-c")
    assert event["start"]["dateTime"] == own["start"]["dateTime"]  # did not move


# --------------------------------------------------------------------------
# The AI tools
# --------------------------------------------------------------------------


async def test_the_ai_booking_tool_is_refused_on_google_and_hands_back(
    db, calls, google, monkeypatch
):
    _tenant_c, x_c, cal_c = await _world(db)

    class _HandBack(Exception):
        pass

    async def _hand_back(cal, start, appointment_type, professional_id):
        return _HandBack(start)

    monkeypatch.setattr(tools, "_slot_taken_hand_back", _hand_back)

    with pytest.raises(_HandBack):
        await tools._create_booking_event(
            cal_c, SLOT_UTC, SLOT_END, summary="Consulta - Maria", description="",
            tool_name="create_event", appointment_type="Consulta", professional_id=x_c.id,
        )
    assert google.events_of("tok-c") == []


# --------------------------------------------------------------------------
# Hold promotion at the verified code, and the Pix promotion
# --------------------------------------------------------------------------


async def test_the_code_gate_promotion_is_refused_on_google(db, calls, google, monkeypatch):
    tenant_c, x_c, cal_c = await _world(db)
    patient, conversation = await _seed_conversation(db, tenant_c)
    await holds.place_hold(
        tenant_id=tenant_c.id, conversation_id=conversation.id, patient_id=patient.id,
        professional_id=x_c.id, appointment_type="Consulta", insurance=None,
        start_at=SLOT_UTC, end_at=SLOT_END, ttl_minutes=10,
    )

    async def _calendar(*_a, **_k):
        return cal_c

    offered = []

    async def _reoffer(reply, tenant_, held, calendar):
        offered.append(held.start_at)
        return SlotTakenAtPromotion(reoffer=fr.FlowRouterResult(action="reply"))

    monkeypatch.setattr(workers_ns, "_appointment_calendar", _calendar)
    monkeypatch.setattr(worker_holds, "_held_slot_unavailable", _stale)
    monkeypatch.setattr(worker_holds, "_slot_taken_reoffer", _reoffer)

    result = await tasks._promote_booking_hold(
        _reply(conversation), tenant=tenant_c, waba_token=None, redis=None
    )

    assert isinstance(result, SlotTakenAtPromotion) and len(offered) == 1
    assert google.events_of("tok-c") == []
    assert await _appointments_of(db, tenant_c.id) == []


async def test_the_pix_promotion_is_refused_on_google(db, calls, google):
    tenant_c, x_c, cal_c = await _world(db)
    async with db() as session:
        deposit = PixDeposit(
            id=uuid4(), tenant_id=tenant_c.id, asaas_payment_id=f"pay_{uuid4().hex[:8]}",
            provider="asaas", amount_cents=6000, percent_applied=30,
            status=PixDepositStatus.PAID,
        )
        session.add(deposit)
        await session.commit()
        snapshot = booking_promotion.BookingSnapshot(
            tenant_id=tenant_c.id, conversation_id=None, patient_id=None,
            professional_id=x_c.id, appointment_type="Consulta", insurance=None,
            attendee_name=None, start_at=SLOT_UTC, end_at=SLOT_END,
        )
        tenant = await session.get(Tenant, tenant_c.id)
        # The payment path's own re-check already sees the sibling (Task 4)...
        assert await booking_promotion.slot_conflict(
            session, cal_c, snapshot, deposit=deposit
        ) == "calendar"
        # ...and the write refuses on its own when that re-check went stale.
        with pytest.raises(CalendarSlotTakenError):
            await booking_promotion.promote(session, tenant, deposit, snapshot, cal_c)

    assert google.events_of("tok-c") == []
    assert await _appointments_of(db, tenant_c.id) == []


# --------------------------------------------------------------------------
# The patient's edit (service change written on the agenda) - spec §7 items 3 and 5
# --------------------------------------------------------------------------


async def _c_appointment(db, tenant_c, x_c, cal_c, start):
    event = await cal_c.create_event(start, start + timedelta(minutes=30), "Consulta - Maria")
    async with db() as session:
        row = Appointment(
            tenant_id=tenant_c.id, professional_id=x_c.id, google_event_id=event["id"],
            appointment_type="Consulta", start_at=start, end_at=start + timedelta(minutes=30),
            status=AppointmentStatus.SCHEDULED,
        )
        session.add(row)
        await session.commit()
        return row


async def test_the_service_change_window_check_counts_the_sibling(db, calls, google):
    tenant_c, x_c, cal_c = await _world(db)
    # C's consultation ends where P's begins; a longer service would run into it.
    row = await _c_appointment(db, tenant_c, x_c, cal_c, SLOT_UTC - timedelta(minutes=30))
    async with db() as session:
        tenant = await session.get(Tenant, tenant_c.id)
        appointment = await session.get(Appointment, row.id)
        own_start = SLOT_UTC - timedelta(minutes=30)
        assert await service_change._window_fits(session, tenant, appointment, own_start, SLOT_UTC)
        assert not await service_change._window_fits(
            session, tenant, appointment, own_start, SLOT_END
        )


async def test_the_service_change_agenda_write_is_refused_on_google(db, calls, google, monkeypatch):
    tenant_c, x_c, cal_c = await _world(db)
    row = await _c_appointment(db, tenant_c, x_c, cal_c, SLOT_UTC - timedelta(minutes=30))

    async def _stale_free(*_a, **_k):
        return True

    monkeypatch.setattr(cal_c, "is_slot_free", _stale_free)
    async with db() as session:
        tenant = await session.get(Tenant, tenant_c.id)
        appointment = await session.get(Appointment, row.id)
        appointment.end_at = SLOT_END  # the longer service's window, as the edit wrote it
        with pytest.raises(CalendarSlotTakenError):
            await service_change.sync_agenda_event(
                session, tenant, appointment, cal_c, patient_name=None, window_changed=True
            )
    assert google.patches == []


# --------------------------------------------------------------------------
# release_keep - the clinic keeps a released paid slot (spec §7 item 5)
# --------------------------------------------------------------------------


class _BlindCalendar:
    """A calendar that sees nothing (the other clinic's booking is not on it)."""

    def __init__(self) -> None:
        self.created = []

    async def check_availability(self, start, end, max_results=50):
        return []

    async def create_event(self, start, end, summary, description="", reminders=None, *,
                           event_id=None):
        self.created.append(event_id)
        return {"id": event_id}

    async def cancel_event(self, event_id):
        return True


async def _released_with_sibling(db):
    async with db() as session:
        released = await make_released(
            session, professional_hours={"friday": [{"start": "08:00", "end": "18:00"}]}
        )
        offer = await ro.open_offer(
            session, tenant=released.tenant, appointment=released.appointment,
            deposit=released.deposit, now=T,
        )
        await ro.request_keep(session, offer=offer, now=T + timedelta(hours=1))
        await session.commit()
    tenant_p = await seed_tenant(db, name="Clínica P")
    x_p = await seed_professional(db, tenant_p, "Dr. X")
    await _link(db, released.appointment.professional_id, x_p.id)
    await _book(db, tenant_p.id, x_p.id, RELEASED_START, RELEASED_START + timedelta(minutes=30))
    return released, offer


async def _accept(db, released, offer_id):
    async with db() as session:
        offer = await session.get(DepositReleaseOffer, offer_id)
        tenant = await session.get(Tenant, released.tenant_id)
        return await rk.accept_keep(
            session, tenant=tenant, offer=offer, resolved_by="Ana", now=T + timedelta(hours=2)
        )


async def test_release_keep_counts_the_siblings_consultation(db, calls, monkeypatch):
    released, offer = await _released_with_sibling(db)
    blind = _BlindCalendar()

    async def _for_booking(session, tenant, professional_id):
        return blind

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", _for_booking)

    result = await _accept(db, released, offer.id)

    assert result.outcome == rk.ACCEPT_TAKEN
    assert blind.created == []


async def test_release_keep_write_is_refused_on_google(db, calls, google, monkeypatch):
    released, offer = await _released_with_sibling(db)
    cal = await _google_of(db, released.tenant_id, released.appointment.professional_id)

    async def _for_booking(session, tenant, professional_id):
        return cal

    async def _sees_nothing(*_a, **_k):
        return []

    async def _no_overlap(*_a, **_k):
        return False

    monkeypatch.setattr(booking_promotion, "calendar_for_booking", _for_booking)
    monkeypatch.setattr(cal, "check_availability", _sees_nothing)  # stale read
    monkeypatch.setattr(rk, "_consultation_overlaps", _no_overlap)  # stale read

    result = await _accept(db, released, offer.id)

    # Nothing is re-created; the clinic may refuse or try again (as on the local agenda).
    assert result.outcome in {rk.ACCEPT_TAKEN, rk.ACCEPT_UNAVAILABLE}
    assert google.events_of("tok-c") == []
    async with db() as session:
        live = await session.scalars(
            select(Appointment).where(
                Appointment.tenant_id == released.tenant_id,
                Appointment.status == AppointmentStatus.SCHEDULED,
            )
        )
        assert list(live) == []
